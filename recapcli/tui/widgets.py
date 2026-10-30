"""聊天输入、历史跟随与按需显示的状态详情。"""

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class PromptInput(TextArea):
    """完整保留粘贴内容；输入高度随实际换行增长。"""

    class Submitted(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(soft_wrap=True, tab_behavior="focus", compact=True,
                         highlight_cursor_line=False, **kwargs)

    async def _on_key(self, event: events.Key):
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
        elif event.key in {"shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
        else:
            await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed):
        if event.text_area is self:
            self.fit_height()

    def on_resize(self):
        self.call_after_refresh(self.fit_height)

    def fit_height(self):
        limit = min(6, max(2, (self.app.size.height - 6) // 3))
        height = max(1, min(limit, self.wrapped_document.height))
        if self.styles.height is None or self.styles.height.value != height:
            self.styles.height = height


class ConversationView(VerticalScroll):
    """用户上翻时暂停跟随，手动回到末尾时恢复。"""

    class FollowingChanged(Message):
        pass

    def __init__(self, **kwargs):
        self.following = True
        super().__init__(**kwargs)

    def _set_following(self, value):
        if self.following != value:
            self.following = value
            self.post_message(self.FollowingChanged())

    def release_anchor(self):
        super().release_anchor()
        if self.max_scroll_y > 0:
            self._set_following(False)
            self.call_after_refresh(self._check_following)

    def _check_following(self):
        if self.is_vertical_scroll_end and self.scroll_target_y >= self.max_scroll_y:
            self._set_following(True)

    def watch_scroll_y(self, old_value, new_value):
        super().watch_scroll_y(old_value, new_value)
        self._check_following()

    def follow_latest(self):
        self._set_following(True)
        self.anchor()
        self.scroll_end(animate=False)


class StatusScreen(ModalScreen):
    """状态按需查看，不占用常驻对话宽度。"""

    BINDINGS = [Binding("escape,ctrl+g", "dismiss", show=False)]

    def __init__(self, body):
        self.body = body
        super().__init__()

    def compose(self):
        with Vertical(id="status-dialog"):
            with Horizontal(id="status-heading"):
                yield Static("会话详情", id="status-title")
                yield Button("关闭 · Esc", id="close-status", variant="default")
            with VerticalScroll(id="status-scroll"):
                yield Static(self.body, id="status-body", markup=False)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "close-status":
            event.stop()
            self.dismiss()
