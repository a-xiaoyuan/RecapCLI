"""状态行、按键提示和任务详情文字。"""

import time
from rich.text import Text
from textual.widgets import Static

from ..application.changes import change_report, commands_summary, files_summary, git_summary
from ..core.project_rules import project_rules_description
from ..ui.terminal_ui import clean_output
from .status import TaskStatus, task_text, usage_text
from .theme import PALETTE, gradient_text, progress_bar
from .widgets import StatusScreen



class StatusBarMixin:
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
            text.append(title + "\n", style="bold")
            text.append(clean_output(content) + "\n\n", style=PALETTE["text"])
        return text

    def _tick_status(self):
        if self._busy:
            self._refresh_status()

    def _flow_status(self):
        """工作中让状态文字流光；空闲时不做任何刷新。"""
        if (self._busy and not self._approval and not self._cancel_requested and self._status_message
                and self.query("#status-line")):
            self._status_phase = (self._status_phase + 0.03) % 1.0
            self.query_one("#status-line", Static).update(gradient_text(self._status_message, self._status_phase, 40))

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
            label = f"● {activity} · {elapsed}s"
        if self.demo:
            label = ""
        elif not self._busy:
            label = ("模型请求超时，详情见上方提示" if "APITimeoutError" in self._task_status.stop_reason else
                     ("任务未完成，详情见上方提示" if self._task_status.status in {"failed", "blocked", "unknown"} else ""))
        color = PALETTE["error"] if self._task_status.status == "failed" and not self._busy else PALETTE["accent"]
        self.query_one("#status-line", Static).update(Text(label, style=color, no_wrap=True, overflow="ellipsis"))
        self._status_message = label
        composer = self.query_one("#composer")
        composer.set_class(self._busy and not self._approval, "-busy")
        composer.set_class(self._approval, "-approval")
        self._refresh_navigation()
        mode = " / ".join({"read": "读取", "write": "编辑", "exec": "命令", "web": "网页"}[item]
                          for item in self.options.capabilities)
        agent = Text(no_wrap=True, overflow="ellipsis")
        if self.demo:
            agent.append("界面演示 · 不请求模型")
        elif not self._model_ready:
            agent.append("模型未配置 · F3 设置")
        else:
            agent.append(clean_output(self._display_model or self._selected_profile().get("model") or "当前模型"))
        agent.append(" · " + self.options.mode + " · " + mode)
        self.query_one("#welcome-agent", Static).update(agent)
        self._model_label = agent.plain.split(" · ")[0]
        self._refresh_inspector(mode)
        self._refresh_hints()
        if isinstance(self.screen, StatusScreen) and self.screen.query("#status-body"):
            self.screen.query_one("#status-body", Static).update(self._status_details())

    def _refresh_inspector(self, mode):
        if not self.query("#task-inspector"):  # 退出时界面可能已经卸载。
            return
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
            ("模型请求", f"{requests} / {self.options.max_steps} 次" if task.task_id or requests else "尚未开始"),
            ("Token 用量", usage_text(task) if task.task_id else "尚无任务用量"),
            ("本轮编辑", file_text),
        ):
            body.append(title + "\n", style="bold " + PALETTE["accent"])
            body.append(clean_output(content) + "\n", style=PALETTE["muted"])
            if title == "模型请求":
                body.append_text(progress_bar(requests, self.options.max_steps, 24))
                body.append("\n")
            body.append("\n")
        body.append("Ctrl+G 查看完整详情", style=PALETTE["faint"])
        self.query_one("#task-inspector", Static).update(body)

    def _refresh_hints(self):
        if not self.query("#key-hints"):
            return
        if self._cancel_requested:
            hint = "正在停止 · 草稿保留 · ctrl+q 退出"
        elif self._approval:
            hint = "↑↓ 选择 · enter 确认 · 数字键直选 · esc 停止本轮"
        elif self._busy:
            hint = "esc 停止 · 可先写下一条 · ctrl+g 详情"
        elif self.demo:
            hint = "界面演示 · /help 命令 · ctrl+q 退出"
        elif self.size.width < 72:
            hint = "enter 发送 · /help" if self._model_ready else "/config 设置 · /help"
        else:
            hint = (f"{self._model_label} · enter 发送 · shift+enter 换行 · /help 命令" if self._model_ready
                    else "/config 设置模型 · /help 命令")
        self.query_one("#key-hints", Static).update(Text(hint, style=PALETTE["muted"], no_wrap=True, overflow="ellipsis"))
