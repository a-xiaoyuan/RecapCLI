"""流式对话、输入区命令补全与审批交互。"""

import asyncio
import os
import queue
import threading
import time

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
from .theme import PALETTE, render_answer, render_line
from .widgets import CommandMenu, ConversationView, PromptInput, StatusScreen, ToolProgress
from .settings import ModelConfigScreen, ModelPickerScreen
from ..ui.terminal_art import project_version, wordmark_text
from ..ui.terminal_ui import clean_output
from ..config import ConfigError, list_model_profiles
from ..application.help import command_items, help_text
from ..core.project_rules import project_rules_description
from ..application.changes import change_report, git_summary, files_summary, commands_summary


TERMINAL_THEME = Theme(
    name="recap-terminal", primary=PALETTE["accent"], secondary=PALETTE["warning"],
    accent=PALETTE["accent"], foreground=PALETTE["text"], background=PALETTE["base"],
    surface=PALETTE["stage"], panel=PALETTE["stage"], success=PALETTE["online"],
    warning=PALETTE["warning"], error=PALETTE["error"], boost=PALETTE["muted"],
    dark=False, ansi=False,
)
WELCOME_NOTE = "欢迎回来，从手头的代码读起。"
WELCOME_TIPS = (
    "/help 查看命令",
    "F2 选模型，F3 写配置",
    "Esc 停止，Ctrl+L 清屏",
    "写入和命令会先审批",
)
DEMO_NOTE = "这是界面演示，输入未发送给模型。"
DEMO_PROMPT = "梳理项目入口，说明启动和执行流程。"
DEMO_REPLY = (
    "启动与执行分为三个部分：\n\n"
    "- **启动入口** · `tui/launch.py` 选择终端或行式界面。\n"
    "- **会话调度** · `tui/driver.py` 连接会话、任务与审批。\n"
    "- **任务执行** · `core/agent.py` 负责模型请求和工具调用。\n\n"
    "> **权限提示**\n> 编辑与命令执行由启动选项控制，需要时仍会请求审批。\n\n"
    "接下来可以：\n\n"
    "1. 解释启动流程\n2. 查看当前模型配置\n3. 继续阅读 Agent 执行过程\n\n"
    "*界面演示：以上为示例内容，进度为演示事件，未调用工具或模型。*"
)
MISSING_CONFIG = "还没有可用的模型配置。按 F3 填写模型、接口地址和 API Key；F2 可选择已有配置。"


def _copy_windows_clipboard(text):
    """直接写入 Windows Unicode 剪贴板，兼容不支持 OSC 52 的 IDE 终端。"""
    import ctypes as ct
    from ctypes import wintypes as wt

    user = ct.WinDLL("user32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    user.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                    ct.c_int, ct.c_int, ct.c_int, ct.c_int,
                                    wt.HWND, wt.HMENU, wt.HINSTANCE, ct.c_void_p]
    user.CreateWindowExW.restype = wt.HWND
    user.DestroyWindow.argtypes, user.DestroyWindow.restype = [wt.HWND], wt.BOOL
    user.OpenClipboard.argtypes, user.OpenClipboard.restype = [wt.HWND], wt.BOOL
    user.CloseClipboard.argtypes, user.CloseClipboard.restype = [], wt.BOOL
    user.EmptyClipboard.argtypes, user.EmptyClipboard.restype = [], wt.BOOL
    user.SetClipboardData.argtypes, user.SetClipboardData.restype = [wt.UINT, wt.HANDLE], wt.HANDLE
    kernel.GlobalAlloc.argtypes, kernel.GlobalAlloc.restype = [wt.UINT, ct.c_size_t], wt.HANDLE
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [wt.HANDLE], ct.c_void_p
    kernel.GlobalUnlock.argtypes, kernel.GlobalUnlock.restype = [wt.HANDLE], wt.BOOL
    kernel.GlobalFree.argtypes, kernel.GlobalFree.restype = [wt.HANDLE], wt.HANDLE

    # OpenClipboard 的所有者需要有效窗口；IDE 终端不一定有控制台窗口。
    window = user.CreateWindowExW(0, "STATIC", "RecapCLI clipboard", 0, 0, 0, 0, 0,
                                 None, None, None, None)
    if not window:
        raise ct.WinError(ct.get_last_error())
    memory, opened = None, False
    try:
        data = (text + "\0").encode("utf-16-le")
        memory = kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not memory:
            raise ct.WinError(ct.get_last_error())
        pointer = kernel.GlobalLock(memory)
        if not pointer:
            raise ct.WinError(ct.get_last_error())
        ct.memmove(pointer, data, len(data))
        kernel.GlobalUnlock(memory)
        if not user.OpenClipboard(window):
            raise ct.WinError(ct.get_last_error())
        opened = True
        if not user.EmptyClipboard() or not user.SetClipboardData(13, memory):  # CF_UNICODETEXT
            raise ct.WinError(ct.get_last_error())
        memory = None  # 系统接管内存，成功后不能释放。
    finally:
        if opened:
            user.CloseClipboard()
        if memory:
            kernel.GlobalFree(memory)
        user.DestroyWindow(window)


def version_label():
    """版本读取项目元数据，避免在界面里维护第二份版本号。"""
    release = project_version()
    label = "v" + clean_output(release) if release else "开发版"
    return Text(label, style=PALETTE["muted"], no_wrap=True)


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
        self.task_status = None
        super().__init__("", classes=f"turn {kind}-turn", markup=False)
        self.set_content(content)

    def set_content(self, content, kind=None):
        if kind is not None:
            self.remove_class(f"{self.kind}-turn")
            self.kind = kind
            self.add_class(f"{kind}-turn")
        self.content_text = clean_output(content)
        shown = self.content_text
        if self.kind == "assistant":
            rendered = render_answer(shown, self.task_status)
        elif self.kind == "user":
            rendered = Text.assemble(("❯ ", "bold " + PALETTE["user"]),
                                     (shown, "bold " + PALETTE["text"]), overflow="fold")
        else:
            rendered = render_line(shown, self.kind)
        self.update(rendered)

    def set_task(self, task):
        self.task_status = task
        self.set_content(self.content_text)

    def get_selection(self, selection):
        if selection.start is None and selection.end is None:
            return self.content_text, "\n\n"
        return super().get_selection(selection)


class RecapShell(App):
    """终端壳。--demo 只演示界面；其余输入走现有 run_agent。"""

    CSS_PATH = "violet.tcss"
    TITLE = "RecapCLI"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("f1", "show_help", "帮助", priority=True),
                Binding("f2", "choose_model", "选择模型", priority=True),
                Binding("f3", "configure_model", "配置模型", priority=True),
                Binding("ctrl+l", "clear", "清屏"),
                Binding("ctrl+c", "copy", "复制", show=False),
                Binding("ctrl+g", "show_status", "详情", priority=True),
                Binding("escape", "cancel_turn", "停止本轮", priority=True),
                Binding("ctrl+end", "jump_latest", "回到底部", priority=True),
                Binding("alt+pageup", "history_up", "上翻历史", priority=True),
                Binding("alt+pagedown", "history_down", "下翻历史", priority=True),
                Binding("ctrl+q", "quit", "退出", priority=True)]

    def __init__(self, state: ShellState, *, demo=False, options: ShellOptions | None = None, client_factory=None):
        super().__init__(ansi_color=True)
        self.console.push_theme(RichTheme({
            **{f"markdown.h{level}": "bold " + PALETTE["text"] for level in range(1, 7)},
            "markdown.code": "bold " + PALETTE["text"] + " on #f4e6d8",
            "markdown.block_quote": PALETTE["text"],
            "markdown.item.bullet": PALETTE["muted"],
            "markdown.item.number": PALETTE["accent"],
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
        self._turn_answer_widget = None
        self._progress_widget = None
        self._status_message = ""
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
                yield Static("", id="task-inspector", markup=False)
                with ConversationView(id="conversation"):
                    with Vertical(id="history"):
                        with Vertical(id="welcome"):
                            with Horizontal(id="welcome-columns"):
                                with Vertical(id="welcome-primary"):
                                    yield Static(wordmark_text(max_width=40), id="welcome-mark")
                                    yield Static(WELCOME_NOTE, id="welcome-message", markup=False)
                                    yield Static(self._workspace_caption(), id="welcome-subtitle", markup=False)
                                    yield Static("", id="welcome-agent", markup=False)
                                with Vertical(id="welcome-tips"):
                                    yield Static("Tips", id="welcome-tips-title", markup=False)
                                    yield Static("\n".join(WELCOME_TIPS), id="welcome-tips-body", markup=False)
                        yield Vertical(id="transcript")
                with Horizontal(id="footer-frame"):
                    with Vertical(id="footer"):
                        yield CommandMenu(id="command-menu")
                        yield Static("", id="command-help", markup=False)
                        yield Static("", id="command-hint", markup=False)
                        with Horizontal(id="status-row"):
                            yield Static("", id="status-line", markup=False)
                            yield GlyphButton("↓ 回到底部", "latest", "回到底部并恢复自动跟随（Ctrl+End）", id="jump-latest")
                        with Horizontal(id="composer"):
                            yield Static("RecapCLI ❯", id="input-prefix", markup=False)
                            placeholder = "界面演示，不发送请求" if self.demo else "输入问题，/ 查看命令…"
                            yield PromptInput(placeholder=placeholder, id="prompt")
                            yield GlyphButton("↵", "send", "发送（Enter）", id="send")
                        yield Static("", id="key-hints", markup=False)

    async def on_mount(self):
        self.ui_thread = threading.get_ident()
        self.register_theme(TERMINAL_THEME)
        self.theme = "recap-terminal"
        self.query_one("#welcome").border_title = Text.assemble(
            ("◆ RecapCLI ", "bold " + PALETTE["accent"]), version_label())
        self._apply_width(self.size.width)
        self.set_interval(1, self._tick_status)
        if self.demo:
            await self._append(DEMO_PROMPT, "user")
            for name in ("read_file · tui/launch.py", "read_file · tui/driver.py", "search_symbols · core/agent.py"):
                self.event_update("[工具] " + name)
                await asyncio.sleep(0.06)
                self.event_update("[结果] 成功")
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
        if self.demo:
            self.call_after_refresh(self.query_one("#conversation", ConversationView).scroll_home, animate=False)
        self._refresh_status()

    def on_unmount(self):
        if self.driver is not None:
            self.driver.close()

    def on_resize(self, event):
        self._apply_width(event.size.width)

    def _apply_width(self, width):
        if self.screen_stack:
            self.screen_stack[0].set_class(width < 72, "-narrow")
            self.screen_stack[0].set_class(width >= 128, "-wide")
        if self.query("#input-prefix"):
            self.query_one("#input-prefix", Static).update("❯" if width < 72 else "RecapCLI ❯")
        self.call_after_refresh(self._refresh_wordmark)
        self.call_after_refresh(self._refresh_hints)

    def _workspace_caption(self):
        """目录和分支留在左栏副文；模型已经出现在按键提示里。"""
        path = clean_output(str(self.state.workspace))
        branch = clean_output(self.state.branch) or "无 Git 分支"
        caption = Text(no_wrap=True, overflow="ellipsis")
        caption.append(path, style=PALETTE["muted"])
        caption.append("  ·  ", style=PALETTE["faint"])
        caption.append(branch, style=PALETTE["online"] if self.state.branch else PALETTE["muted"])
        return caption

    def _refresh_wordmark(self):
        if not self.query("#welcome-mark"):
            return
        primary = self.query_one("#welcome-primary")
        width = primary.content_size.width or primary.size.width
        if width <= 1 or width == getattr(self, "_mark_width", None):
            return
        self._mark_width = width
        self.query_one("#welcome-mark", Static).update(wordmark_text(max_width=width))

    def _cursor_text(self):
        text = Text("  ", style=PALETTE["faint"])
        text.append("█" if self._cursor_visible else " ", style=PALETTE["online"])
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
            self.query_one("#status-row").display = bool(self._status_message) or not following

    def on_conversation_view_following_changed(self):
        self._refresh_navigation()

    async def _append(self, content, kind="user"):
        await self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest(force=True)

    def append_line(self, content, kind):
        self.query_one("#transcript").mount(self._static(content, kind))
        self._follow_latest(force=kind == "user")

    def event_update(self, message):
        message = clean_output(message)
        prefix, separator, _ = message.partition("] ")
        name = prefix.removeprefix("[")
        role = name if separator and (name in {"analysis", "review", "aggregate", "planner", "reviewer", "team_summary"}
                                     or name.startswith("worker:")) else ""
        event = message.removeprefix(f"[{role}] ") if role else message
        if event.startswith("[模型]"):
            self.set_activity("请求模型 · " + event.removeprefix("[模型]").strip())
            model = self._display_model or self._selected_profile().get("model") or "当前模型"
            message = (f"[{role}] " if role else "") + "[模型] 请求模型 · " + clean_output(model)
        if event.startswith("[工具]"):
            self.set_activity("工具 · " + event.removeprefix("[工具]").strip())
        if event.startswith(("[模型]", "[工具]")) and self._progress_widget is None:
            self._progress_widget = ToolProgress()
            self.query_one("#transcript").mount(self._progress_widget)
        if self._progress_widget is not None and self._progress_widget.observe(message, role):
            self._follow_latest()
        else:
            self.append_line(message, "event")

    def stream_update(self, kind, text):
        if kind == "start":
            self._response_widget = None
            self._response_text = ""
        elif kind == "delta":
            self._response_text += text
            if self._response_widget is None:
                self._response_widget = self._static(self._response_text, "assistant")
                self._turn_answer_widget = self._response_widget
                self._response_widget.set_task(self._task_status)
                self.query_one("#transcript").mount(self._response_widget)
            else:
                self._response_widget.set_content(self._response_text)
            self._follow_latest()
        elif kind == "end":
            if self._progress_widget is not None:
                self._progress_widget.complete_request()
        elif kind == "abort":
            if self._response_widget is not None:
                self._response_widget.set_content("[回答未完成]\n" + self._response_text, "error")
            self._response_widget = None
            self._response_text = ""

    def finish_answer(self, answer):
        if self._progress_widget is not None:
            for role in {row["role"] for row in self._progress_widget.rows}:
                self._progress_widget.complete_request(role)
        if self._response_widget is not None:
            self._response_widget.set_content(answer)
            self._turn_answer_widget = self._response_widget
        else:
            self._turn_answer_widget = self._static(answer, "assistant")
            self.query_one("#transcript").mount(self._turn_answer_widget)
        self._turn_answer_widget.set_task(self._task_status)
        self._follow_latest()
        self._response_widget = None
        self._response_text = ""

    async def clear_transcript_sync(self):
        self._response_widget = None
        self._turn_answer_widget = None
        self._response_text = ""
        self._progress_widget = None
        await self.query_one("#transcript").remove_children()
        self._follow_latest(force=True)

    async def replace_transcript_sync(self, rows):
        self._task_status = TaskStatus()
        self._refresh_status()
        self._response_widget = None
        self._turn_answer_widget = None
        self._response_text = ""
        self._progress_widget = None
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
        if self._turn_answer_widget is not None:
            self._turn_answer_widget.set_task(self._task_status)
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
            label = ""
        elif not self._busy:
            label = ("模型请求超时，详情见上方提示" if "APITimeoutError" in self._task_status.stop_reason else
                     ("任务未完成，详情见上方提示" if self._task_status.status in {"failed", "blocked", "unknown"} else ""))
        color = PALETTE["error"] if self._task_status.status == "failed" and not self._busy else (PALETTE["accent"] if self._busy else PALETTE["faint"])
        self.query_one("#status-line", Static).update(Text(label, style=color, no_wrap=True, overflow="ellipsis"))
        self._status_message = label
        self._refresh_navigation()
        mode = " / ".join({"read": "读取", "write": "编辑", "exec": "命令", "web": "网页"}[item]
                          for item in self.options.capabilities)
        agent = Text()
        agent.append("● ", style=PALETTE["online"] if self._model_ready else PALETTE["faint"])
        agent.append("界面演示" if self.demo else ("Agent 待配置" if not self._model_ready else
                     ("Agent 执行中" if self._busy else "Agent Ready")), style=PALETTE["text"])
        agent.append(" · " + mode, style=PALETTE["muted"])
        self.query_one("#welcome-agent", Static).update(agent)
        self._refresh_inspector(mode)
        send = self.query_one("#send", GlyphButton)
        send.update("■" if self._busy else "↵")
        send.tooltip = "停止本轮（Esc）" if self._busy else "发送（Enter）"
        self._refresh_hints()
        if isinstance(self.screen, StatusScreen) and self.screen.query("#status-body"):
            self.screen.query_one("#status-body", Static).update(self._status_details())

    def _refresh_inspector(self, mode):
        task = self._task_status
        files = list(task.modified_files)
        file_text = "\n".join(files[:6]) if files else "暂无本轮编辑"
        if len(files) > 6:
            file_text += f"\n还有 {len(files) - 6} 个 · Ctrl+G 查看"
        requests = task.requests
        if self._progress_widget is not None:
            requests = max(requests, sum(row["kind"] == "request" for row in self._progress_widget.rows))
        body = Text()
        for title, content in (
            ("本轮任务", task_text(task, busy=self._busy, approval=self._approval)),
            ("执行模式", self.options.mode + " · " + mode),
            ("模型请求", f"{requests} 次" if task.task_id or requests else "尚未开始"),
            ("Token 用量", usage_text(task) if task.task_id else "尚无任务用量"),
            ("本轮编辑", file_text),
        ):
            body.append(title + "\n", style="bold " + PALETTE["accent"])
            body.append(clean_output(content) + "\n\n", style=PALETTE["muted"])
        body.append("Ctrl+G 查看完整详情", style=PALETTE["faint"])
        self.query_one("#task-inspector", Static).update(body)

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
            hint = "Enter 发送 · Shift+Enter 换行 · /help 命令" if self._model_ready else "/config 设置 · /help 命令"
        model = self._display_model or self._selected_profile().get("model") or "未配置模型"
        prefix = clean_output(model) + " · " if self.size.width >= 100 else ""
        if self.size.width < 72 and not self._busy and not self._approval:
            hint = "Enter 发送 · /help 命令" if self._model_ready else "/config 设置 · /help 命令"
        label = Text(prefix + hint, style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
        self.query_one("#key-hints", Static).update(label)

    def _hide_commands(self):
        self._command_anchor = None
        for selector in ("#command-menu", "#command-help", "#command-hint"):
            self.query_one(selector).display = False
        self.query_one("#prompt", PromptInput).suggestion = ""

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
        self.query_one("#prompt", PromptInput).update_suggestion()

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
            self.query_one("#prompt", PromptInput).update_suggestion()
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
        self._turn_answer_widget = None
        self._task_status = TaskStatus()
        self._progress_widget = None
        self._busy = True
        self._busy_since = time.monotonic()
        self._activity = ""
        self._approval = False
        self._cancel_requested = False
        self.query_one("#prompt", PromptInput).placeholder = "可先写下一条问题，Esc 停止本轮…"
        self._refresh_status()

    def _unlock(self):
        if self._progress_widget is not None:
            self._progress_widget.finish()
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
        prompt.placeholder = "界面演示，不发送请求" if self.demo else "输入问题，/ 查看命令…"
        if len(self.screen_stack) == 1:
            prompt.focus()
        self._refresh_status()

    def _submit_value(self, value):
        head, _, argument = value.strip().partition(" ")
        if not self._approval and head.lower() == "/copy":
            if argument.strip() not in {"", "all"}:
                self.notify("用法：/copy 复制最近回答；/copy all 复制完整对话。", severity="warning")
            else:
                self.action_copy_reply(all_messages=argument.strip() == "all")
            return
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

    def copy_to_clipboard(self, text):
        if os.name != "nt":
            super().copy_to_clipboard(text)
            return True
        try:
            _copy_windows_clipboard(text)
        except OSError as error:
            self.notify(f"复制失败：无法写入系统剪贴板（{error}）。", severity="error")
            return False
        self._clipboard = text
        return True

    def action_copy_reply(self, *, all_messages=False):
        rows = [row for row in self.query(ConversationLine)
                if row.kind in {"user", "assistant"} and row.content_text]
        if all_messages:
            text = "\n\n".join(("用户" if row.kind == "user" else "RecapCLI") + "\n" + row.content_text
                               for row in rows)
        else:
            text = next((row.content_text for row in reversed(rows) if row.kind == "assistant"), "")
        if not text:
            self.notify("暂无可复制的对话。")
            return
        if self.copy_to_clipboard(text):
            self.notify("已复制完整对话。" if all_messages else "已复制完整回答。")

    def action_copy(self):
        self.action_copy_reply()

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
