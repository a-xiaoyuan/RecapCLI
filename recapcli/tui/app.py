"""黑底薄荷绿终端：对话、输入和真实任务状态。"""

import asyncio
import queue
import threading

from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Input, Static

from .driver import Presenter, ShellDriver, ShellOptions, describe_runtime
from .state import ShellState
from .status import TaskStatus, task_text, usage_text
from .theme import DISPLAY_LIMIT, PALETTE, dot, render_line, spaced
from ..ui.terminal_ui import clean_output


MINT_TERMINAL = Theme(
    name="mint-terminal", primary=PALETTE["mint"], secondary=PALETTE["mint"],
    accent=PALETTE["mint"], foreground=PALETTE["text"], background=PALETTE["base"],
    surface=PALETTE["base"], panel=PALETTE["stage"], success=PALETTE["online"],
    error=PALETTE["error"], dark=True,
)
DEMO_NOTE = "这是界面演示，输入未发送给模型。"
DEMO_PROMPT = "读取项目入口，说明 RecapCLI 怎样处理一次提问。"
DEMO_REPLY = (
    "### 从问题到回答\n\n"
    "RecapCLI 把问题交给现有 Agent。需要代码时读取文件，工具结果回传后继续回答。\n\n"
    "```python\nfrom recapcli.core.agent import run_agent\n\n"
    "result = run_agent(\n    client, model, prompt, workspace,\n    capabilities=(\"read\",),\n)\n"
    "print(result.answer)\n```\n\n"
    "- 回答逐段显示，阅读位置自动跟到底部。\n"
    "- 编辑和命令执行仍遵守能力开关与审批。\n"
    "- 右栏显示模型、工具能力和真实任务用量。\n\n"
    "*这是界面演示，没有发送模型请求。*"
)
MISSING_CONFIG = "模型配置未齐。发送前请在 RecapCLI 的 .env 或环境变量中设置 OPENAI_API_KEY 和 OPENAI_MODEL。"


def model_label(state, demo=False, profile_name=None):
    if demo:
        text = Text("演示模型 · preview  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
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


class ConversationLine(Static):
    """回答使用 Markdown；用户输入保持字面显示。"""

    def __init__(self, content, kind):
        self.kind = kind
        self.content_text = ""
        super().__init__("", classes=f"turn {kind}-turn", markup=False)
        self.set_content(content)

    def set_content(self, content, kind=None):
        if kind is not None:
            self.remove_class(f"{self.kind}-turn")
            self.kind = kind
            self.add_class(f"{kind}-turn")
        self.content_text = clean_output(content)
        shown = self.content_text[:DISPLAY_LIMIT]
        if len(self.content_text) > DISPLAY_LIMIT:
            shown += "\n…（显示已截断，完整结果见 /task ID）"
        if self.kind == "assistant":
            rendered = Markdown(shown, code_theme="monokai", hyperlinks=False)
            # 对话里的普通换行仍按原回答显示，避免 Markdown 将多行挤成一段。
            for token in rendered.parsed:
                for child in token.children or ():
                    if child.type == "softbreak":
                        child.type = "hardbreak"
        else:
            rendered = render_line(shown, self.kind)
        self.update(rendered)


class RecapShell(App):
    """终端壳。--demo 只演示界面；其余输入走现有 run_agent。"""

    CSS_PATH = "violet.tcss"
    TITLE = "RecapCLI"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+l", "clear", "清屏"),
                Binding("ctrl+g", "toggle_inspector", "状态栏"),
                Binding("ctrl+q", "quit", "退出", priority=True)]

    def __init__(self, state: ShellState, *, demo=False, options: ShellOptions | None = None, client_factory=None):
        super().__init__()
        self.console.push_theme(RichTheme({
            **{f"markdown.h{level}": "bold " + PALETTE["text"] for level in range(1, 7)},
            "markdown.code": PALETTE["mint"] + " on " + PALETTE["stage"],
            "markdown.item.bullet": PALETTE["mint"],
            "markdown.link": "underline " + PALETTE["mint"],
            "markdown.link_url": PALETTE["muted"],
        }))
        self.state = state
        self.demo = demo
        self.options = options or ShellOptions(workspace=state.workspace)
        self.client_factory = client_factory
        self._cursor_visible = True
        self._busy = False
        self._approval = False
        self._response_widget = None
        self._response_text = ""
        self._task_status = TaskStatus()
        self._display_model = ""
        self._model_ready = state.ready
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
            with Vertical(id="stage"):
                with Horizontal(id="topbar"):
                    brand = Static(self.state.workspace.name or "RecapCLI", id="brand", markup=False)
                    brand.tooltip = str(self.state.workspace)
                    yield brand
                    mode = {"react": "REACT", "plan": "PLAN", "multi": "MULTI · 只读"}[self.options.mode]
                    yield Static(Text(mode, style=PALETTE["faint"]), id="studio")
                    status = Static(model_label(self.state, self.demo, None if self.demo else self.options.profile), id="model")
                    status.tooltip = "演示数据" if self.demo else "本地配置齐全时为绿点，不代表接口已探测"
                    yield status
                with VerticalScroll(id="conversation"):
                    yield Vertical(id="transcript")
                yield Static("", id="activity", markup=False)
                with Horizontal(id="composer"):
                    yield Static(">", id="input-prefix", markup=False)
                    placeholder = "界面演示，不发送请求" if self.demo else "输入问题…"
                    yield Input(placeholder=placeholder, id="prompt")
                    yield GlyphButton("➤", "send", "发送（Enter）", id="send")
                yield Static("enter 发送  ·  tab 焦点  ·  ctrl+l 清屏  ·  ctrl+g 状态  ·  ctrl+q 退出", id="key-hints", markup=False)
            with VerticalScroll(id="inspector"):
                yield Static(Text(spaced("RECAPCLI"), style=PALETTE["mint"] + " bold"), id="inspector-brand")
                yield Static("界面演示 · 不请求模型" if self.demo else "TERMINAL WORKSPACE", id="inspector-subtitle")
                yield Static("当前任务", classes="section-title")
                yield Static("就绪", id="task-status", markup=False)
                yield Static("", id="task-reference", classes="secondary", markup=False)
                yield Static("模型", classes="section-title")
                yield Static("", id="inspector-model", markup=False)
                yield Static("", id="model-capabilities", classes="secondary", markup=False)
                yield Static("Tokens · 本次任务", classes="section-title")
                yield Static("—\n还没有任务用量", id="usage", markup=False)
                yield Static("工作区", classes="section-title")
                yield Static(str(self.state.workspace), id="workspace-path", markup=False)
                yield Static("工具能力", classes="section-title")
                yield Static("", id="tool-capabilities", markup=False)
                yield Static("修改文件", classes="section-title")
                yield Static("暂无编辑记录", id="modified-files", markup=False)
                yield Static("编辑 / 恢复工具记录", classes="secondary")
                yield Static("LSP", classes="section-title")
                yield Static("○ 未接入", classes="secondary")
                yield Static("MCP", classes="section-title")
                yield Static("○ 未接入", classes="secondary")

    async def on_mount(self):
        self.ui_thread = threading.get_ident()
        self.register_theme(MINT_TERMINAL)
        self.theme = "mint-terminal"
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
        self._refresh_inspector()

    def on_unmount(self):
        if self.driver is not None:
            self.driver.close()

    def on_resize(self, event):
        self._apply_width(event.size.width)

    def _apply_width(self, width):
        self.screen.set_class(width < 120, "-compact")
        self.screen.set_class(width < 65, "-narrow")
        if self.query("#key-hints"):
            self.query_one("#key-hints", Static).update(
                "enter 发送 · ctrl+g 状态 · ctrl+q 退出" if width < 85 else
                "enter 发送  ·  tab 焦点  ·  ctrl+l 清屏  ·  ctrl+g 状态  ·  ctrl+q 退出")

    def _cursor_text(self):
        text = Text("  流式光标演示  ", style=PALETTE["faint"])
        text.append("▏" if self._cursor_visible else " ", style=PALETTE["mint"])
        return text

    def _blink_cursor(self):
        if self.query("#stream-cursor"):
            self._cursor_visible = not self._cursor_visible
            self.query_one("#stream-cursor", Static).update(self._cursor_text())

    def _static(self, content, kind):
        return ConversationLine(content, kind)

    def _follow_latest(self):
        conversation = self.query_one("#conversation", VerticalScroll)
        # 挂载和换行可能跨多次布局刷新；锚定会在内容高度变化后继续跟到底部。
        # 手动上翻仍可阅读历史，下一条消息到达时再恢复跟随。
        conversation.anchor()
        conversation.scroll_end(animate=False)

    async def _append(self, content, kind="user"):
        await self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest()

    def append_line(self, content, kind):
        self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest()

    def stream_update(self, kind, text):
        if kind == "start":
            self._response_widget = None
            self._response_text = ""
        elif kind == "delta":
            self._response_text += text
            if self._response_widget is None:
                self._response_widget = self._static(self._response_text, "assistant")
                self.query_one("#transcript").mount(self._response_widget)
            else:
                self._response_widget.set_content(self._response_text)
            self._follow_latest()
        elif kind == "abort":
            if self._response_widget is not None:
                self._response_widget.set_content("[回答未完成]\n" + self._response_text, "error")
            self._response_widget = None
            self._response_text = ""

    def finish_answer(self, answer):
        if self._response_widget is not None:
            self._response_widget.set_content(answer)
            self._follow_latest()
        else:
            self.append_line(answer, "assistant")
        self._response_widget = None
        self._response_text = ""

    async def clear_transcript_sync(self):
        self._response_widget = None
        self._response_text = ""
        await self.query_one("#transcript").remove_children()

    async def replace_transcript_sync(self, rows):
        self._task_status = TaskStatus()
        self._refresh_inspector()
        self._response_widget = None
        self._response_text = ""
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        for kind, text in rows:
            await transcript.mount(self._static(text, kind))
        self._follow_latest()

    def apply_model(self, model, ready):
        self._display_model = str(model)
        self._model_ready = bool(ready)
        text = Text(str(model) + "  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
        text.append_text(dot(True if ready else False))
        self.query_one("#model", Static).update(text)
        self._refresh_inspector()

    def apply_task(self, task):
        self._task_status = TaskStatus.from_task(task)
        self._refresh_inspector()

    def _refresh_inspector(self):
        if not self.query("#inspector-model"):
            return
        profile = next((item for item in self.state.profiles if item.get("name") == self.options.profile), None) if self.options.profile else self.state.active_profile
        metadata = self.driver.client.metadata() if self.driver is not None and self.driver.client is not None else None
        capabilities = metadata.get("capabilities", {}) if metadata else (profile or {})
        model = self._display_model or (profile or {}).get("model") or "未配置模型"
        label = Text()
        label.append_text(dot(self._model_ready if not self.demo else None))
        label.append(" " + model, style=PALETTE["text"])
        self.query_one("#inspector-model", Static).update(label)
        self.query_one("#inspector-model").tooltip = "本地配置齐全，不代表接口已经验证可用"
        flags = ["图片" if capabilities.get("images") else "纯文本", "流式" if capabilities.get("streaming") else "整段"]
        self.query_one("#model-capabilities", Static).update(" · ".join(flags))
        tools = {"read": "读取与检索", "write": "编辑与恢复", "exec": "执行命令", "web": "网页搜索"}
        granted = "\n".join("• " + label for key, label in tools.items() if key in self.options.capabilities)
        self.query_one("#tool-capabilities", Static).update(granted if capabilities.get("tools", True) else "当前模型未开放工具")
        task = self._task_status
        self.query_one("#task-status", Static).update(task_text(task, busy=self._busy, approval=self._approval))
        self.query_one("#task-reference", Static).update(task.task_id[:12] if task.task_id else "等待第一次提问")
        self.query_one("#usage", Static).update(usage_text(task))
        self.query_one("#modified-files", Static).update(clean_output("\n".join(task.modified_files)) or "暂无编辑记录")

    def begin_approval(self, message):
        self.append_line(message, "event")
        self._approval = True
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "approve、reject 或 cancel"
        prompt.focus()
        self._refresh_inspector()

    def end_approval(self):
        self._approval = False
        if self._busy:
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = True
            prompt.placeholder = "正在处理…"
        self._refresh_inspector()

    def _lock(self):
        self._busy = True
        self._approval = False
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        prompt.placeholder = "正在处理…"
        self.query_one("#activity", Static).update(Text("● 正在处理…", style=PALETTE["mint"]))
        self._refresh_inspector()

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
        self.query_one("#activity", Static).update("")
        self._refresh_inspector()

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
        self.query_one("#prompt", Input).focus()

    async def action_clear(self):
        await self.clear_transcript_sync()
        self.query_one("#prompt", Input).focus()

    def action_toggle_inspector(self):
        self.screen.toggle_class("-inspector-hidden")

    def action_quit(self):
        if self.driver is not None and self._busy:
            self.driver.request_stop()
            return
        if self.driver is not None:
            self.driver.save_if_needed()
        self.exit()
