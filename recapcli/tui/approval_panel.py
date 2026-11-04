"""审批菜单：按键选择、草稿保存和恢复。"""

import re
import time

from .widgets import ApprovalMenu, PromptInput

APPROVAL_GUARD_SECONDS = 0.4  # 菜单刚弹出时用户可能还在打字，这段时间的按键不算决定。


class ApprovalPanelMixin:
    def approval_key(self, event):
        menu = self.query_one("#approval-menu", ApprovalMenu)
        if not self._approval or not menu.display or self.query_one("#prompt", PromptInput).text:
            return False
        if event.key == "up":
            menu.action_cursor_up()
            return True
        if event.key == "down":
            menu.action_cursor_down()
            return True
        if event.key == "enter":
            index = menu.highlighted
        elif event.key.isdigit() and 1 <= int(event.key) <= len(menu.values):
            index = int(event.key) - 1
        else:
            return False
        if time.monotonic() - self._approval_since >= APPROVAL_GUARD_SECONDS:
            self._choose_approval(index)
        return True

    def _choose_approval(self, index):
        self.approval_queue.put(self.query_one("#approval-menu", ApprovalMenu).values[index])
        self.end_approval()

    def begin_approval(self, message):
        self._hide_commands()
        if len(self.screen_stack) > 1:
            self.pop_screen()
        self.append_line(message, "approval")
        self._approval = True
        self._approval_since = time.monotonic()
        prompt = self.query_one("#prompt", PromptInput)
        self._approval_draft = prompt.text
        prompt.load_text("")
        prompt.placeholder = "也可输入 approve、reject 或 cancel"
        remember = re.search(r"^输入 always 可在本会话允许(.+)。$", message, re.MULTILINE)
        self.query_one("#approval-menu", ApprovalMenu).show_choices(
            [("approve", "批准")] + ([("always", f"批准，本会话允许{remember.group(1)}")] if remember else [])
            + [("reject", "拒绝")])
        prompt.focus()
        self._refresh_status()

    def end_approval(self):
        if self._approval:
            self.query_one("#prompt", PromptInput).load_text(self._approval_draft)
            self._approval_draft = ""
        self.query_one("#approval-menu", ApprovalMenu).display = False
        self._approval = False
        if self._busy:
            self.query_one("#prompt", PromptInput).placeholder = "可先写下一条问题，Esc 停止本轮…"
        self._refresh_status()
