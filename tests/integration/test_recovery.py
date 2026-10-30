"""通过真实 SDK + MockTransport 验证重试、工具事实恢复和不重放操作。"""

import io
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import httpx2
from openai import APIConnectionError, APIStatusError, OpenAI

from recapcli.cli import run_turn
from recapcli.core.agent import AgentLimits, run_agent
from recapcli.core.budget import AgentCancelled
from recapcli.models import request_completion
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore
from recapcli.tools.approval import approve_all
from recapcli.ui.terminal_ui import TerminalUI


def reply(text=None, calls=None):
    return {"id": "recovery-test", "object": "chat.completion", "created": 0, "model": "offline",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": text, "tool_calls": calls}}]}


def call(identifier, name="edit_file", **arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def unavailable(code=503, **headers):
    return httpx2.Response(code, headers={"retry-after": "0", **headers}, json={"error": {
        "message": "offline transient error", "type": "server_error"}})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("offline")
        self.requests = []
        self.ui = TerminalUI(io.StringIO(), io.StringIO(), color="never")

    def sdk(self, responses):
        def handler(request):
            self.requests.append(json.loads(request.content))
            value = responses[len(self.requests) - 1]
            return value if isinstance(value, httpx2.Response) else httpx2.Response(200, json=value)
        client = OpenAI(api_key="offline-fake-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self.addCleanup(client.close)
        return client

    def turn(self, responses, **options):
        return run_turn(self.sdk(responses), "offline", self.workspace, self.store, self.ui,
                        self.session, "执行一次操作", memory_enabled=False, auto_compact=False,
                        capabilities=("read", "write", "exec"), approver=approve_all, **options)

    def test_retry_after_edit_reuses_tool_result_without_reexecuting_edit(self):
        (self.workspace / "a.txt").write_text("old", encoding="utf-8")
        answer = self.turn([reply(calls=[call("edit", path="a.txt", old_text="old", new_text="new")]),
                            unavailable(429), unavailable(), reply("done")])
        self.assertEqual(answer, "done")
        self.assertEqual(len(list((self.workspace / ".recapcli/backups").glob("*.json"))), 1)
        self.assertEqual(self.requests[1], self.requests[2])
        self.assertEqual(self.requests[2], self.requests[3])
        task = TaskStore(self.workspace).list_tasks()[0]
        events = TaskStore(self.workspace).get(task["id"])["events"]
        self.assertEqual(sum(event["kind"] == "model_request" for event in events), 4)
        self.assertEqual(sum(event["kind"] == "tool_result" for event in events), 1)

    def test_command_receipt_survives_failed_request_and_continuation(self):
        script = "from pathlib import Path; p=Path('count.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
        with self.assertRaises(APIStatusError):
            self.turn([reply(calls=[call("command", "run_command", argv=[sys.executable, "-c", script])]),
                       unavailable(401)])
        self.session = self.store.load(self.session["id"])
        receipt = json.loads(self.session["messages"][2]["content"])
        self.assertTrue(receipt["ok"])
        self.assertIn("record_id", receipt)
        self.assertEqual(receipt["returncode"], 0)
        self.assertIn("本轮已中断", self.session["messages"][-1]["content"])
        self.requests = []
        self.turn([reply("继续核对已有操作")])
        self.assertIn("record_id", json.dumps(self.requests[0]["messages"]))
        self.assertEqual((self.workspace / "count.txt").read_text(), "x")
        self.assertEqual(len(list((self.workspace / ".recapcli/commands").glob("*.json"))), 1)

    def test_interrupted_tool_batch_closes_unknown_and_unexecuted_calls(self):
        calls = [call(str(index), path=f"{index}.txt", old_text="", new_text="created") for index in range(3)]
        approvals = 0
        def approve(request):
            nonlocal approvals
            approvals += 1
            if approvals == 2:
                raise KeyboardInterrupt()
            return approve_all(request)
        with self.assertRaises(KeyboardInterrupt):
            run_turn(self.sdk([reply(calls=calls)]), "offline", self.workspace, self.store, self.ui,
                     self.session, "创建三个文件", memory_enabled=False, auto_compact=False,
                     capabilities=("read", "write"), approver=approve)
        saved = self.store.load(self.session["id"])
        results = [json.loads(message["content"]) for message in saved["messages"] if message["role"] == "tool"]
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[1]["execution_status"], "unknown")
        self.assertEqual(results[2]["execution_status"], "not_executed")
        self.assertTrue((self.workspace / "0.txt").exists())
        self.assertFalse((self.workspace / "1.txt").exists())
        self.assertFalse((self.workspace / "2.txt").exists())

    def test_retries_count_toward_steps_and_keep_final_request_for_summary(self):
        self.turn([unavailable(), reply(calls=[call("read", "list_directory")]), reply("done")], max_steps=3)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1]["tool_choice"], "none")

    def test_request_timeout_is_configurable_and_bounded_by_task_time(self):
        with patch("recapcli.core.agent.request_completion", wraps=request_completion) as complete:
            self.turn([reply("done")], limits=AgentLimits(request_timeout=75, max_seconds=120))
        self.assertEqual(complete.call_args.kwargs["timeout"], 75)
        self.requests = []
        with patch("recapcli.core.agent.request_completion", wraps=request_completion) as complete:
            self.turn([reply("done")], limits=AgentLimits(request_timeout=75, max_seconds=10))
        self.assertLessEqual(complete.call_args.kwargs["timeout"], 10)

    def test_raw_sdk_automatic_retries_do_not_bypass_agent_accounting(self):
        client = self.sdk([unavailable(429), reply("done")])
        client.max_retries = 2
        run_turn(client, "offline", self.workspace, self.store, self.ui, self.session,
                 "hello", memory_enabled=False, auto_compact=False)
        tasks = TaskStore(self.workspace)
        self.assertEqual(tasks.get(tasks.list_tasks()[0]["id"])["model_requests"], 2)
        self.assertEqual(client.max_retries, 2)

    def test_permanent_http_and_quota_errors_are_not_retried(self):
        for status, code in ((400, None), (401, None), (403, None), (429, "insufficient_quota")):
            with self.subTest(status=status, code=code):
                self.requests = []
                response = httpx2.Response(status, json={"error": {"message": "offline permanent error", "code": code}})
                with self.assertRaises(APIStatusError):
                    self.turn([response])
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.session["messages"], [])

    def test_retry_after_beyond_remaining_budget_stops_without_wait(self):
        with self.assertRaises(APIStatusError):
            self.turn([unavailable(**{"retry-after": "1000"})], limits=AgentLimits(max_seconds=10))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session["messages"], [])

    def test_stream_with_visible_text_is_never_retried(self):
        client = self.sdk([])
        def interrupted(*args, **kwargs):
            kwargs["on_delta"]("partial text")
            raise APIConnectionError(request=httpx2.Request("POST", "https://offline.invalid"))
        with patch("recapcli.core.agent.request_completion", side_effect=interrupted) as complete:
            with self.assertRaises(APIConnectionError):
                run_agent(client, "offline", "hello", self.workspace, on_event=lambda text: None,
                          on_stream=lambda kind, text: None)
        self.assertEqual(complete.call_count, 1)

    def test_cancel_during_retry_wait_does_not_send_another_request(self):
        cancelled = Event()
        def event(text):
            if text.startswith("[重试]"):
                cancelled.set()
        with self.assertRaises(AgentCancelled):
            run_agent(self.sdk([unavailable()]), "offline", "hello", self.workspace,
                      on_event=event, cancel_event=cancelled)
        self.assertEqual(len(self.requests), 1)
