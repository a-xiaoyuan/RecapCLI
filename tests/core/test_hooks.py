"""Hooks：工具前可阻止，工具后可反馈，结束时可要求继续。"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import httpx2
from openai import OpenAI

from recapcli.application.commands import _TASK_EVENT_PRINTERS
from recapcli.application.options import hooks_from
from recapcli.cli import build_parser
from recapcli.core.agent import run_agent
from recapcli.core.hooks import HookSet
from recapcli.tools.approval import approve_all


def script(body):
    return [sys.executable, "-c", body]


BLOCK = script("import sys; print('不允许'); sys.exit(2)")
BROKEN = script("import sys; sys.exit(5)")
ALLOW = script("import sys; sys.exit(0)")
LOG = script("import sys; open('hook.log', 'a', encoding='utf-8').write(sys.stdin.read() + '\\n')")


def completion(message, finish_reason):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


def edit(new="new"):
    arguments = json.dumps({"path": "a.txt", "old_text": "old", "new_text": new})
    return completion({"role": "assistant", "content": None, "tool_calls": [{
        "id": "call", "type": "function", "function": {"name": "edit_file", "arguments": arguments}}]}, "tool_calls")


def text(content):
    return completion({"role": "assistant", "content": content}, "stop")


class HookTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.file = self.workspace / "a.txt"
        self.file.write_text("old", encoding="utf-8")

    def hooks(self, **events):
        path = self.workspace / "hooks.json"
        path.write_text(json.dumps(events), encoding="utf-8")
        return HookSet.load(path)

    def run_turn(self, replies, hooks):
        self.requests = []
        self.traces = []

        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(self.requests) - 1])

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with client:
            return run_agent(client, "test-model", "修改", self.workspace, capabilities=("read", "write"),
                             approver=approve_all, on_event=lambda event: None, hooks=hooks,
                             on_trace=lambda kind, data: self.traces.append((kind, data)))

    def events(self):
        return [data for kind, data in self.traces if kind == "hook"]

    def test_pre_tool_block_stops_the_edit_and_tells_the_model(self):
        self.run_turn([edit(), text("不改了")], self.hooks(pre_tool=[{"command": BLOCK}]))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "old")
        self.assertTrue(self.events()[0]["blocked"])
        self.assertIn("不允许", json.dumps(self.requests[1]["messages"], ensure_ascii=False))

    def test_match_selects_tools(self):
        hooks = self.hooks(pre_tool=[{"match": "run_.*", "command": BLOCK}])
        self.run_turn([edit(), text("好了")], hooks)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "new")
        self.assertEqual(self.events(), [])

    def test_broken_hook_does_not_block(self):
        self.run_turn([edit(), text("好了")], self.hooks(pre_tool=[{"command": BROKEN}]))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "new")
        self.assertFalse(self.events()[0]["blocked"])

    def test_missing_program_does_not_block(self):
        self.run_turn([edit(), text("好了")], self.hooks(pre_tool=[{"command": ["recapcli-no-such-hook"]}]))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "new")

    def test_post_tool_sees_the_call_and_can_send_feedback(self):
        hooks = self.hooks(post_tool=[{"command": LOG}, {"command": BLOCK}])
        self.run_turn([edit(), text("收到")], hooks)
        logged = json.loads((self.workspace / "hook.log").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((logged["event"], logged["tool"], logged["arguments"]["path"], logged["result"]["ok"]),
                         ("post_tool", "edit_file", "a.txt", True))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "new")
        self.assertIn("不允许", json.dumps(self.requests[1]["messages"], ensure_ascii=False))

    def test_stop_block_makes_the_model_continue(self):
        once = script("import os, sys\nif os.path.exists('seen'): sys.exit(0)\nopen('seen', 'w').close()\nprint('还没写测试')\nsys.exit(2)")
        result = self.run_turn([text("完成"), text("现在完成")], self.hooks(stop=[{"command": once}]))
        self.assertEqual(result.answer, "现在完成")
        self.assertEqual(result.status, "returned")
        self.assertIn("还没写测试", json.dumps(self.requests[1]["messages"], ensure_ascii=False))

    def test_repeated_stop_block_ends_as_incomplete(self):
        result = self.run_turn([text("完成")] * 3, self.hooks(stop=[{"command": BLOCK}]))
        self.assertEqual(result.status, "incomplete")
        self.assertIn("结束 Hook 未放行", result.stop_reason)
        self.assertEqual(len(self.requests), 3)

    def test_pre_tool_hook_disables_parallel_reads_but_still_runs(self):
        calls = [{"id": f"c{index}", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}
                 for index in range(2)]
        reply = completion({"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")
        self.run_turn([reply, text("读完了")], self.hooks(pre_tool=[{"command": ALLOW}]))
        self.assertEqual(len(self.events()), 2)


class HookConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
            return HookSet.load(path)

    def test_invalid_configs_are_rejected(self):
        for data in ("not json", [], {"unknown": []}, {"stop": {}}, {"pre_tool": [{}]}, {"pre_tool": [{"command": "ls"}]},
                     {"stop": [{"command": ["x"], "match": "y"}]}, {"pre_tool": [{"command": ["x"], "match": "("}]},
                     {"pre_tool": [{"command": ["x"], "timeout": 0}]}, {"pre_tool": [{"command": ["x"], "extra": 1}]}):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "--hooks"):
                self.load(data)

    def test_missing_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "--hooks"):
            HookSet.load("recapcli-no-such-hooks.json")

    def test_multi_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multi"):
                hooks_from(SimpleNamespace(hooks=path, mode="multi"))
            self.assertIsNotNone(hooks_from(SimpleNamespace(hooks=path, mode="react")))

    def test_cli_option_and_task_printer(self):
        self.assertIsNone(hooks_from(build_parser().parse_args([])))
        lines = []
        entry = {"event": "pre_tool", "tool": "edit_file", "command": ["check"], "blocked": True, "status": "finished", "returncode": 2}
        _TASK_EVENT_PRINTERS["hook"](SimpleNamespace(info=lines.append), "hook", entry, None, None, None)
        self.assertEqual(lines, ["Hook pre_tool edit_file：check 已阻止"])


if __name__ == "__main__":
    unittest.main()
