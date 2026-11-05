"""配置验证命令后，模型准备结束前自动运行；失败时把结果交回模型继续修。"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import httpx2
from openai import OpenAI

from recapcli.application.commands import _TASK_EVENT_PRINTERS
from recapcli.application.options import verify_command_from
from recapcli.cli import build_parser
from recapcli.core.agent import run_agent
from recapcli.tools.approval import approve_all, fail_closed

PASSES_WHEN_FIXED = [sys.executable, "-c",
                     "import sys; text = open('a.txt').read(); print('当前内容', text); sys.exit(text != 'fixed')"]
ALWAYS_FAILS = [sys.executable, "-c", "import sys; print('测试失败'); sys.exit(3)"]


def completion(message, finish_reason):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


def edit(old, new):
    arguments = json.dumps({"path": "a.txt", "old_text": old, "new_text": new})
    return completion({"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call-{new}", "type": "function", "function": {"name": "edit_file", "arguments": arguments}}]}, "tool_calls")


def text(content):
    return completion({"role": "assistant", "content": content}, "stop")


class AutoVerifyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        (self.workspace / "a.txt").write_text("old", encoding="utf-8")

    def run_turn(self, replies, command, approver=approve_all):
        self.requests = []
        self.traces = []
        self.events = []

        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(self.requests) - 1])

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with client:
            return run_agent(client, "test-model", "修改", self.workspace, capabilities=("read", "write"),
                             approver=approver, on_event=self.events.append, verify_command=command,
                             on_trace=lambda kind, data: self.traces.append((kind, data)))

    def checks(self):
        return [data for kind, data in self.traces if kind == "auto_verify"]

    def test_passing_check_finishes_the_turn(self):
        result = self.run_turn([edit("old", "fixed"), text("改好了")], PASSES_WHEN_FIXED)
        self.assertEqual(result.status, "returned")
        self.assertIn("自动验证通过", result.stop_reason)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.checks()[0]["success"])

    def test_failure_goes_back_to_the_model_until_fixed(self):
        result = self.run_turn([edit("old", "bad"), text("改好了"), edit("bad", "fixed"), text("这次好了")],
                               PASSES_WHEN_FIXED)
        self.assertEqual(result.status, "returned")
        self.assertEqual(result.answer, "这次好了")
        self.assertEqual([item["success"] for item in self.checks()], [False, True])
        feedback = json.dumps(self.requests[2]["messages"], ensure_ascii=False)
        self.assertIn("自动验证失败", feedback)
        self.assertIn("当前内容 bad", feedback)

    def test_repeated_failure_stops_as_incomplete(self):
        replies = [edit("old", "one"), text("好了"), edit("one", "two"), text("好了"), edit("two", "three"), text("好了")]
        result = self.run_turn(replies, ALWAYS_FAILS)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("自动验证未通过", result.stop_reason)
        self.assertEqual(len(self.checks()), 3)

    def test_no_edit_means_no_check(self):
        result = self.run_turn([text("只是回答")], ALWAYS_FAILS)
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.checks(), [])

    def test_rejected_edit_means_no_check(self):
        result = self.run_turn([edit("old", "fixed"), text("未修改")], ALWAYS_FAILS, approver=fail_closed)
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.checks(), [])

    def test_missing_program_is_reported_as_failure(self):
        result = self.run_turn([edit("old", "one"), text("好了"), text("没法验证")], ["recapcli-no-such-program"])
        self.assertFalse(self.checks()[0]["success"])
        self.assertEqual(result.status, "returned")

    def test_without_a_command_nothing_changes(self):
        result = self.run_turn([edit("old", "fixed"), text("改好了")], None)
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.checks(), [])


class VerifyOptionTests(unittest.TestCase):
    def test_cli_option_reaches_the_parser(self):
        args = build_parser().parse_args(["--verify-cmd", "python -m unittest"])
        self.assertEqual(verify_command_from(args), ["python", "-m", "unittest"])

    def test_task_details_show_the_result(self):
        lines = []
        ui = SimpleNamespace(info=lines.append)
        entry = {"command": ["pytest"], "success": False, "status": "finished", "returncode": 1}
        _TASK_EVENT_PRINTERS["auto_verify"](ui, "auto_verify", entry, None, None, None)
        self.assertEqual(lines, ["自动验证 pytest：失败（finished，退出码 1）"])

    def test_command_is_split_into_argv(self):
        args = SimpleNamespace(verify_cmd='python -m unittest "tests/a b.py"')
        self.assertEqual(verify_command_from(args), ["python", "-m", "unittest", "tests/a b.py"])

    def test_missing_option_means_disabled(self):
        self.assertIsNone(verify_command_from(SimpleNamespace(verify_cmd=None)))

    def test_blank_or_broken_command_is_rejected(self):
        for value in ("   ", 'python "unclosed'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "--verify-cmd"):
                verify_command_from(SimpleNamespace(verify_cmd=value))


if __name__ == "__main__":
    unittest.main()
