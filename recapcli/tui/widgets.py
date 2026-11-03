"""聊天输入、工具进度、历史跟随与按需显示的状态详情。"""

import time

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from rich.text import Text
from rich.segment import Segment
from rich.tree import Tree
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .theme import PALETTE


class PromptInput(TextArea):
    """完整保留粘贴内容；输入高度随实际换行增长。"""

    BINDINGS = [Binding("ctrl+a", "select_all", "全选", show=False)]

    class Submitted(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(soft_wrap=True, tab_behavior="focus", compact=True,
                         highlight_cursor_line=False, **kwargs)

    async def _on_key(self, event: events.Key):
        if self.app.command_menu_key(event):
            event.stop()
            event.prevent_default()
        elif event.key == "enter":
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
            if self.text:
                self.screen.clear_selection()
            self.fit_height()

    def action_select_all(self):
        if self.text:
            self.screen.clear_selection()
            super().action_select_all()
        else:
            self.app.query_one("#transcript").text_select_all()

    def on_resize(self):
        self.call_after_refresh(self.fit_height)

    def fit_height(self):
        limit = min(6, max(2, (self.app.size.height - 6) // 3))
        height = max(1, min(limit, self.wrapped_document.height))
        if self.styles.height is None or self.styles.height.value != height:
            self.styles.height = height

    def update_suggestion(self):
        self.suggestion = ""
        if not self.is_mounted or self.selection.start != self.selection.end:
            return
        value = self.text
        if not value.startswith("/") or self.cursor_location != (0, len(value)):
            return
        menu = self.app.query_one("#command-menu", CommandMenu)
        if menu.display and menu.highlighted is not None:
            name = menu.commands[menu.highlighted][0]
            if name.startswith(value):
                self.suggestion = name[len(value):]


class CommandMenu(OptionList):
    """与输入共用焦点的命令候选，不遮盖整个终端。"""

    can_focus = False

    def __init__(self, **kwargs):
        super().__init__(markup=False, **kwargs)
        self.commands = []
        self.display = False

    def show_commands(self, commands, max_rows):
        self.commands = commands
        self.clear_options()
        for name, arguments, description, _ in commands:
            label = Text(no_wrap=True, overflow="ellipsis")
            label.append(name, style="bold " + PALETTE["accent"])
            if arguments:
                label.append(" " + arguments, style=PALETTE["muted"])
            label.append("  ·  " + description, style=PALETTE["text"])
            self.add_option(Option(label, id=name))
        self.styles.height = min(len(commands), max_rows)
        self.display = bool(commands)
        if commands:
            self.highlighted = 0


class CompactTree(Tree):
    """不显示空的根节点行，同时保留子节点的连接线。"""

    def __rich_console__(self, console, options):
        for index, line in enumerate(Segment.split_lines(super().__rich_console__(console, options))):
            if index:
                yield from line
                yield Segment.line()


class ToolProgress(Static):
    """同一轮的工具事件组成一棵树；未知结果不标为成功。"""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    INDEX_TOOLS = {"search_code_index", "search_symbols"}

    def __init__(self):
        super().__init__("", classes="turn progress-turn", markup=False)
        self.rows = []
        self.content_text = ""
        self._frame = 0
        self._timer = None

    def on_mount(self):
        self._timer = self.set_interval(0.12, self._animate)
        self._redraw()

    def observe(self, message, role=""):
        event = message.removeprefix(f"[{role}] ") if role else message
        if event.startswith("[模型]"):
            self.complete_request(role)
            self.rows.append({"label": event.removeprefix("[模型]").strip(), "role": role,
                              "kind": "request", "state": "running", "started": time.monotonic(),
                              "duration": None, "detail": ""})
        elif event.startswith("[工具]"):
            self.complete_request(role)
            self.rows.append({"label": event.removeprefix("[工具]").strip(), "role": role,
                              "kind": "tool", "state": "running", "started": time.monotonic(), "duration": None,
                              "detail": ""})
        elif event.startswith("[结果]"):
            row = next((item for item in self.rows
                        if item["role"] == role and item["kind"] == "tool" and item["state"] == "running"), None)
            if row is None:
                return False
            result = event.removeprefix("[结果]").strip()
            state = "failed" if result.startswith("失败") else "done"
            if "退出码 " in result and result.rsplit("退出码 ", 1)[1] != "0":
                state = "warning"
            row.update(state=state, duration=time.monotonic() - row["started"],
                       detail="" if result == "成功" else result)
        else:
            return False
        self.content_text += (self.rows[-1]["label"] if event.startswith("[模型]") else message) + "\n"
        self._redraw()
        return True

    def complete_request(self, role=""):
        for row in self.rows:
            if row["kind"] == "request" and row["role"] == role and row["state"] == "running":
                row.update(state="done", duration=time.monotonic() - row["started"])
        self._redraw()

    def finish(self):
        for row in self.rows:
            if row["state"] == "running":
                row.update(state="unknown", duration=time.monotonic() - row["started"], detail="结果待核对")
        self._redraw()

    def _animate(self):
        self._frame = (self._frame + 1) % len(self.FRAMES)
        self._redraw()

    def _redraw(self):
        running = any(row["state"] == "running" for row in self.rows)
        if self._timer is not None:
            self._timer.resume() if running else self._timer.pause()
        if not self.rows:
            self.update("")
            return
        requests = sum(row["kind"] == "request" for row in self.rows)
        tools = sum(row["kind"] == "tool" for row in self.rows)
        reads = sum(row["label"].split(" ", 1)[0] in {"read_file", "find_files", "list_directory"}
                    for row in self.rows if row["kind"] == "tool")
        searches = sum(row["label"].split(" ", 1)[0] in self.INDEX_TOOLS
                       for row in self.rows if row["kind"] == "tool")
        active = next((row for row in reversed(self.rows) if row["state"] == "running"), None)
        tree = CompactTree("", guide_style=PALETTE["faint"])
        if active is not None:
            label = Text(self.FRAMES[self._frame] + " ", style=PALETTE["accent"])
            if active["role"]:
                label.append(active["role"] + " · ", style=PALETTE["muted"])
            label.append(active["label"], style=PALETTE["text"])
            tree.add(label)
        summary = f"模型请求 {requests} 次 · 工具 {tools} 次"
        if reads:
            summary += f" · 读取 {reads}"
        if searches:
            summary += f" · 搜索 {searches}"
        tree.add(Text(("✓ " if not running else "◇ ") + summary,
                      style=PALETTE["online"] if not running else PALETTE["muted"]))
        for row in self.rows:
            if row["state"] not in {"failed", "warning", "unknown"}:
                continue
            color = PALETTE["error"] if row["state"] == "failed" else PALETTE["warning"]
            label = Text("◈ ", style=color)
            if row["role"]:
                label.append(row["role"] + " · ", style=PALETTE["muted"])
            label.append(row["label"], style=PALETTE["text"])
            if row["detail"]:
                label.append(" · " + row["detail"], style=color)
            if row["duration"] is not None:
                label.append(f" ({row['duration']:.1f}s)", style=PALETTE["faint"])
            tree.add(label)
        self.update(tree)


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
