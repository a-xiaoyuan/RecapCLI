"""状态栏只采用服务用量和工具确认的本轮事实。"""

import unittest

from recapcli.tui.status import TaskStatus, task_text, usage_text


class StatusTests(unittest.TestCase):
    def test_usage_is_not_estimated_when_service_omits_it(self):
        self.assertIn("还没有任务", usage_text(TaskStatus()))
        self.assertIn("未知", usage_text(TaskStatus(task_id="a")))
        status = TaskStatus(task_id="a", usage={"prompt_tokens": 1200, "completion_tokens": None})
        self.assertEqual(usage_text(status), "输入  1,200\n输出  未知\n合计  未知")

    def test_only_confirmed_edits_and_restores_are_listed_once(self):
        def event(name, **result):
            return {"kind": "tool_result", "data": {"name": name, "result": result}}

        task = TaskStatus.from_task({"id": "a", "events": [
            {"kind": "tool_start", "data": {"name": "run_command", "action_id": "interrupted",
                "arguments": '{"argv":["python","verify.py"]}'}},
            {"kind": "tool_interrupted", "data": {"name": "run_command", "action_id": "interrupted"}},
            event("edit_file", ok=True, changed=True, path="a.py"),
            event("edit_file", ok=True, changed=True, path="a.py"),
            event("edit_file", ok=False, changed=True, path="failed.py"),
            event("edit_file", ok=True, changed=False, path="unchanged.py"),
            event("read_file", ok=True, path="read.py"),
            event("run_command", ok=True, changed=True, path="inferred.py"),
            event("restore_file", ok=True, restored=True, path="b.py"),
        ]})
        self.assertEqual(task.modified_files, ("a.py", "b.py"))
        interrupted = next(command for command in task.changes["commands"] if command["argv"] == ["python", "verify.py"])
        self.assertIsNone(interrupted["returncode"])
        self.assertIn("结果未知", interrupted["error"])

    def test_busy_and_approval_take_precedence_over_saved_terminal_state(self):
        status = TaskStatus(status="returned", duration=1.25)
        self.assertEqual(task_text(status), "已回答 · 1.2s")
        self.assertEqual(task_text(status, busy=True), "处理中…")
        self.assertEqual(task_text(status, busy=True, approval=True), "等待审批")
        self.assertEqual(task_text(TaskStatus(status="running")), "未获得终态")
