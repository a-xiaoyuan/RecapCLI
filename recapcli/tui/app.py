"""极简终端预览：炭黑底、等宽文字、薰衣草提示符和一条输入线。

沿用只读 ShellState；不接管 Agent、工具、审批或会话写入。
"""

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Input, Static

from .state import ShellState
from .theme import PALETTE, dot, spaced


VIOLET_STUDIO = Theme(
    name="violet-studio", primary=PALETTE["lavender"], secondary=PALETTE["lavender"],
    accent=PALETTE["lavender"], foreground=PALETTE["text"], background=PALETTE["base"],
    surface=PALETTE["base"], panel=PALETTE["stage"], success=PALETTE["online"],
    error=PALETTE["error"], dark=True,
)
PREVIEW_NOTE = "这是界面预览，输入未发送给模型。正式对话请运行 uv run python -m recapcli。"
DEMO_PROMPT = "Can you summarize the key architectural decisions in the last session?"
DEMO_REPLY = (
    "Yes. In the last session we focused on three key decisions:\n\n"
    "  - Moving to a flat module structure to reduce indirection.\n\n"
    "  - Replacing the custom loader with an explicit registry pattern.\n\n"
    "  - Treating configuration as code, with validation at parse time."
)


def model_label(state, demo=False):
    profile = state.active_profile or {}
    name = "gpt-4o-mini · preview" if demo else (profile.get("model") or "未配置模型")
    text = Text(name + "  ", style=PALETTE["muted"], no_wrap=True, overflow="ellipsis")
    text.append_text(dot(True if demo else state.ready))
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
    """只读预览壳；demo 内容固定，不产生模型请求。"""

    CSS_PATH = "violet.tcss"
    TITLE = "RecapCLI"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+b", "toggle_rail", "图标轨"), Binding("ctrl+l", "clear", "清屏"),
                Binding("ctrl+q", "quit", "退出", priority=True)]

    def __init__(self, state: ShellState, *, demo=False):
        super().__init__()
        self.state = state
        self.demo = demo
        self._cursor_visible = True

    def compose(self) -> ComposeResult:
        with Horizontal(id="shell"):
            with Vertical(id="rail"):
                for glyph, action, tooltip in (("~", "home", "回到对话"), ("+", "new", "新预览对话"),
                                                ("✓", "tasks", "任务入口"), ("◷", "history", "已保存会话"),
                                                ("…", "help", "快捷键与预览说明")):
                    yield GlyphButton(glyph, action, tooltip, id=f"rail-{action}", classes="rail-button")
            with Vertical(id="stage"):
                with Horizontal(id="topbar"):
                    brand = Static("RECAPCLI", id="brand", markup=False)
                    brand.tooltip = str(self.state.workspace)
                    yield brand
                    yield Static(Text(spaced("VIOLET STUDIO"), style=PALETTE["faint"]), id="studio")
                    status = Static(model_label(self.state, self.demo), id="model")
                    status.tooltip = "本地配置就绪，不代表接口已探测；preview 为固定演示"
                    yield status
                with VerticalScroll(id="conversation"):
                    yield Vertical(id="transcript")
                with Horizontal(id="composer"):
                    yield Static(">", id="input-prefix", markup=False)
                    yield Input(placeholder="输入问题…（界面预览，不发送请求）", id="prompt")
                    yield GlyphButton("➤", "send", "发送预览（Enter）", id="send")

    async def on_mount(self):
        self.register_theme(VIOLET_STUDIO)
        self.theme = "violet-studio"
        self._apply_width(self.size.width)
        if self.demo:
            await self._append(DEMO_PROMPT)
            await self._append(DEMO_REPLY, assistant=True)
            await self.query_one("#transcript").mount(Static(self._cursor_text(), id="stream-cursor"))
            self.set_interval(0.6, self._blink_cursor)
        else:
            await self._append("界面预览。输入仅在本地展示；Ctrl+Q 退出。", assistant=True)
        for note in self.state.notes:
            await self._append(note, assistant=True)
        self.query_one("#prompt", Input).focus()

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

    async def _append(self, content, assistant=False):
        text = Text("> ", style=PALETTE["lavender"] if assistant else PALETTE["muted"])
        text.append(content, style=PALETTE["text"])
        await self.query_one("#transcript").mount(Static(text, classes="turn", markup=False))
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    async def _submit(self):
        prompt = self.query_one("#prompt", Input)
        value = prompt.value.strip()
        prompt.value = ""
        if not value:
            return
        if value in {"/exit", "/quit"}:
            self.exit()
        elif value == "/clear":
            await self.action_clear()
        else:
            await self._append(value)
            await self._append(PREVIEW_NOTE, assistant=True)
        prompt.focus()

    async def on_input_submitted(self, event: Input.Submitted):
        await self._submit()

    async def on_glyph_button_pressed(self, event: GlyphButton.Pressed):
        if event.action == "send":
            await self._submit()
        elif event.action == "new":
            await self.action_clear()
        elif event.action == "history":
            lines = [f"{item.get('id', '?')}  ·  {item.get('messages', 0)} 条消息" for item in self.state.sessions]
            await self._append("已保存会话\n" + ("\n".join(lines) or "暂无会话。"), assistant=True)
        elif event.action == "tasks":
            await self._append("任务记录请在正式 CLI 使用 /tasks 或 /task ID 查看。", assistant=True)
        elif event.action == "help":
            await self._append("Enter 发送预览 · Ctrl+L 清屏 · Ctrl+B 图标轨 · Ctrl+Q 退出。\n" + PREVIEW_NOTE, assistant=True)
        else:
            self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)
        self.query_one("#prompt", Input).focus()

    async def action_clear(self):
        await self.query_one("#transcript").remove_children()

    def action_toggle_rail(self):
        self.query_one("#rail").toggle_class("-hidden")
