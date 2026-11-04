"""输入框里的斜杠命令列表和 @ 文件补全。"""

import re
import time
from textual.widgets import OptionList, Static, TextArea

from ..application.help import command_items, help_text
from .state import workspace_files
from .widgets import CommandMenu, PromptInput

MENTION_FILES_TTL = 30
MENTION_LIMIT = 20


class CommandInputMixin:
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
        mention = None if self._approval or value == self._command_dismissed else self._mention_query(event.text_area)
        if mention is not None:
            self._show_mentions(mention)
        elif self._busy or self._approval or value == self._command_dismissed or not value.startswith("/") or any(char.isspace() for char in value):
            self._hide_commands()
        else:
            self._show_commands("all", value)

    def dismiss_command_menu(self, value):
        """程序写入输入框的内容（如历史记录）不触发命令列表。"""
        self._command_dismissed = value
        self._hide_commands()

    def _mention_query(self, prompt):
        row, column = prompt.cursor_location
        match = re.search(r"(?:^|\s)@([^\s@]*)$", prompt.document.get_line(row)[:column])
        if match is None or match.group(1).startswith("image:"):
            return None
        return match.group(1)

    def _mention_candidates(self, query):
        now = time.monotonic()
        if self._mention_files is None or now - self._mention_loaded > MENTION_FILES_TTL:
            self._mention_files = workspace_files(self.state.workspace)
            self._mention_loaded = now
        needle = query.casefold()
        found = [path for path in self._mention_files if needle in path.casefold()]
        found.sort(key=lambda path: (not path.rsplit("/", 1)[-1].casefold().startswith(needle), len(path), path))
        return found[:MENTION_LIMIT]

    def _show_mentions(self, query):
        prompt = self.query_one("#prompt", PromptInput)
        self._command_anchor = prompt.text
        menu = self.query_one("#command-menu", CommandMenu)
        menu.show_commands([("@" + path, "", "", "file") for path in self._mention_candidates(query)],
                           max(3, min(7, self.size.height // 3)))
        hint = self.query_one("#command-hint", Static)
        hint.display = menu.display
        hint.update("↑↓ 选择 · Tab / Enter 插入路径 · Esc 收起")
        prompt.suggestion = ""

    def _insert_mention(self, name):
        prompt = self.query_one("#prompt", PromptInput)
        row, column = prompt.cursor_location
        start = prompt.document.get_line(row)[:column].rfind("@")
        self._hide_commands()
        prompt.replace(name + " ", (row, start), (row, column))
        prompt.focus()

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
        if name.startswith("@"):
            self._insert_mention(name)
            return
        prompt = self.query_one("#prompt", PromptInput)
        required =bool(arguments and not (arguments.startswith("[") and arguments.endswith("]")))
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
        elif event.option_list.id == "approval-menu":
            event.stop()
            self._choose_approval(event.option_index)
