import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from recapcli.cli import main
from recapcli.application.runtime import run_turn
from recapcli.application.commands import handle_command
from recapcli.core.agent import AgentLimits, run_agent
from recapcli.core.compaction import prepare_compaction, turn_ends
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore
from recapcli.ui.terminal_ui import TerminalUI


CONTENT = {"goal": "修复会话保存", "constraints": "使用 uv；保留原始历史",
           "decisions": "只修改工作区", "actions": "已读取代码", "verification": "尚未人工验收", "pending": "继续检查"}
USAGE = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}


def reply(text, calls=None):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model", "usage": USAGE,
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": text, "tool_calls": calls}}]}


def history(count=4):
    return [message for index in range(count) for message in (
        {"role": "user", "content": f"旧问题{index}:" + "x" * 600},
        {"role": "assistant", "content": f"旧回答{index}:" + "y" * 300})]


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("test-model")
        self.session["messages"] = history()
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.ui = TerminalUI(self.output, self.errors, color="never")
        self.requests = []

    def client(self, replies, on_request=None):
        def handler(request):
            self.requests.append(json.loads(request.content))
            if on_request:
                on_request()
            response = replies[len(self.requests) - 1]
            if isinstance(response, BaseException):
                raise response
            return httpx2.Response(200, json=response)
        return OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                      http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    def run_turn(self, replies, **options):
        with self.client(replies) as client:
            return run_turn(client, "test-model", self.workspace, self.store, self.ui, self.session,
                            "继续检查会话保存", **options)

    def tasks(self):
        store = TaskStore(self.workspace, create=False)
        return [store.get(item["id"]) for item in store.list_tasks()]

    def test_manual_compact_persists_summary_keeps_archive_and_traces_usage(self):
        original = copy.deepcopy(self.session["messages"])
        answer = self.run_turn([reply(json.dumps(CONTENT))], compact_only=True)
        self.assertIn("原始消息完整保留", answer)
        self.assertEqual(self.session["messages"], original)
        self.assertEqual(self.session["summary"]["covered_messages"], 4)
        self.assertNotIn("tools", self.requests[0])
        self.assertEqual(self.requests[0]["tool_choice"], "none")
        self.store.save(self.session)
        self.session = self.store.load(self.session["id"])
        self.assertEqual(self.session["messages"], original)
        task = self.tasks()[0]
        self.assertEqual(task["mode"], "compact")
        self.assertEqual(task["model_requests"], 1)
        self.assertEqual(task["usage"]["total_tokens"], 14)
        self.assertEqual(task["verification_status"], "unverified")
        self.assertEqual(self.session["summary"]["task_id"], task["id"])
        self.requests = []
        self.run_turn([reply("接着检查")])
        sent = self.requests[0]["messages"]
        self.assertIn("历史摘要", sent[0]["content"])
        self.assertIn("保留原始历史", sent[0]["content"])
        self.assertNotIn(original[0], sent)
        self.assertIn(original[4], sent)
        self.assertEqual(self.session["messages"][:8], original)
        self.assertEqual(len(self.session["messages"]), 10)

    def test_auto_compaction_shares_request_budget_and_preserves_input(self):
        original = copy.deepcopy(self.session["messages"])
        self.run_turn([reply(json.dumps(CONTENT)), reply("完成回答")], max_steps=2,
                      limits=AgentLimits(max_context_bytes=5000))
        task = self.tasks()[0]
        phases = [event["data"]["phase"] for event in task["events"] if event["kind"] == "model_request"]
        self.assertEqual(phases, ["compact", "react"])
        self.assertEqual(task["model_requests"], 2)
        self.assertEqual(task["usage"]["total_tokens"], 28)
        self.assertEqual(self.requests[-1]["tool_choice"], "none")
        self.assertEqual(self.session["messages"][:8], original)
        self.assertIn("summary", self.session)

    def test_auto_failure_falls_back_and_keeps_failed_request_in_usage(self):
        self.run_turn([reply("invalid json"), reply("回退后回答")], max_steps=2,
                      limits=AgentLimits(max_context_bytes=5000))
        self.assertNotIn("summary", self.session)
        self.assertEqual(self.session["messages"][-1]["content"], "回退后回答")
        task = self.tasks()[0]
        self.assertEqual(task["usage"]["total_tokens"], 28)
        self.assertTrue(any(event["kind"] == "compaction_failed" for event in task["events"]))

    def test_network_failure_falls_back_but_missing_usage_stays_unknown(self):
        self.run_turn([httpx2.ConnectError("offline"), reply("回退")], max_steps=2,
                      limits=AgentLimits(max_context_bytes=5000))
        self.assertIsNone(self.tasks()[0]["usage"])
        self.assertEqual(self.tasks()[0]["model_requests"], 2)

    def test_manual_invalid_response_preserves_saved_data(self):
        path = self.store.save(self.session)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.run_turn([reply("invalid")], compact_only=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("summary", self.session)
        self.assertEqual(self.tasks()[0]["status"], "failed")

    def test_compaction_never_executes_requested_tools(self):
        calls = [{"id": "bad", "type": "function", "function": {"name": "edit_file", "arguments": json.dumps(
            {"path": "unexpected.txt", "old_text": "", "new_text": "bad"})}}]
        with self.assertRaises(ValueError):
            self.run_turn([reply(None, calls)], compact_only=True, capabilities=("read", "write"))
        self.assertFalse((self.workspace / "unexpected.txt").exists())
        self.assertFalse(any(event["kind"] == "tool_start" for event in self.tasks()[0]["events"]))

    def test_auto_compaction_cannot_consume_only_final_request(self):
        self.run_turn([reply("唯一回答")], max_steps=1, limits=AgentLimits(max_context_bytes=5000))
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("summary", self.session)
        self.assertTrue(any(event["kind"] == "compaction_skipped" for event in self.tasks()[0]["events"]))

    def test_record_failure_after_summary_stops_without_next_request(self):
        append = TaskStore.append

        def fail(store, task_id, kind, data):
            if kind == "compaction":
                raise OSError("offline disk full")
            return append(store, task_id, kind, data)

        with patch.object(TaskStore, "append", new=fail), self.assertRaises(RuntimeError):
            self.run_turn([reply(json.dumps(CONTENT))], max_steps=2,
                          limits=AgentLimits(max_context_bytes=5000))
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("summary", self.session)

    def test_second_compaction_merges_prior_summary_and_covers_only_new_turns(self):
        self.run_turn([reply(json.dumps(CONTENT))], compact_only=True)
        prior = copy.deepcopy(self.session["summary"])
        self.session["messages"].extend(history(2))
        self.requests = []
        self.run_turn([reply(json.dumps({**CONTENT, "pending": "核对新增消息"}))], compact_only=True)
        payload = json.loads(self.requests[0]["messages"][1]["content"])
        self.assertEqual(payload["previous_summary"], CONTENT)
        self.assertEqual(len(payload["turns"]), 4)
        self.assertEqual(prior["covered_messages"], 4)
        self.assertEqual(self.session["summary"]["covered_messages"], 8)

    def test_source_hash_prevents_using_summary_after_archive_edit(self):
        self.run_turn([reply(json.dumps(CONTENT))], compact_only=True)
        self.session["messages"][0]["content"] = "原文已被修改"
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.store.save(self.session)

    def test_tool_pairs_are_never_split_and_open_turn_is_rejected(self):
        messages = history()
        messages[1:1] = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "actual result"}]
        request, end = prepare_compaction(messages, None, 5000)
        self.assertIn(end, turn_ends(messages))
        payload = json.loads(request[1]["content"])
        self.assertEqual(payload["turns"][1]["tool_calls"][0]["id"], payload["turns"][2]["tool_call_id"])
        with self.assertRaises(ValueError):
            prepare_compaction(messages + [{"role": "user", "content": "open"}], None, 5000)

    def test_single_oversized_turn_is_not_truncated_into_fake_coverage(self):
        messages = history()
        messages[0]["content"] = "x" * 10000
        with self.assertRaisesRegex(ValueError, "超过摘要输入预算"):
            prepare_compaction(messages, None, 2000)

    def test_empty_and_short_session_needs_no_model_request(self):
        self.session["messages"] = history(2)
        self.assertIn("没有可压缩", self.run_turn([], compact_only=True))
        self.assertEqual(self.requests, [])
        self.assertNotIn("summary", self.session)

    def test_retrieved_memory_sources_are_recorded_alongside_actual_input(self):
        fact = self.store.add_memory("会话保存采用原子替换")
        self.store.add_memory("Kafka 使用业务幂等键")
        self.run_turn([reply("已分析")])
        context = next(event["data"] for event in self.tasks()[0]["events"] if event["kind"] == "context")
        self.assertEqual(context["memory_ids"], [fact["id"]])
        self.assertEqual(context["memory_sources"][0]["source"]["type"], "user")
        self.assertIn("会话保存采用原子替换", self.requests[0]["messages"][0]["content"])
        self.assertNotIn("Kafka 使用业务幂等键", self.requests[0]["messages"][0]["content"])

    def test_no_memory_and_no_auto_compact_make_only_the_answer_request(self):
        self.store.add_memory("必须注入的偏好", kind="preference")
        self.run_turn([reply("回答")], memory_enabled=False, auto_compact=False,
                      limits=AgentLimits(max_context_bytes=5000))
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("必须注入的偏好", self.requests[0]["messages"][0]["content"])
        self.assertNotIn("summary", self.session)

    def test_plan_mode_reserves_four_requests_and_uses_saved_summary(self):
        self.run_turn([reply(json.dumps(CONTENT))], compact_only=True)
        self.requests = []
        self.run_turn([reply(json.dumps({"steps": ["核对"]})), reply("执行说明"),
                       reply(json.dumps({"status": "completed", "reason": "模型核对", "steps": []})), reply("汇总")],
                      mode="plan", max_steps=4, limits=AgentLimits(max_context_bytes=5000))
        self.assertEqual(len(self.requests), 4)
        self.assertIn("历史摘要", self.requests[0]["messages"][0]["content"])
        self.assertEqual(self.tasks()[0]["verification_status"], "unverified")

    def test_compaction_time_counts_against_the_same_deadline(self):
        clock = [0.0]
        with self.client([reply(json.dumps(CONTENT))], on_request=lambda: clock.__setitem__(0, 11.0)) as client, \
                patch("recapcli.core.agent.time.monotonic", side_effect=lambda: clock[0]), \
                self.assertRaisesRegex(RuntimeError, "时间预算"):
            run_agent(client, "test-model", "继续", self.workspace, history=self.session["messages"],
                      auto_compact=True, max_steps=2, limits=AgentLimits(max_context_bytes=5000, max_seconds=10),
                      on_event=lambda event: None)
        self.assertEqual(len(self.requests), 1)

    def test_library_compaction_does_not_mutate_caller_history(self):
        original = copy.deepcopy(self.session["messages"])
        with self.client([reply(json.dumps(CONTENT)), reply("回答")]) as client:
            result = run_agent(client, "test-model", "继续", self.workspace, history=self.session["messages"],
                               auto_compact=True, max_steps=2, limits=AgentLimits(max_context_bytes=5000),
                               on_event=lambda event: None)
        self.assertEqual(self.session["messages"], original)
        self.assertIsNotNone(result.summary)
        self.assertEqual([message["role"] for message in result.messages], ["user", "assistant"])

    def test_cancelled_compaction_keeps_archive_and_records_cancelled_task(self):
        original = copy.deepcopy(self.session)
        with self.assertRaises(KeyboardInterrupt):
            self.run_turn([KeyboardInterrupt()], compact_only=True)
        self.assertEqual(self.session, original)
        self.assertEqual(self.tasks()[0]["status"], "cancelled")

    def test_auto_compaction_and_plan_share_all_five_requests(self):
        self.run_turn([reply(json.dumps(CONTENT)), reply(json.dumps({"steps": ["核对"]})), reply("说明"),
                       reply(json.dumps({"status": "completed", "reason": "模型核对", "steps": []})), reply("汇总")],
                      mode="plan", max_steps=5, limits=AgentLimits(max_context_bytes=5000))
        self.assertEqual(len(self.requests), 5)
        task = self.tasks()[0]
        self.assertEqual(task["model_requests"], 5)
        self.assertEqual(task["usage"]["total_tokens"], 70)
        self.assertEqual(task["status"], "returned")

    def test_cli_compact_continue_and_summary_command(self):
        self.store.save(self.session)
        with self.client([reply(json.dumps(CONTENT))]) as client, \
                patch("recapcli.cli.load_config", return_value=type("Config", (), {"api_key": "offline", "model": "test-model", "base_url": None})()), \
                patch("recapcli.cli.OpenAI", return_value=client), \
                patch("sys.stdout", self.output):
            self.assertEqual(main(["--workspace", str(self.workspace), "--continue", "--compact", "--banner", "off"]), 0)
        loaded = self.store.latest()
        self.assertIn("summary", loaded)
        self.assertEqual(len(loaded["messages"]), 8)
        handle_command("/summary", self.ui, self.store, loaded, "test-model")
        self.assertIn(loaded["summary"]["task_id"], self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
