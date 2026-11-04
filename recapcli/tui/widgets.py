"""聊天输入、工具进度、历史跟随与按需显示的状态详情。"""

import time

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from rich.text import Text
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..ui.terminal_ui import clean_output
from .theme import PALETTE, gradient_text, progress_bar, render_answer, render_line


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
            rendered = Text.assemble(("❯ ", "bold " + PALETTE["accent"]), shown, overflow="fold")
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


class PromptInput(TextArea):
    """完整保留粘贴内容；输入高度随实际换行增长。"""

    BINDINGS = [Binding("ctrl+a", "select_all", "全选", show=False)]

    class Submitted(Message):
        pass

    def __init__(self, **kwargs):
        super().__init__(soft_wrap=True, tab_behavior="focus", compact=True,
                         highlight_cursor_line=False, **kwargs)
        self.sent = []
        self._recall_index = None
        self._recall_draft = ""

    def remember(self, value):
        if value.strip() and (not self.sent or self.sent[-1] != value):
            self.sent.append(value)
        self._recall_index = None

    def recall(self, step):
        """step 为 -1 向前翻历史；翻过最新一条时恢复未发送的草稿。"""
        if self._recall_index is None:
            if step > 0 or not self.sent:
                return False
            self._recall_draft = self.text
            index = len(self.sent) - 1
        else:
            index = self._recall_index + step
        if index < 0:
            return True
        self._recall_index = index if index < len(self.sent) else None
        value = self._recall_draft if self._recall_index is None else self.sent[index]
        self.app.dismiss_command_menu(value)
        self.load_text(value)
        self.move_cursor(self.document.end)
        return True

    def _history_key(self, event):
        if event.key == "up" and self.cursor_location[0] == 0:
            return self.recall(-1)
        if (event.key == "down" and self._recall_index is not None
                and self.cursor_location[0] == self.document.line_count - 1):
            return self.recall(1)
        return False

    async def _on_key(self, event: events.Key):
        if self.app.approval_key(event) or self.app.command_menu_key(event) or self._history_key(event):
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
            label.append("  " + description, style=PALETTE["muted"])
            self.add_option(Option(label, id=name))
        self.styles.height = min(len(commands), max_rows)
        self.display = bool(commands)
        if commands:
            self.highlighted = 0


class ApprovalMenu(OptionList):
    """审批选项；与输入共用焦点，所以用户仍可手动输入 approve / reject / cancel。"""

    can_focus = False

    def __init__(self, **kwargs):
        super().__init__(markup=False, **kwargs)
        self.values = []
        self.labels = []
        self.display = False

    def _prompt(self, index):
        marker = "❯ " if index == self.highlighted else "  "
        return Text(f"{marker}{index + 1}. {self.labels[index]}", no_wrap=True, overflow="ellipsis")

    def show_choices(self, choices):
        self.values = [value for value, _ in choices]
        self.labels = [label for _, label in choices]
        self.clear_options()
        self.highlighted = None
        for index in range(len(choices)):
            self.add_option(Option(self._prompt(index)))
        self.styles.height = len(choices)
        self.display = True
        self.highlighted = 0

    def on_option_list_option_highlighted(self, event):
        event.stop()
        for index in range(len(self.labels)):
            self.replace_option_prompt_at_index(index, self._prompt(index))


class ToolProgress(Static):
    """同一轮的工具调用逐行列出，太长时只留最近几条和有问题的；末行是当前动作或摘要。"""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    INDEX_TOOLS = {"search_code_index", "search_symbols"}
    MAX_VISIBLE_TOOLS = 8
    VERBS = {"read_file": "读取", "list_directory": "目录", "find_files": "查找", "search_text": "搜索",
             "search_symbols": "符号", "search_code_index": "检索", "repository_map": "地图",
             "find_references": "引用", "edit_file": "编辑", "edit_files": "批量编辑", "undo_edit": "撤销",
             "restore_file": "恢复", "run_command": "运行", "command_status": "查看命令", "command_input": "输入",
             "stop_command": "停止", "web_search": "网页搜索", "read_url": "读取网页"}
    PROBLEMS = {"failed", "warning", "unknown", "skipped"}

    def __init__(self):
        super().__init__("", classes="turn progress-turn", markup=False)
        self.rows = []
        self.content_text = ""
        self._frame = 0
        self._timer = None
        self._started = time.monotonic()

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
            state = "failed" if result.startswith("失败") else "skipped" if result.startswith("未执行") else "done"
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
        tools = [row for row in self.rows if row["kind"] == "tool"]
        # 没用工具的一轮不需要过程信息，回答本身就够了。
        self.display = bool(tools) or running
        if not self.rows:
            self.update("")
            return
        requests = sum(row["kind"] == "request" for row in self.rows)
        reads = sum(row["label"].split(" ", 1)[0] in {"read_file", "find_files", "list_directory"} for row in tools)
        searches = sum(row["label"].split(" ", 1)[0] in self.INDEX_TOOLS for row in tools)
        parts = ([f"模型请求 {requests} 次"] if requests else []) + [f"工具 {len(tools)} 次"]
        if reads:
            parts.append(f"读取 {reads}")
        if searches:
            parts.append(f"搜索 {searches}")
        text = Text(no_wrap=False)
        recent = len(tools) - self.MAX_VISIBLE_TOOLS
        shown = [row for index, row in enumerate(tools) if index >= recent or row["state"] in self.PROBLEMS]
        if len(shown) < len(tools):
            text.append(f"… 还有 {len(tools) - len(shown)} 次工具调用，Ctrl+G 查看任务详情\n", style=PALETTE["muted"])
        for row in shown:
            self._tool_line(text, row)
        request = next((row for row in reversed(self.rows)
                        if row["kind"] == "request" and row["state"] == "running"), None)
        if request is not None:
            text.append(self.FRAMES[self._frame] + " ", style=PALETTE["accent"])
            if request["role"]:
                text.append(request["role"] + " · ", style=PALETTE["muted"])
            text.append(request["label"] + "\n")
        if running:
            text.append("  └ " + " · ".join(parts), style=PALETTE["muted"])
        else:
            elapsed = time.monotonic() - self._started
            text.append("  └ " + " · ".join(parts) + f" · {elapsed:.1f}s", style=PALETTE["muted"])
        self.update(text)

    def _tool_line(self, text, row):
        state = row["state"]
        color = {"running": PALETTE["accent"], "done": PALETTE["online"], "failed": PALETTE["error"]}.get(state, PALETTE["warning"])
        glyph = {"running": self.FRAMES[self._frame], "done": "●", "failed": "✗"}.get(state, "!")
        text.append(glyph + " ", style=color)
        if row["role"]:
            text.append(row["role"] + " · ", style=PALETTE["muted"])
        name, _, target = row["label"].partition(" · ")
        text.append(self.VERBS.get(name, name))
        if target:
            text.append("  " + target, style=PALETTE["muted"])
        if row["duration"] is not None and row["duration"] >= 1:
            text.append(f" · {row['duration']:.1f}s", style=PALETTE["muted"])
        if row["detail"]:
            text.append("\n  └ ", style=PALETTE["muted"])
            text.append(row["detail"], style=PALETTE["muted"] if state == "done" else color)
        text.append("\n")


class GradientLogo(Static):
    """渐变字标：启动时流动一小段，然后静止，不占用后续刷新。"""

    FRAMES = 36

    def __init__(self, art, **kwargs):
        super().__init__("", markup=False, **kwargs)
        lines = art.split("\n")
        width = max(len(line) for line in lines)
        self.art = "\n".join(line.ljust(width) for line in lines)
        self._frame = 0
        self._timer = None

    def on_mount(self):
        self._draw()
        self._timer = self.set_interval(0.07, self._advance)

    def _draw(self):
        self.update(gradient_text(self.art, self._frame / self.FRAMES))

    def _advance(self):
        self._frame += 1
        self._draw()
        if self._frame >= self.FRAMES:
            self._timer.stop()


class TodoPanel(Static):
    """模型用 todo_write 维护的清单；同一轮内原地更新。"""

    def __init__(self):
        super().__init__("", classes="turn todo-turn", markup=False)
        self.content_text = ""

    def show(self, body):
        self.content_text = body
        lines = [line for line in body.splitlines() if line.strip()]
        done = sum(line.startswith("☑") for line in lines)
        text = Text()
        text.append("待办", style="bold " + PALETTE["accent"])
        text.append("  ")
        text.append_text(progress_bar(done, len(lines), 10))
        text.append(f"  {done}/{len(lines)}", style=PALETTE["muted"])
        for line in lines:
            mark, _, content = line.partition(" ")
            text.append("\n")
            if mark == "☑":
                text.append("☑ " + content, style=PALETTE["muted"] + " strike")
            elif mark == "◐":
                text.append("◐ ", style=PALETTE["accent"])
                text.append(content)
            else:
                text.append("☐ " + content)
        self.update(text)


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
