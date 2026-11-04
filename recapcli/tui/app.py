"""流式对话、输入区命令补全与审批交互。"""

import asyncio
import os
from pathlib import Path
import queue
import threading
import time

from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Static

from .transcript import TranscriptMixin
from .status_bar import StatusBarMixin
from .command_input import CommandInputMixin
from .approval_panel import ApprovalPanelMixin
from .clipboard import ClipboardMixin
from .driver import Presenter, ShellDriver, ShellOptions
from .state import ShellState
from .status import TaskStatus
from .theme import PALETTE
from .widgets import ApprovalMenu, CommandMenu, ConversationView, GlyphButton, GradientLogo, PromptInput, StatusScreen
from .settings import ModelConfigScreen, ModelPickerScreen
from ..base.version import project_version
from ..ui.terminal_ui import clean_output
from ..config import ConfigError, list_model_profiles
from ..tools.approval import interpret_approval_line


# 跟随终端：背景和前景都用终端自己的颜色，只有强调色固定。
TERMINAL_THEME = Theme(
    name="recap-terminal", primary=PALETTE["accent"], secondary="ansi_bright_black",
    accent=PALETTE["accent"], foreground="ansi_default", background="ansi_default",
    surface="ansi_default", panel="ansi_default", success="ansi_green",
    warning="ansi_yellow", error="ansi_red", boost="ansi_default",
    dark=True, ansi=True,
    variables={
        "ansi-background": "ansi_default", "ansi-foreground": "ansi_default",
        "border": "ansi_bright_black", "border-blurred": "ansi_bright_black",
        "block-cursor-foreground": "ansi_default", "block-cursor-background": "ansi_default",
        "block-cursor-text-style": "bold reverse", "block-hover-background": "ansi_default",
        "input-cursor-background": PALETTE["accent"], "input-cursor-foreground": "ansi_black",
        "input-cursor-text-style": "none",
        "input-selection-background": "ansi_bright_black", "input-selection-foreground": "ansi_default",
        "screen-selection-background": "ansi_bright_black", "screen-selection-foreground": "ansi_default",
        "scrollbar": "ansi_bright_black", "scrollbar-hover": "ansi_bright_black", "scrollbar-active": PALETTE["accent"],
        "scrollbar-background": "ansi_default", "scrollbar-background-hover": "ansi_default",
        "scrollbar-background-active": "ansi_default", "scrollbar-corner-color": "ansi_default",
        "footer-background": "ansi_default",
    },
)
DEMO_NOTE = "这是界面演示，输入未发送给模型。"
DEMO_PROMPT = "梳理项目入口，说明启动和执行流程。"
DEMO_TODO = "☑ 定位启动入口\n◐ 梳理会话调度\n☐ 说明 Agent 执行循环"
DEMO_REPLY = (
    "启动与执行分为三个部分：\n\n"
    "- **启动入口** `tui/launch.py` 选择终端界面或行式 CLI。\n"
    "- **会话调度** `tui/driver.py` 连接会话、任务记录与审批。\n"
    "- **任务执行** `core/agent.py` 负责模型请求、工具调用和收尾。\n\n"
    "> 编辑与命令执行由启动选项控制，需要时仍会请求审批。\n\n"
    "*界面演示：以上为示例内容，未调用工具或模型。*"
)
LOGO = """██████╗ ███████╗ ██████╗ █████╗ ██████╗
██╔══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗
██████╔╝█████╗  ██║     ███████║██████╔╝
██╔══██╗██╔══╝  ██║     ██╔══██║██╔═══╝
██║  ██║███████╗╚██████╗██║  ██║██║
╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝"""
MISSING_CONFIG = "还没有可用的模型配置。按 F3 填写模型、接口地址和 API Key；F2 可选择已有配置。"


def version_label():
    """版本读取项目元数据，避免在界面里维护第二份版本号。"""
    release = project_version()
    return "v" + clean_output(release) if release else "开发版"




class RecapShell(TranscriptMixin, StatusBarMixin, CommandInputMixin, ApprovalPanelMixin, ClipboardMixin, App):
    """终端壳。--demo 只演示界面；其余输入走现有 run_agent。"""

    CSS_PATH = "recap.tcss"
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
            **{f"markdown.h{level}": "bold" for level in range(1, 7)},
            "markdown.code": "bold #ddb6f2 on #26233a",
            "markdown.block_quote": PALETTE["muted"],
            "markdown.item.bullet": PALETTE["muted"],
            "markdown.item.number": PALETTE["muted"],
            "markdown.link": "underline " + PALETTE["accent"],
            "markdown.link_url": PALETTE["muted"],
        }))
        self.state = state
        self.demo = demo
        self.options = options or ShellOptions(workspace=state.workspace)
        self.client_factory = client_factory
        self._busy = False
        self._busy_since = None
        self._activity = ""
        self._approval = False
        self._approval_draft = ""
        self._approval_since = 0.0
        self._status_phase = 0.0
        self._status_message = ""
        self._mention_files = None
        self._mention_loaded = 0.0
        self._cancel_requested = False
        self._has_new_content = False
        self._response_widget = None
        self._turn_answer_widget = None
        self._progress_widget = None
        self._todo_widget = None
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
                with ConversationView(id="conversation"):
                    with Vertical(id="history"):
                        with Vertical(id="welcome"):
                            with Horizontal(id="welcome-columns"):
                                with Vertical(id="welcome-left"):
                                    yield Static("欢迎回来", id="welcome-title", markup=False)
                                    yield GradientLogo(LOGO, id="welcome-logo")
                                    yield Static("", id="welcome-agent", markup=False)
                                    yield Static(self._workspace_caption(), id="welcome-subtitle", markup=False)
                                with Vertical(id="welcome-right"):
                                    yield Static(self._welcome_tips(), id="welcome-tips", markup=False)
                        yield Vertical(id="transcript")
                with Vertical(id="footer"):
                    yield CommandMenu(id="command-menu")
                    yield ApprovalMenu(id="approval-menu")
                    yield Static("", id="command-help", markup=False)
                    yield Static("", id="command-hint", markup=False)
                    with Horizontal(id="status-row"):
                        yield Static("", id="status-line", markup=False)
                        yield GlyphButton("↓ 回到底部", "latest", "回到底部并恢复自动跟随（Ctrl+End）", id="jump-latest")
                    with Horizontal(id="composer"):
                        yield Static("RecapCLI ❯", id="input-prefix", markup=False)
                        placeholder = "界面演示，不发送请求" if self.demo else "输入问题，/ 查看命令…"
                        yield PromptInput(placeholder=placeholder, id="prompt")
                        yield Static("↵", id="input-enter", markup=False)
                    yield Static("", id="key-hints", markup=False)
            yield Static("", id="task-inspector", markup=False)

    def _welcome_tips(self):
        text = Text()
        for title, lines in (("开始使用", ("直接说你想做什么，比如“修复登录报错”", "@ 引用文件 · / 查看命令 · F2 切换模型")),
                             ("当前项目", (f"已保存 {len(self.state.sessions)} 个会话", "/tasks 查看任务记录"))):
            if text:
                text.append("\n\n")
            text.append(title, style="bold " + PALETTE["accent"])
            for line in lines:
                text.append("\n" + line)
        return text

    async def on_mount(self):
        self.query_one("#welcome").border_title = "RecapCLI " + version_label()
        self.ui_thread = threading.get_ident()
        self.register_theme(TERMINAL_THEME)
        self.theme = "recap-terminal"
        self._apply_width(self.size.width)
        self.set_interval(1, self._tick_status)
        self.set_interval(0.1, self._flow_status)
        if self.demo:
            await self._append(DEMO_PROMPT, "user")
            for name in ("read_file · tui/launch.py", "read_file · tui/driver.py", "search_symbols · core/agent.py"):
                self.event_update("[工具] " + name)
                await asyncio.sleep(0.06)
                self.event_update("[结果] 成功")
            self.event_update("[待办]\n" + DEMO_TODO)
            await self._append(DEMO_REPLY, "assistant")
            for note in self.state.notes:
                await self._append(note, "info")
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
        self.query_one("#task-inspector").display = width >= 100
        self.query_one("#welcome-logo").display = width >= 52
        if self.screen_stack:
            self.screen_stack[0].set_class(width < 72, "-narrow")
        self.call_after_refresh(self._refresh_hints)

    def _workspace_caption(self):
        path = clean_output(str(self.state.workspace))
        try:
            home = clean_output(str(Path.home()))
        except RuntimeError:  # Windows 缺少 USERPROFILE 时取不到主目录；只是少缩写，不能让界面起不来。
            home = None
        if home and (path.startswith(home + os.sep) or path == home):
            path = "~" + path[len(home):]
        caption = Text(no_wrap=True, overflow="ellipsis")
        caption.append(path)
        if self.state.branch:
            caption.append(" · " + clean_output(self.state.branch))
        return caption


    def _lock(self):
        self._turn_answer_widget = None
        self._task_status = TaskStatus()
        self._progress_widget = None
        self._todo_widget = None
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
                if interpret_approval_line(value) is not None:
                    self.end_approval()
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
        if not self._approval:
            prompt.remember(value)
        prompt.load_text("")
        self._submit_value(value)
        if len(self.screen_stack) == 1:
            prompt.focus()

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted):
        await self._submit()

    async def on_glyph_button_pressed(self, event: GlyphButton.Pressed):
        if event.action == "latest":
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
