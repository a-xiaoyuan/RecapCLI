import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from revocli.cli import main, run_turn
from revocli.core.agent import AgentLimits, TraceError, run_agent
from revocli.core.budget import AgentBudget, AgentCancelled
from revocli.core.compaction import SUMMARY_FIELDS
from revocli.core.context import json_size
from revocli.storage.session import SessionStore
from revocli.storage.tasks import TaskStore
from revocli.ui.terminal_ui import TerminalUI


def reply(content=None, calls=None):
    return {"id": "subagent-test", "object": "chat.completion", "created": 0, "model": "test-model",
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": content, "tool_calls": calls}}]}


def tool(tool_name="read_file", **arguments):
    return {"id": "same-id", "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments or {"path": "app.py"})}}


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / "app.py").write_text("print(1)\n", encoding="utf-8")
        (self.workspace / "README.md").write_text("review source\n", encoding="utf-8")
        self.requests = []
        self.traces = []
        self.counts = {}
        self.lock = Lock()

    def client(self, respond):
        def handler(request):
            data = json.loads(request.content)
            system = data["messages"][0]["content"]
            role = next((role for role in ("analysis", "review", "aggregate")
                         if f"当前角色：{role}" in system), "compact")
            with self.lock:
                self.requests.append((role, data))
                self.counts[role] = self.counts.get(role, 0) + 1
                count = self.counts[role]
            response = respond(role, count, data)
            return response if isinstance(response, httpx2.Response) else httpx2.Response(200, json=response)
        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self.addCleanup(client.close)
        return client

    def run_multi(self, respond, **options):
        return run_agent(self.client(respond), "test-model", "分析并审查 app.py", self.workspace,
                         mode="multi", on_event=lambda text: None,
                         on_trace=lambda kind, data: self.traces.append((kind, deepcopy(data))), **options)

    def reading(self, role, count, data):
        if role == "aggregate":
            return reply("汇总：app.py:1；README.md:1；测试未验证")
        if count == 1:
            return reply(calls=[tool(path="app.py" if role == "analysis" else "README.md")])
        return reply(f"{role}-only：已读取来源，测试未验证")

    def test_real_overlap_independent_context_and_actual_tool_results(self):
        overlap = Barrier(2)
        history = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]
        original = deepcopy(history)

        def respond(role, count, data):
            if role != "aggregate" and count == 1:
                overlap.wait(timeout=5)  # 顺序执行会超时，必须两个 HTTP 调用同时在途。
            return self.reading(role, count, data)

        result = self.run_multi(respond, history=history)
        self.assertEqual(result.status, "returned")
        self.assertEqual(history, original)
        self.assertEqual(len(result.messages), 2)
        self.assertIn("任务验收未验证", result.answer)
        self.assertEqual(self.counts, {"analysis": 2, "review": 2, "aggregate": 1})
        for role, data in self.requests:
            if role in ("analysis", "review"):
                other = "review" if role == "analysis" else "analysis"
                self.assertNotIn(other + "-only", json.dumps(data["messages"], ensure_ascii=False))
                self.assertIn(history[0], data["messages"])
        facts = [data for kind, data in self.traces if kind == "tool_result"]
        self.assertEqual({item["result"]["reference"] for item in facts}, {"app.py:1", "README.md:1"})
        self.assertEqual(len({item["action_id"] for item in facts}), 2)
        self.assertEqual(sorted(data["index"] for kind, data in self.traces if kind == "model_request"), list(range(1, 6)))
        aggregate = next(data for role, data in self.requests if role == "aggregate")
        self.assertEqual(aggregate["tool_choice"], "none")
        self.assertNotIn("tools", aggregate)
        self.assertIn("analysis-only", aggregate["messages"][-1]["content"])
        self.assertIn("review-only", aggregate["messages"][-1]["content"])

    def test_child_permissions_never_include_write_or_exec_even_when_parent_grants_them(self):
        def respond(role, count, data):
            if role == "aggregate" or count > 1:
                return reply("权限拒绝，未修改未执行")
            names = {item["function"]["name"] for item in data["tools"]}
            self.assertNotIn("edit_file", names)
            self.assertNotIn("run_command", names)
            return reply(calls=[tool("edit_file", path="app.py", old_text="print(1)", new_text="print(2)"),
                                {**tool("run_command", argv=["python", "app.py"]), "id": "exec"}])
        self.run_multi(respond, capabilities=("read", "write", "exec"))
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(1)\n")
        results = [data["result"] for kind, data in self.traces if kind == "tool_result"]
        self.assertEqual(len(results), 4)
        self.assertTrue(all(not item["ok"] for item in results))
        self.assertFalse((self.workspace / ".revocli" / "commands").exists())

    def test_parent_without_read_cannot_grant_read_to_children(self):
        def respond(role, count, data):
            if role == "aggregate" or count > 1:
                return reply("未获读取权限")
            self.assertNotIn("tools", data)
            return reply(calls=[tool()])
        self.run_multi(respond, capabilities=())
        facts = [data for kind, data in self.traces if kind == "tool_result"]
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(not item["result"]["ok"] for item in facts))

    def test_shared_request_budget_reserves_parent_summary(self):
        def respond(role, count, data):
            if data.get("tool_choice") == "none":
                return reply("预算结束，汇报现状")
            return reply(calls=[tool()])
        result = self.run_multi(respond, max_steps=7)
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(self.counts, {"analysis": 3, "review": 3, "aggregate": 1})
        self.assertEqual(result.status, "incomplete")
        budget = next(data for kind, data in self.traces if kind == "subagent_budget")
        self.assertEqual(budget["model_requests"], 7)
        self.assertEqual(budget["tool_calls"], 4)

    def test_shared_tool_budget_reservation_is_atomic(self):
        overlap = Barrier(2)

        def respond(role, count, data):
            if role != "aggregate" and count == 1:
                overlap.wait(timeout=5)
            return self.reading(role, count, data)
        result = self.run_multi(respond, limits=AgentLimits(max_tool_calls=1))
        facts = [data for kind, data in self.traces if kind == "tool_result"]
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0]["result"]["ok"])
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(next(data for kind, data in self.traces if kind == "subagent_budget")["tool_calls"], 1)

    def test_shared_output_budget_counts_both_inflight_tools(self):
        overlap = Barrier(2)

        def execute(*args, **kwargs):
            overlap.wait(timeout=5)
            return {"ok": True, "content": "x" * 600}
        with patch("revocli.core.agent.execute_tool", side_effect=execute):
            result = self.run_multi(self.reading, limits=AgentLimits(max_tool_calls=4, max_tool_bytes=1000))
        outputs = [json.loads(message["content"]) for role, data in self.requests if role != "aggregate"
                   for message in data["messages"] if message["role"] == "tool"]
        self.assertEqual(sum(item["ok"] for item in outputs), 1)
        budget = next(data for kind, data in self.traces if kind == "subagent_budget")
        self.assertLessEqual(budget["tool_bytes"], 1000)
        self.assertTrue(budget["output_exhausted"])
        self.assertEqual(result.status, "incomplete")

    def test_pre_cancelled_task_makes_no_model_requests(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(AgentCancelled):
            self.run_multi(self.reading, cancel_event=cancelled)
        self.assertEqual(self.requests, [])

    def test_cancellation_propagates_to_both_inflight_children_before_tools(self):
        cancelled, overlap = Event(), Barrier(2)

        def respond(role, count, data):
            overlap.wait(timeout=5)
            cancelled.set()
            return reply(calls=[tool()])
        with patch("revocli.core.agent.execute_tool") as execute, self.assertRaises(AgentCancelled):
            self.run_multi(respond, cancel_event=cancelled)
        execute.assert_not_called()
        self.assertEqual(self.counts, {"analysis": 1, "review": 1})
        stopped = [data["agent"] for kind, data in self.traces if kind == "subagent_stopped"]
        self.assertEqual(set(stopped), {"analysis", "review"})

    def test_cancel_after_tool_result_stops_next_requests_and_aggregation(self):
        cancelled = Event()

        def trace(kind, data):
            self.traces.append((kind, data))
            if kind == "tool_result":
                cancelled.set()
        with self.assertRaises(AgentCancelled):
            run_agent(self.client(self.reading), "test-model", "读取", self.workspace, mode="multi",
                      cancel_event=cancelled, on_event=lambda text: None, on_trace=trace)
        self.assertNotIn("aggregate", self.counts)
        self.assertTrue(all(count == 1 for count in self.counts.values()))

    def test_trace_failure_cancels_siblings_and_is_not_a_partial_success(self):
        cancelled = Event()

        def trace(kind, data):
            if kind == "tool_start":
                raise OSError("offline trace failure")
        with patch("revocli.core.agent.execute_tool") as execute, self.assertRaisesRegex(TraceError, "trace failure"):
            run_agent(self.client(self.reading), "test-model", "读取", self.workspace, mode="multi",
                      cancel_event=cancelled, on_event=lambda text: None, on_trace=trace)
        self.assertTrue(cancelled.is_set())
        execute.assert_not_called()

    def test_shared_deadline_does_not_restart_in_children(self):
        overlap = Barrier(2)
        clock = [0.0]
        limits = AgentLimits(max_seconds=3)
        budget = AgentBudget(limits, 20, clock=lambda: clock[0])

        def respond(role, count, data):
            overlap.wait(timeout=5)
            clock[0] = 4.0
            return reply(calls=[tool()])
        with patch("revocli.core.agent.execute_tool") as execute, self.assertRaisesRegex(RuntimeError, "时间预算"):
            self.run_multi(respond, limits=limits, _budget=budget)
        execute.assert_not_called()
        self.assertEqual(budget.requests, 2)

    def test_parent_keyboard_interrupt_cancels_inflight_workers_and_joins_them(self):
        cancelled, started = Event(), Event()
        entered = []

        def respond(role, count, data):
            with self.lock:
                entered.append(role)
                if len(entered) == 2:
                    started.set()
            self.assertTrue(cancelled.wait(timeout=5))
            return reply(calls=[tool()])

        def interrupt(*args, **kwargs):
            self.assertTrue(started.wait(timeout=5))
            raise KeyboardInterrupt()

        with patch("revocli.core.subagents.wait", side_effect=interrupt), \
                patch("revocli.core.agent.execute_tool") as execute, self.assertRaises(KeyboardInterrupt):
            self.run_multi(respond, cancel_event=cancelled)
        self.assertTrue(cancelled.is_set())
        execute.assert_not_called()
        self.assertEqual(self.counts, {"analysis": 1, "review": 1})
        self.assertEqual(len([item for item in self.traces if item[0] == "subagent_stopped"]), 2)

    def test_oversized_reports_are_marked_truncated_but_full_reports_are_traced(self):
        def respond(role, count, data):
            if role != "aggregate":
                return reply(role + "x" * 15000)
            self.assertLessEqual(json_size(data["messages"]), 12000)
            reports = json.loads(data["messages"][-1]["content"].split("：\n", 1)[1])
            self.assertTrue(all(report["truncated"] for report in reports))
            return reply("报告截断，完整内容见任务追溯")
        result = self.run_multi(respond, limits=AgentLimits(max_context_bytes=12000))
        self.assertEqual(result.status, "returned")
        reports = [data for kind, data in self.traces if kind == "subagent_result"]
        self.assertTrue(all(len(report["answer"]) > 15000 for report in reports))

    def test_timeout_during_aggregation_is_not_downgraded_to_a_returned_report(self):
        clock = [0.0]
        limits = AgentLimits(max_seconds=3)
        budget = AgentBudget(limits, 20, clock=lambda: clock[0])

        def respond(role, count, data):
            if role == "aggregate":
                clock[0] = 4.0
            return reply(role)
        with self.assertRaisesRegex(RuntimeError, "时间预算"):
            self.run_multi(respond, limits=limits, _budget=budget)
        self.assertEqual(budget.requests, 3)

    def test_failed_child_keeps_other_report_and_marks_incomplete(self):
        def respond(role, count, data):
            if role == "analysis":
                return httpx2.Response(500, json={"error": {"message": "offline error"}})
            return self.reading(role, count, data)
        result = self.run_multi(respond)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("analysis=failed", result.answer)
        aggregate = next(data for role, data in self.requests if role == "aggregate")
        self.assertIn("review-only", aggregate["messages"][-1]["content"])
        self.assertEqual(self.counts["analysis"], 1)

    def test_aggregation_failure_preserves_reports_without_retry(self):
        def respond(role, count, data):
            if role == "aggregate":
                return httpx2.Response(500, json={"error": {"message": "offline error"}})
            return self.reading(role, count, data)
        result = self.run_multi(respond)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("analysis-only", result.answer)
        self.assertIn("review-only", result.answer)
        self.assertIn("未重复请求", result.answer)
        self.assertEqual(self.counts["aggregate"], 1)

    def test_aggregate_cannot_execute_tools_even_if_provider_ignores_tool_choice(self):
        def respond(role, count, data):
            return reply(calls=[tool("edit_file", path="new.py", old_text="", new_text="bad")]) if role == "aggregate" else reply(role)
        with patch("revocli.core.agent.execute_tool") as execute:
            result = self.run_multi(respond, capabilities=("read", "write", "exec"))
        execute.assert_not_called()
        self.assertEqual(result.status, "incomplete")

    def test_auto_compaction_is_once_and_counts_against_shared_total(self):
        history = [message for index in range(4) for message in (
            {"role": "user", "content": "x" * 600}, {"role": "assistant", "content": "y" * 300})]
        original = deepcopy(history)

        def respond(role, count, data):
            if role == "compact":
                return reply(json.dumps({field: "保留来源与未验证项" for field in SUMMARY_FIELDS}))
            return reply(role + " 报告")
        result = self.run_multi(respond, history=history, auto_compact=True, max_steps=4,
                                limits=AgentLimits(max_context_bytes=5000))
        self.assertEqual(self.counts, {"compact": 1, "analysis": 1, "review": 1, "aggregate": 1})
        self.assertEqual(result.status, "returned")
        self.assertIsNotNone(result.summary)
        self.assertEqual(history, original)

    def test_concurrent_index_queries_work_on_fresh_workspace(self):
        overlap = Barrier(2)

        def respond(role, count, data):
            if role != "aggregate" and count == 1:
                overlap.wait(timeout=5)
                return reply(calls=[tool("search_symbols", name="hello")])
            return reply(role)
        (self.workspace / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
        self.run_multi(respond)
        results = [data["result"] for kind, data in self.traces if kind == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertTrue(all(result["matches"][0]["reference"] == "app.py:1-2" for result in results))

    def test_cli_persists_one_parent_turn_role_traces_usage_and_unverified_status(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch("revocli.config.dotenv_values", return_value={"OPENAI_API_KEY": "offline", "OPENAI_MODEL": "test-model"}), \
                patch("revocli.cli.OpenAI", side_effect=lambda **options: self.client(self.reading)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["分析", "--workspace", str(self.workspace), "--mode", "multi", "--banner", "off", "--color", "never"])
        self.assertEqual(code, 0, errors.getvalue())
        store = SessionStore(self.workspace)
        self.assertEqual(len(store.latest()["messages"]), 2)
        tasks = TaskStore(self.workspace, create=False)
        task = tasks.get(tasks.list_tasks()[0]["id"])
        self.assertEqual(task["mode"], "multi")
        self.assertEqual(task["status"], "returned")
        self.assertEqual(task["verification_status"], "unverified")
        self.assertEqual(task["model_requests"], 5)
        self.assertEqual(task["usage"]["total_tokens"], 70)
        self.assertEqual({event["data"]["agent"] for event in task["events"] if event["kind"] == "model_request"},
                         {"analysis", "review", "aggregate"})
        self.assertIn("[analysis]", output.getvalue())
        self.assertIn("[review]", output.getvalue())
        session = store.latest()
        with self.client(lambda *args: reply("继续回答")) as client:
            answer = run_turn(client, "test-model", self.workspace, store, TerminalUI(io.StringIO()), session,
                              "继续", mode="react", auto_compact=False)
        self.assertEqual(answer, "继续回答")
        self.assertEqual(len(session["messages"]), 4)

    def test_cancelled_turn_has_cancelled_task_and_no_session_mutation(self):
        cancelled = Event()
        cancelled.set()
        store = SessionStore(self.workspace)
        session = store.new_session("test-model")
        original = deepcopy(session)
        with self.assertRaises(AgentCancelled):
            run_turn(self.client(self.reading), "test-model", self.workspace, store, TerminalUI(io.StringIO()),
                     session, "读取", mode="multi", cancel_event=cancelled)
        self.assertEqual(session, original)
        self.assertEqual(TaskStore(self.workspace, create=False).list_tasks()[0]["status"], "cancelled")
        self.assertEqual(self.requests, [])

    def test_invalid_request_budget_stops_before_model_and_cli_config(self):
        with self.assertRaisesRegex(ValueError, "至少需要 3"):
            self.run_multi(self.reading, max_steps=2)
        with contextlib.redirect_stderr(io.StringIO()), patch("revocli.cli.load_config") as config, \
                self.assertRaises(SystemExit):
            main(["读取", "--mode", "multi", "--max-steps", "2"])
        config.assert_not_called()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
