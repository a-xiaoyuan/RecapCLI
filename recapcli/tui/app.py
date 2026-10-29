"""炭黑底、等宽文字、薰衣草提示符。输入交给现有 Agent。"""

import asyncio
import queue
import threading

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Input, Static

from .driver import Presenter, ShellDriver, ShellOptions, describe_runtime
from .state import ShellState
from .theme import PALETTE, dot, render_line, spaced


VIOLET_STUDIO = Theme(
    name="violet-studio", primary=PALETTE["lavender"], secondary=PALETTE["lavender"],
    accent=PALETTE["lavender"], foreground=PALETTE["text"], background=PALETTE["base"],
    surface=PALETTE["base"], panel=PALETTE["stage"], success=PALETTE["online"],
    error=PALETTE["error"], dark=True,
)
DEMO_NOTE = "这是界面演示，输入未发送给模型。"
DEMO_PROMPT = "Can you summarize the key architectural decisions in the last session?"
DEMO_REPLY = (
    "Yes. In the last session we focused on three key decisions:\n\n"
    "  - Moving to a flat module structure to reduce indirection.\n\n"
    "  - Replacing the custom loader with an explicit registry pattern.\n\n"
    "  - Treating configuration as code, with validation at parse time."
)
MISSING_CONFIG = "模型配置未齐。发送前请在 RecapCLI 的 .env 或环境变量中设置 OPENAI_API_KEY 和 OPENAI_MODEL。"


def model_label(state, demo=False, profile_name=None):
    if demo:
        text = Text("gpt-4o-mini · preview  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
        text.append_text(dot(True))
        return text
    profile = None
    if profile_name:
        profile = next((item for item in state.profiles if item.get("name") == profile_name), None)
        if profile is None:
            text = Text(f"{profile_name}  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
            text.append_text(dot(False))
            return text
    else:
        profile = state.active_profile or {}
    name = (profile or {}).get("model") or "未配置模型"
    ready = bool(profile and profile.get("configured"))
    text = Text(name + "  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
    text.append_text(dot(ready))
    return text


class GlyphButton(Static):
    can_focus = True

    class Pressed(Message):
        def __init__(self, action):
            super().__init__()
            self.action = action

    def __init__(self, glyph, action, tooltip, **kwargs):
        super().__init__(glyph, markup=False, **kwargs)
        self.action = action
        self.tooltip = tooltip

    def on_click(self):
        self.post_message(self.Pressed(self.action))

    def on_key(self, event):
        if event.key in {"enter", "space"}:
            event.stop()
            self.post_message(self.Pressed(self.action))


class RecapShell(App):
    """终端壳。--demo 只演示界面；其余输入走现有 run_agent。"""

    CSS_PATH = "violet.tcss"
    TITLE = "RecapCLI"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+b", "toggle_rail", "图标轨"), Binding("ctrl+l", "clear", "清屏"),
                Binding("ctrl+q", "quit", "退出", priority=True)]

    def __init__(self, state: ShellState, *, demo=False, options: ShellOptions | None = None, client_factory=None):
        super().__init__()
        self.state = state
        self.demo = demo
        self.options = options or ShellOptions(workspace=state.workspace)
        self.client_factory = client_factory
        self._cursor_visible = True
        self._busy = False
        self._approval = False
        self.ui_thread = None
        self.approval_queue = queue.Queue()
        self.driver = None
        if not demo:
            from openai import OpenAI
            self.driver = ShellDriver(
                Presenter(self), self.options,
                client_factory=client_factory or OpenAI, ready=state.ready,
            )

    def compose(self) -> ComposeResult:
        with Horizontal(id="shell"):
            with Vertical(id="rail"):
                for glyph, action, tooltip in (("~", "home", "回到对话"), ("+", "new", "新会话"),
                                                ("✓", "tasks", "任务记录"), ("◷", "history", "已保存会话"),
                                                ("…", "help", "命令与快捷键")):
                    yield GlyphButton(glyph, action, tooltip, id=f"rail-{action}", classes="rail-button")
            with Vertical(id="stage"):
                with Horizontal(id="topbar"):
                    brand = Static("RECAPCLI", id="brand", markup=False)
                    brand.tooltip = str(self.state.workspace)
                    yield brand
                    yield Static(Text(spaced("VIOLET STUDIO"), style=PALETTE["faint"]), id="studio")
                    status = Static(model_label(self.state, self.demo, None if self.demo else self.options.profile), id="model")
                    status.tooltip = "演示数据" if self.demo else "本地配置齐全时为绿点，不代表接口已探测"
                    yield status
                with VerticalScroll(id="conversation"):
                    yield Vertical(id="transcript")
                with Horizontal(id="composer"):
                    yield Static(">", id="input-prefix", markup=False)
                    placeholder = "界面演示，不发送请求" if self.demo else "输入问题…"
                    yield Input(placeholder=placeholder, id="prompt")
                    yield GlyphButton("➤", "send", "发送（Enter）", id="send")

    async def on_mount(self):
        self.ui_thread = threading.get_ident()
        self.register_theme(VIOLET_STUDIO)
        self.theme = "violet-studio"
        self._apply_width(self.size.width)
        if self.demo:
            await self._append(DEMO_PROMPT, "user")
            await self._append(DEMO_REPLY, "assistant")
            await self.query_one("#transcript").mount(Static(self._cursor_text(), id="stream-cursor"))
            self.set_interval(0.6, self._blink_cursor)
            for note in self.state.notes:
                await self._append(note, "assistant")
        else:
            await self._append(describe_runtime(self.options), "assistant")
            if not self.state.ready and not any("模型配置" in note for note in self.state.notes):
                await self._append(MISSING_CONFIG, "info")
            for note in self.state.notes:
                await self._append(note, "info")
            for note in self.driver.prepare():
                await self._append(note, "info")
            if self.driver.session and self.driver.session.get("model"):
                self.apply_model(self.driver.session["model"], self.state.ready)
            for kind, text in self.driver.transcript_rows():
                await self._append(text, kind)
        self.query_one("#prompt", Input).focus()

    def on_unmount(self):
        if self.driver is not None:
            self.driver.close()

    def on_resize(self, event):
        self._apply_width(event.size.width)

    def _apply_width(self, width):
        self.screen.set_class(width < 110, "-compact")
        self.screen.set_class(width < 65, "-narrow")

    def _cursor_text(self):
        text = Text("  Working on the summary now  ", style=PALETTE["text"])
        text.append("▏" if self._cursor_visible else " ", style=PALETTE["lavender"])
        return text

    def _blink_cursor(self):
        if self.query("#stream-cursor"):
            self._cursor_visible = not self._cursor_visible
            self.query_one("#stream-cursor", Static).update(self._cursor_text())

    def _static(self, content, kind):
        return Static(render_line(content, kind), classes="turn", markup=False)

    async def _append(self, content, kind="user"):
        await self.query_one("#transcript").mount(self._static(content, kind))
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    def append_line(self, content, kind):
        self.query_one("#transcript").mount(self._static(content, kind))
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    async def clear_transcript_sync(self):
        await self.query_one("#transcript").remove_children()

    async def replace_transcript_sync(self, rows):
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        for kind, text in rows:
            await transcript.mount(self._static(text, kind))
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    def apply_model(self, model, ready):
        text = Text(str(model) + "  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
        text.append_text(dot(True if ready else False))
        self.query_one("#model", Static).update(text)

    def begin_approval(self, message):
        self.append_line(message, "event")
        self._approval = True
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "approve、reject 或 cancel"
        prompt.focus()

    def end_approval(self):
        self._approval = False
        if self._busy:
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = True
            prompt.placeholder = "正在处理…"

    def _lock(self):
        self._busy = True
        self._approval = False
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        prompt.placeholder = "正在处理…"

    def _unlock(self):
        self._busy = False
        self._approval = False
        while True:
            try:
                self.approval_queue.get_nowait()
            except queue.Empty:
                break
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "界面演示，不发送请求" if self.demo else "输入问题…"
        prompt.focus()

    def _submit_value(self, value):
        if self.demo:
            if value in {"/exit", "/quit"}:
                self.exit()
            elif value == "/clear":
                self.query_one("#transcript").remove_children()
            elif value:
                self.append_line(value, "user")
                self.append_line(DEMO_NOTE, "assistant")
            return
        if self._approval:
            if value:
                self.approval_queue.put(value)
            return
        if self._busy:
            if value:
                self.append_line("上一条还在处理。", "info")
            return
        if not value:
            return
        if not value.startswith("/"):
            self.append_line(value, "user")
        self._lock()
        self.run_worker(self._worker_line(value), name="turn", group="agent", exit_on_error=False)

    async def _worker_line(self, line):
        action = None
        try:
            action = await asyncio.to_thread(self.driver.handle_line, line)
        except Exception as error:
            self.append_line(f"界面处理失败：{type(error).__name__}: {error}", "error")
        finally:
            if self.driver is None or not self.driver.quitting:
                self._unlock()
        if self.driver is not None and self.driver.quitting and action != "exit":
            self.driver.save_if_needed()
        if action == "exit" or (self.driver is not None and self.driver.quitting):
            self.exit()

    async def _submit(self):
        prompt = self.query_one("#prompt", Input)
        value = prompt.value.strip()
        if self._busy and not self._approval and not self.demo:
            if value:
                self.append_line("上一条还在处理。", "info")
            return
        prompt.value = ""
        self._submit_value(value)
        prompt.focus()

    async def on_input_submitted(self, event: Input.Submitted):
        await self._submit()

    async def on_glyph_button_pressed(self, event: GlyphButton.Pressed):
        if event.action == "send":
            await self._submit()
        elif event.action == "new":
            if self.demo:
                await self.clear_transcript_sync()
            elif self._busy:
                self.append_line("上一条还在处理。", "info")
            else:
                self._submit_value("/new")
        elif event.action == "history":
            if self._busy and not self.demo:
                self.append_line("上一条还在处理。", "info")
            elif self.demo:
                lines = [f"{item.get('id', '?')}  ·  {item.get('messages', 0)} 条消息" for item in self.state.sessions]
                self.append_line("已保存会话\n" + ("\n".join(lines) or "暂无会话。"), "assistant")
            else:
                self.driver.show_sessions()
        elif event.action == "tasks":
            if self._busy and not self.demo:
                self.append_line("上一条还在处理。", "info")
            elif self.demo:
                self.append_line("任务记录在正式会话中用 /tasks 或 /task ID 查看。", "assistant")
            else:
                self.driver.show_tasks()
        elif event.action == "help":
            if self.demo:
                self.append_line("Enter 发送演示 · Ctrl+L 清屏 · Ctrl+B 图标轨 · Ctrl+Q 退出。\n" + DEMO_NOTE, "assistant")
            else:
                self.driver.show_help()
        else:
            self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)
        self.query_one("#prompt", Input).focus()

    async def action_clear(self):
        await self.clear_transcript_sync()
        self.query_one("#prompt", Input).focus()

    def action_toggle_rail(self):
        self.query_one("#rail").toggle_class("-hidden")

    def action_quit(self):
        if self.driver is not None and self._busy:
            self.driver.request_stop()
            return
        if self.driver is not None:
            self.driver.save_if_needed()
        self.exit()
