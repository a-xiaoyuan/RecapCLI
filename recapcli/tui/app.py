"""流式对话、输入区命令补全与审批交互。"""

import asyncio
import queue
import threading
import time
import tomllib

from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.theme import Theme
from textual.widgets import OptionList, Static, TextArea

from .driver import Presenter, ShellDriver, ShellOptions
from .state import ShellState
from .status import TaskStatus, task_text, usage_text
from .theme import DISPLAY_LIMIT, PALETTE, render_line, render_wordmark
from .widgets import CommandMenu, ConversationView, PromptInput, StatusScreen
from .settings import ModelConfigScreen, ModelPickerScreen
from ..ui.terminal_ui import clean_output
from ..config import ConfigError, PROJECT_ROOT, list_model_profiles
from ..application.help import command_items, help_text
from ..core.project_rules import project_rules_description
from ..application.changes import change_report, git_summary, files_summary, commands_summary


TERMINAL_THEME = Theme(
    name="recap-terminal", primary=PALETTE["accent"], secondary=PALETTE["accent"],
    accent=PALETTE["accent"], foreground=PALETTE["text"], background=PALETTE["base"],
    surface=PALETTE["base"], panel=PALETTE["stage"], success=PALETTE["online"],
    error=PALETTE["error"], dark=True,
)
DEMO_NOTE = "这是界面演示，输入未发送给模型。"
DEMO_PROMPT = "你好，介绍一下你能做什么。"
DEMO_REPLY = (
    "你好！我是 RecapCLI，可以陪你阅读和修改这个项目。\n\n"
    "- 定位文件、搜索代码，解释调用流程。\n"
    "- 按当前能力开关编辑文件、执行命令；写入和命令仍需审批。\n"
    "- 使用支持视觉的模型分析附加图片。\n\n"
    "直接告诉我你想完成什么，也可以粘贴代码和报错。\n\n"
    "*这是界面演示，没有发送模型请求。*"
)
MISSING_CONFIG = "还没有可用的模型配置。按 F3 填写模型、接口地址和 API Key；F2 可选择已有配置。"


def version_label():
    """版本读取项目元数据，避免在界面里维护第二份版本号。"""
    try:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as source:
            release = tomllib.load(source)["project"]["version"]
        label = "v" + clean_output(str(release))
    except (OSError, KeyError, TypeError, ValueError):
        label = "开发版"
    return Text(label, style=PALETTE["muted"], justify="center", no_wrap=True)


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
            rendered = Markdown(shown, code_theme="ansi_dark", hyperlinks=False)
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
    BINDINGS = [Binding("f1", "show_help", "帮助", priority=True),
                Binding("f2", "choose_model", "选择模型", priority=True),
                Binding("f3", "configure_model", "配置模型", priority=True),
                Binding("ctrl+l", "clear", "清屏"),
                Binding("ctrl+g", "show_status", "详情", priority=True),
                Binding("escape", "cancel_turn", "停止本轮", priority=True),
                Binding("ctrl+end", "jump_latest", "回到底部", priority=True),
                Binding("alt+pageup", "history_up", "上翻历史", priority=True),
                Binding("alt+pagedown", "history_down", "下翻历史", priority=True),
                Binding("ctrl+q", "quit", "退出", priority=True)]

    def __init__(self, state: ShellState, *, demo=False, options: ShellOptions | None = None, client_factory=None):
        super().__init__()
        self.console.push_theme(RichTheme({
            **{f"markdown.h{level}": "bold " + PALETTE["text"] for level in range(1, 7)},
            "markdown.code": PALETTE["accent"],
            "markdown.item.bullet": PALETTE["muted"],
            "markdown.link": "underline " + PALETTE["accent"],
            "markdown.link_url": PALETTE["muted"],
        }))
        self.state = state
        self.demo = demo
        self.options = options or ShellOptions(workspace=state.workspace)
        self.client_factory = client_factory
        self._cursor_visible = True
        self._busy = False
        self._busy_since = None
        self._activity = ""
        self._approval = False
        self._approval_draft = ""
        self._cancel_requested = False
        self._has_new_content = False
        self._response_widget = None
        self._response_text = ""
        self._task_status = TaskStatus()
        self._display_model = ""
        self._profiles = list(state.profiles)
        self._command_dismissed = None
        self._command_anchor = None
        selected = next((p for p in state.profiles if p.get("name") == self.options.profile), {}) if self.options.profile else (state.active_profile or {})
        self._model_ready = bool(selected.get("configured"))
        self.ui_thread = None
        self.approval_queue = queue.Queue()
        self.driver = None
        if not demo:
            from openai import OpenAI
            self.driver = ShellDriver(
                Presenter(self), self.options,
                client_factory=client_factory or OpenAI, ready=self._model_ready,
            )

    def _selected_profile(self):
        """与驱动的选择顺序一致：已打开客户端、显式配置、恢复会话、默认项。"""
        client = self.driver.client if self.driver is not None else None
        if client is not None:
            name = getattr(client.config, "profile", "default")
        else:
            session = self.driver.session if self.driver is not None else None
            name = self.options.profile or (session or {}).get("model_profile")
        if name:
            return next((p for p in self._profiles if p.get("name") == name), {"name": name})
        return next((p for p in self._profiles if p.get("default")), self._profiles[0] if self._profiles else {})

    def compose(self) -> ComposeResult:
        with Horizontal(id="shell"):
            with Vertical(id="stage"):
                with ConversationView(id="conversation"):
                    with Vertical(id="history"):
                        with Vertical(id="welcome"):
                            yield Static(render_wordmark(0), id="brand", markup=False)
                            yield Static(version_label(), id="version", markup=False)
                        yield Vertical(id="transcript")
                yield CommandMenu(id="command-menu")
                yield Static("", id="command-help", markup=False)
                yield Static("", id="command-hint", markup=False)
                with Horizontal(id="status-row"):
                    yield Static("", id="status-line", markup=False)
                    yield GlyphButton("↓ 回到底部", "latest", "回到底部并恢复自动跟随（Ctrl+End）", id="jump-latest")
                with Horizontal(id="composer"):
                    yield Static(">", id="input-prefix", markup=False)
                    placeholder = "界面演示，不发送请求" if self.demo else "输入问题…"
                    yield PromptInput(placeholder=placeholder, id="prompt")
                    yield GlyphButton("↵", "send", "发送（Enter）", id="send")
                yield Static("", id="key-hints", markup=False)

    async def on_mount(self):
        self.ui_thread = threading.get_ident()
        self.register_theme(TERMINAL_THEME)
        self.theme = "recap-terminal"
        self._apply_width(self.size.width)
        self.set_interval(1, self._tick_status)
        if self.demo:
            await self._append(DEMO_PROMPT, "user")
            await self._append(DEMO_REPLY, "assistant")
            await self.query_one("#transcript").mount(Static(self._cursor_text(), id="stream-cursor"))
            self.set_interval(0.6, self._blink_cursor)
            for note in self.state.notes:
                await self._append(note, "assistant")
        else:
            startup_notes = self.driver.prepare()
            profile = self._selected_profile()
            self._model_ready = bool(profile.get("configured"))
            if not self._model_ready and not any("模型配置" in note for note in self.state.notes):
                await self._append(MISSING_CONFIG, "info")
            for note in self.state.notes:
                await self._append(note, "info")
            for note in startup_notes:
                await self._append(note, "info")
            if self.driver.session:
                self.apply_model(profile.get("model") or self.driver.session.get("model") or "未配置模型", self._model_ready)
            for kind, text in self.driver.transcript_rows():
                await self._append(text, kind)
        self.query_one("#prompt", PromptInput).focus()
        self._follow_latest(force=True)
        self._refresh_status()

    def on_unmount(self):
        if self.driver is not None:
            self.driver.close()

    def on_resize(self, event):
        self._apply_width(event.size.width)

    def _apply_width(self, width):
        if self.screen_stack:
            self.screen_stack[0].set_class(width < 65, "-narrow")
        if self.query("#brand"):
            self.query_one("#brand", Static).update(render_wordmark(max(0, width - 10), compact=self.size.height < 22))
        self._refresh_hints()

    def _cursor_text(self):
        text = Text("  ", style=PALETTE["faint"])
        text.append("▏" if self._cursor_visible else " ", style=PALETTE["accent"])
        return text

    def _blink_cursor(self):
        if self.query("#stream-cursor"):
            self._cursor_visible = not self._cursor_visible
            self.query_one("#stream-cursor", Static).update(self._cursor_text())

    def _static(self, content, kind):
        return ConversationLine(content, kind)

    def _follow_latest(self, *, force=False):
        conversation = self.query_one("#conversation", ConversationView)
        if force:
            conversation.follow_latest()
            self._has_new_content = False
        elif conversation.following:
            conversation.scroll_end(animate=False)
        else:
            self._has_new_content = True
        self._refresh_navigation()

    def _refresh_navigation(self):
        if self.query("#jump-latest"):
            following = self.query_one("#conversation", ConversationView).following
            if following:
                self._has_new_content = False
            button = self.query_one("#jump-latest", GlyphButton)
            button.display = not following
            button.update("↓ 新内容" if self._has_new_content else "↓ 回到底部")

    def on_conversation_view_following_changed(self):
        self._refresh_navigation()

    async def _append(self, content, kind="user"):
        await self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest(force=True)

    def append_line(self, content, kind):
        self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest(force=kind == "user")

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
        self._follow_latest(force=True)

    async def replace_transcript_sync(self, rows):
        self._task_status = TaskStatus()
        self._refresh_status()
        self._response_widget = None
        self._response_text = ""
        transcript = self.query_one("#transcript")
        await transcript.remove_children()
        for kind, text in rows:
            await transcript.mount(self._static(text, kind))
        self._follow_latest(force=True)

    def apply_model(self, model, ready):
        if self.driver is not None and self.driver.client is not None:
            model = self.driver.client.model
        self._display_model = str(model)
        self._model_ready = bool(ready)
        self._refresh_status()

    def apply_task(self, task):
        self._task_status = TaskStatus.from_task(task)
        self._refresh_status()

    def _status_details(self):
        profile = self._selected_profile()
        metadata = self.driver.client.metadata() if self.driver is not None and self.driver.client is not None else None
        capabilities = metadata.get("capabilities", {}) if metadata else (profile or {})
        model = self._display_model or (profile or {}).get("model") or "未配置模型"
        flags = ["图片" if capabilities.get("images") else "纯文本", "流式" if capabilities.get("streaming") else "整段"]
        tools = {"read": "读取与检索", "write": "编辑与恢复", "exec": "执行命令", "web": "网页搜索"}
        granted = "\n".join("• " + label for key, label in tools.items() if key in self.options.capabilities)
        task = self._task_status
        report = task.changes or change_report({})
        text = Text()
        for title, content in (
            ("模型", model + " · " + profile.get("name", "default") + "\n" + " · ".join(flags) + "\n" + ("本地配置齐全，接口尚需实际请求验证" if self._model_ready else "模型配置未齐")),
            ("工作区", str(self.state.workspace)),
            ("项目规则", project_rules_description(task.project_rules)),
            ("开始时的 Git 状态 · 左列暂存区 / 右列工作区", git_summary(report["git"])),
            ("模式与工具", self.options.mode + "\n" + (granted if capabilities.get("tools", True) else "当前模型未开放工具")),
            ("本次任务", task_text(task, busy=self._busy, approval=self._approval) + "\n" + (task.task_id or "还没有任务")),
            ("Token 用量", usage_text(task)),
            ("本轮文件 · 编辑 / 恢复工具记录", files_summary(report)),
            ("本轮命令记录", commands_summary(report)),
        ):
            text.append(title + "\n", style=PALETTE["accent"] + " bold")
            text.append(clean_output(content) + "\n\n", style=PALETTE["text"])
        return text

    def _tick_status(self):
        if self._busy:
            self._refresh_status()

    def set_activity(self, activity):
        self._activity = clean_output(activity)
        self._refresh_status()

    def _refresh_status(self):
        if not self.query("#status-line"):
            return
        label = "停止中…等待在途操作返回" if self._cancel_requested else task_text(self._task_status, busy=self._busy, approval=self._approval)
        if self._busy and not self._approval and not self._cancel_requested and self._busy_since is not None:
            elapsed = max(0, int(time.monotonic() - self._busy_since))
            activity = self._activity or "正在处理"
            if self._response_text and activity.startswith("请求模型"):
                activity = "正在回答"
            label = f"{activity} · {elapsed}s"
        if self._task_status.status == "failed" and not self._busy and "APITimeoutError" in self._task_status.stop_reason:
            label += " · 模型请求超时，详情见上方提示"
        if self.demo:
            label = "界面演示 · 不请求模型"
        usage = self._task_status.usage or {}
        total = usage.get("total_tokens")
        if isinstance(total, int):
            label += f" · {total:,} tokens"
        if not self.demo and not self._busy and not self._task_status.task_id:
            label = "" if self._model_ready else "模型配置未齐 · /config 设置"
        color = PALETTE["error"] if self._task_status.status == "failed" and not self._busy else (PALETTE["accent"] if self._busy else PALETTE["faint"])
        self.query_one("#status-line", Static).update(Text(label, style=color, no_wrap=True, overflow="ellipsis"))
        send = self.query_one("#send", GlyphButton)
        send.update("■" if self._busy else "↵")
        send.tooltip = "停止本轮（Esc）" if self._busy else "发送（Enter）"
        self._refresh_hints()
        if isinstance(self.screen, StatusScreen) and self.screen.query("#status-body"):
            self.screen.query_one("#status-body", Static).update(self._status_details())

    def _refresh_hints(self):
        if not self.query("#key-hints"):
            return
        if self._cancel_requested:
            hint = "正在停止 · 草稿保留 · ctrl+q 退出"
        elif self._approval:
            hint = "输入 approve / reject / cancel · esc 停止本轮"
        elif self._busy:
            hint = "esc 停止 · 可编辑下一条草稿 · ctrl+g 详情"
        else:
            hint = "/model 切换 · /config 设置 · /help 命令"
        model = self._display_model or self._selected_profile().get("model") or "未配置模型"
        label = Text(clean_output(model) + " · " + hint, style=PALETTE["faint"], no_wrap=True, overflow="ellipsis")
        self.query_one("#key-hints", Static).update(label)

    def _hide_commands(self):
        self._command_anchor = None
        for selector in ("#command-menu", "#command-help", "#command-hint"):
            self.query_one(selector).display = False

    def _show_commands(self, topic="common", query=""):
        if len(self.screen_stack) > 1:
            return
        self._command_anchor = self.query_one("#prompt", PromptInput).text
        menu = self.query_one("#command-menu", CommandMenu)
        items = command_items(query, topic)
        menu.show_commands(items, max(3, min(7, self.size.height // 3)))
        body = self.query_one("#command-help", Static)
        body.display = topic == "keys" or (not items and topic not in {"all", "common"})
        if body.display:
            body.update(help_text(topic))
        hint = self.query_one("#command-hint", Static)
        hint.display = menu.display or body.display
        hint.update("Esc 收起" if body.display else "↑↓ 选择 · Tab 补全 · Enter 确认 · Esc 收起")

    def on_text_area_changed(self, event: TextArea.Changed):
        if event.text_area.id != "prompt" or not self.query("#command-menu"):
            return
        value = event.text_area.text
        if value == self._command_anchor:
            return
        if self._busy or self._approval or value == self._command_dismissed or not value.startswith("/") or any(char.isspace() for char in value):
            self._hide_commands()
        else:
            self._show_commands("all", value)

    def command_menu_key(self, event):
        menu = self.query_one("#command-menu", CommandMenu)
        if not menu.display:
            return False
        if event.key in {"up", "down"}:
            if event.key == "up":
                menu.action_cursor_up()
            else:
                menu.action_cursor_down()
        elif event.key in {"tab", "enter"}:
            self._choose_command(insert_only=event.key == "tab")
        else:
            return False
        return True

    def _choose_command(self, *, insert_only=False):
        menu = self.query_one("#command-menu", CommandMenu)
        if menu.highlighted is None:
            return
        name, arguments, _, _ = menu.commands[menu.highlighted]
        prompt = self.query_one("#prompt", PromptInput)
        required = bool(arguments and not (arguments.startswith("[") and arguments.endswith("]")))
        if insert_only or required:
            value = name + (" " if arguments else "")
            self._command_dismissed = value
            prompt.load_text(value)
            prompt.move_cursor((0, len(value)))
            self._hide_commands()
        elif self._busy:
            self.notify("本轮仍在处理，Esc 停止后可执行命令。")
        else:
            self._hide_commands()
            prompt.load_text("")
            self._submit_value(name)
        if len(self.screen_stack) == 1:
            prompt.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option_list.id == "command-menu":
            event.stop()
            self._choose_command()

    def begin_approval(self, message):
        self._hide_commands()
        if len(self.screen_stack) > 1:
            self.pop_screen()
        self.append_line(message, "event")
        self._approval = True
        prompt = self.query_one("#prompt", PromptInput)
        self._approval_draft = prompt.text
        prompt.load_text("")
        prompt.placeholder = "approve、reject 或 cancel"
        prompt.focus()
        self._refresh_status()

    def end_approval(self):
        if self._approval:
            self.query_one("#prompt", PromptInput).load_text(self._approval_draft)
            self._approval_draft = ""
        self._approval = False
        if self._busy:
            self.query_one("#prompt", PromptInput).placeholder = "可先写下一条问题，Esc 停止本轮…"
        self._refresh_status()

    def _lock(self):
        self._busy = True
        self._busy_since = time.monotonic()
        self._activity = ""
        self._approval = False
        self._cancel_requested = False
        self.query_one("#prompt", PromptInput).placeholder = "可先写下一条问题，Esc 停止本轮…"
        self._refresh_status()

    def _unlock(self):
        self._busy = False
        self._busy_since = None
        self._activity = ""
        self._cancel_requested = False
        self.end_approval()
        while True:
            try:
                self.approval_queue.get_nowait()
            except queue.Empty:
                break
        prompt = self.query_one("#prompt", PromptInput)
        prompt.placeholder = "界面演示，不发送请求" if self.demo else "输入问题…"
        if len(self.screen_stack) == 1:
            prompt.focus()
        self._refresh_status()

    def _submit_value(self, value):
        head, _, argument = value.strip().partition(" ")
        if not self._approval and head.lower() in {"/help", "/config"}:
            if head.lower() == "/help":
                self._show_commands(argument or "common")
            else:
                self.action_configure_model()
            return
        if not self._approval and head.lower() in {"/model", "/models"} and not argument:
            self.action_choose_model()
            return
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
                self.notify("本轮仍在处理，Esc 停止后可发送；草稿已保留。")
            return
        if not value.strip():
            return
        # 在调度工作线程前分配本轮取消标记，Esc 即使立即到达也不会被清除。
        self.driver.cancel_event = threading.Event()
        self._follow_latest(force=True)
        if not value.lstrip().startswith("/"):
            self.append_line(value, "user")
        self._lock()
        self.run_worker(self._worker_line(value), name="turn", group="agent", exit_on_error=False)

    async def _worker_line(self, line):
        action = None
        try:
            action = await asyncio.to_thread(self.driver.handle_line, line, cancel_event=self.driver.cancel_event)
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
        prompt = self.query_one("#prompt", PromptInput)
        value = prompt.text
        if not value.strip():
            return
        if self._busy and not self._approval and not self.demo:
            if value:
                self.notify("本轮仍在处理，Esc 停止后可发送；草稿已保留。")
            return
        prompt.load_text("")
        self._submit_value(value)
        if len(self.screen_stack) == 1:
            prompt.focus()

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted):
        await self._submit()

    async def on_glyph_button_pressed(self, event: GlyphButton.Pressed):
        if event.action == "send":
            if self._busy:
                self.action_cancel_turn()
            else:
                await self._submit()
        elif event.action == "latest":
            self.action_jump_latest()
        if len(self.screen_stack) == 1:
            self.query_one("#prompt", PromptInput).focus()

    async def action_clear(self):
        await self.clear_transcript_sync()
        self.query_one("#prompt", PromptInput).focus()

    def action_show_status(self):
        if isinstance(self.screen, StatusScreen):
            self.pop_screen()
        else:
            self.push_screen(StatusScreen(self._status_details()))

    def action_show_help(self):
        if self.query_one("#command-menu").display or self.query_one("#command-help").display:
            self._hide_commands()
        else:
            self._show_commands()

    def _settings_available(self):
        if self.demo:
            self.notify("演示模式不修改本机配置；正常启动后按 F2 / F3。")
            return False
        if self._busy:
            self.notify("本轮运行中，结束后可切换模型；Esc 停止本轮。")
            return False
        return len(self.screen_stack) == 1

    def action_choose_model(self):
        if self._settings_available():
            self.push_screen(ModelPickerScreen(self._selected_profile().get("name")), self._model_picked)

    def _model_picked(self, result):
        if not result:
            return
        action, name = result
        if action == "edit":
            self._open_config(name)
        else:
            profile = next((p for p in list_model_profiles() if p["name"] == name), {})
            if not profile.get("configured"):
                self._open_config(name)
            else:
                self._submit_value("/model " + name)

    def action_configure_model(self):
        if self._settings_available():
            self._open_config(self._selected_profile().get("name"))

    def _open_config(self, name):
        try:
            profiles = list_model_profiles()
        except ConfigError as error:
            self.notify(str(error), severity="error")
            return
        profile = next((p for p in profiles if p["name"] == name), None)
        self.push_screen(ModelConfigScreen(profile), self._config_saved)

    def _config_saved(self, name):
        if name:
            self._profiles = list_model_profiles()
            self._submit_value("/model " + name)

    def action_jump_latest(self):
        self._follow_latest(force=True)

    def action_history_up(self):
        self.query_one("#conversation", ConversationView).scroll_page_up(animate=False)

    def action_history_down(self):
        self.query_one("#conversation", ConversationView).scroll_page_down(animate=False)

    def action_cancel_turn(self):
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        if self.query_one("#command-menu").display or self.query_one("#command-help").display:
            self._command_dismissed = self.query_one("#prompt", PromptInput).text
            self._hide_commands()
            return
        if self._busy and self.driver is not None and not self._cancel_requested:
            self._cancel_requested = True
            self.driver.cancel_turn()
            self._refresh_status()

    def action_quit(self):
        if self.driver is not None and self._busy:
            self._cancel_requested = True
            self.driver.request_stop()
            self._refresh_status()
            return
        if self.driver is not None:
            self.driver.save_if_needed()
        self.exit()
