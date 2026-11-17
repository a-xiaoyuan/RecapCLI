"""Ctrl+K 命令面板：把斜杠命令、已保存会话和技能放在一个可搜索的列表里。"""

from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..application.help import command_items
from ..core.skills import discover_skills
from .state import _sessions
from .theme import PALETTE

MAX_SESSIONS = 30
MAX_ROWS = 9


@dataclass(frozen=True)
class Entry:
    kind: str      # 命令 / 会话 / 技能
    label: str
    detail: str
    action: str    # submit：直接执行；insert：写入输入框让用户补全
    text: str


def palette_entries(workspace):
    """读取当前可选项。会话和技能读取失败只是少一类，不影响命令。"""
    entries = []
    for name, arguments, description, _ in command_items(topic="all"):
        label = name + (" " + arguments if arguments else "")
        entries.append(Entry("命令", label, description, "insert" if arguments else "submit",
                             name + " " if arguments else name))
    try:
        for item in _sessions(workspace)[:MAX_SESSIONS]:
            entries.append(Entry("会话", item["id"], f"{item.get('messages', 0)} 条消息 · 恢复", "submit",
                                 "/open " + item["id"]))
    except (OSError, ValueError):
        pass
    try:
        for skill in discover_skills(workspace).skills:
            entries.append(Entry("技能", skill.name, skill.description, "insert", f"请使用技能「{skill.name}」："))
    except (OSError, ValueError):
        pass
    return entries


def filter_entries(entries, query):
    """所有词都要出现在“类型 名称 说明”里；名称命中的排在前面。"""
    words = query.casefold().split()
    if not words:
        return list(entries)
    found = []
    for entry in entries:
        label = entry.label.casefold()
        haystack = f"{entry.kind} {label} {entry.detail.casefold()}"
        if all(word in haystack for word in words):
            found.append((0 if all(word in label for word in words) else 1, entry))
    found.sort(key=lambda pair: pair[0])  # 稳定排序，同级保持原顺序
    return [entry for _, entry in found]


class CommandPaletteScreen(ModalScreen):
    BINDINGS = [Binding("escape,ctrl+k", "dismiss", show=False)]

    def __init__(self, entries):
        super().__init__()
        self.entries = entries
        self.shown = []

    def compose(self):
        with Vertical(classes="settings-dialog"):
            yield Static("命令面板", classes="dialog-title")
            yield Input(placeholder="搜索命令、会话或技能", id="palette-filter")
            yield OptionList(id="palette-options", markup=False)
            yield Static("", id="palette-count", markup=False)

    def on_mount(self):
        self._filter("")
        self.query_one("#palette-filter", Input).focus()

    def _filter(self, query):
        options = self.query_one("#palette-options", OptionList)
        options.clear_options()
        self.shown = filter_entries(self.entries, query)
        for entry in self.shown:
            label = Text(no_wrap=True, overflow="ellipsis")
            label.append(f"{entry.kind}  ", style=PALETTE["faint"])
            label.append(entry.label, style="bold " + PALETTE["accent"])
            label.append("  " + entry.detail, style=PALETTE["muted"])
            options.add_option(Option(label))
        options.styles.height = max(1, min(MAX_ROWS, len(self.shown)))
        if self.shown:
            options.highlighted = 0
        count = f"{len(self.shown)} 项 · ↑↓ 选择 · Enter 确认 · Esc 关闭" if self.shown else "没有匹配项 · Esc 关闭"
        self.query_one("#palette-count", Static).update(count)

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "palette-filter":
            self._filter(event.value)

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        options = self.query_one("#palette-options", OptionList)
        if options.highlighted is not None:
            self._choose(options.highlighted)

    def on_key(self, event):
        options = self.query_one("#palette-options", OptionList)
        if event.key in {"down", "up"} and self.shown:
            event.stop()
            event.prevent_default()
            step = 1 if event.key == "down" else -1
            options.highlighted = ((options.highlighted or 0) + step) % len(self.shown)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        event.stop()
        self._choose(event.option_index)

    def _choose(self, index):
        if 0 <= index < len(self.shown):
            entry = self.shown[index]
            self.dismiss((entry.action, entry.text))
