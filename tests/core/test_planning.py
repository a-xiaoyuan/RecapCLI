import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from recapcli.cli import main
from recapcli.core.agent import AgentLimits, run_agent
from recapcli.storage.session import SessionStore
from recapcli.ui.terminal_ui import TerminalUI


def reply(content=None, calls=None):
    return {
        "id": "plan-test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                     "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
    }


def document(**data):
    return reply(json.dumps(data, ensure_ascii=False))


def call(name="read_file", arguments=None, call_id="read"):
    return reply(calls=[{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {"path": "app.py"}),
    }}])


def completed(reason="已根据实际结果核对"):
    return document(status="completed", reason=reason, steps=[])


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / "app.py").write_text("print(1)\n", encoding="utf-8")
        self.requests = []
        self.events = []

    def client(self, replies, on_request=None):
        def handler(request):
            data = json.loads(request.content)
            self.requests.append(data)
            if on_request is not None:
                on_request(len(self.requests))
            return httpx2.Response(200, json=replies[len(self.requests) - 1])

        return OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                      http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    def run_plan(self, replies, on_request=None, **options):
        with self.client(replies, on_request) as client:
            return run_agent(client, "test-model", "分析 app.py", self.workspace,
                             mode="plan", on_event=self.events.append, **options)

    def test_plan_execute_review_summary_preserve_one_user_turn_and_history(self):
        history = [{"role": "user", "content": "前问"}, {"role": "assistant", "content": "前答"}]
        original = json.loads(json.dumps(history))
        result = self.run_plan([
            document(steps=["读取 app.py", "解释输出"]), call(), reply("文件内容为 print(1)"),
            completed("app.py:1 的读取结果为 print(1)"), reply("输出为 1"), completed(), reply("程序输出 1"),
        ], history=history, memory_text="项目记忆：使用中文解释")
        self.assertIn("计划状态：已完成", result.answer)
        self.assertIn("2/2", result.answer)
        self.assertEqual(history, original)
        self.assertEqual(sum(message["role"] == "user" for message in result.messages), 1)
        self.assertEqual(result.messages[-1]["content"], result.answer)
        for index in (0, 3, 5, 6):
            self.assertEqual(self.requests[index]["tool_choice"], "none")
        self.assertNotIn("tool_choice", self.requests[1])
        self.assertIn("项目记忆：使用中文解释", self.requests[0]["messages"][0]["content"])
        self.assertIn("当前步骤：解释输出", self.requests[4]["messages"][0]["content"])
        tool_result = next(message for message in result.messages if message["role"] == "tool")
        self.assertEqual(tool_result["tool_call_id"], "read")
        self.assertTrue(json.loads(tool_result["content"])["ok"])
        self.assertTrue(any(event.startswith("[计划]") for event in self.events))
        self.assertEqual(sum(event.startswith("[完成]") for event in self.events), 2)

    def test_failed_tool_can_trigger_replanning_of_remaining_steps(self):
        result = self.run_plan([
            document(steps=["读取 missing.py", "解释旧文件"]),
            call(arguments={"path": "missing.py"}), reply("目标文件不存在"),
            document(status="replan", reason="文件名有误，改为实际存在的 app.py", steps=["读取 app.py"]),
            call(call_id="actual"), reply("已读取 app.py"), completed(), reply("app.py 输出 1"),
        ])
        self.assertIn("计划状态：已完成", result.answer)
        self.assertIn("1/1", result.answer)
        self.assertIn("当前步骤：读取 app.py", self.requests[4]["messages"][0]["content"])
        self.assertNotIn("解释旧文件", self.requests[4]["messages"][0]["content"])
        results = [json.loads(message["content"]) for message in result.messages if message["role"] == "tool"]
        self.assertEqual([item["ok"] for item in results], [False, True])
        self.assertEqual(sum(event.startswith("[重规划]") for event in self.events), 1)

    def test_replanning_retains_completed_prefix(self):
        result = self.run_plan([
            document(steps=["分析问题", "修改错误文件"]), reply("分析完毕"), completed(),
            reply("需要换一个目标文件"), document(status="replan", reason="目标改变", steps=["修改 app.py"]),
            reply("当前只读，不能修改"), document(status="blocked", reason="未开放编辑能力"), reply("尚未修改"),
        ])
        self.assertIn("未完成", result.answer)
        self.assertIn("1/2", result.answer)
        system = self.requests[5]["messages"][0]["content"]
        self.assertIn("[已完成] 分析问题", system)
        self.assertIn("当前步骤：修改 app.py", system)

    def test_blocked_step_is_saved_as_incomplete_with_reason(self):
        result = self.run_plan([
            document(steps=["读取 missing.py"]), call(arguments={"path": "missing.py"}),
            reply("文件不存在"), document(status="blocked", reason="用户指定的文件不存在"), reply("请核对路径"),
        ])
        self.assertTrue(result.answer.startswith("计划状态：未完成"))
        self.assertIn("0/1", result.answer)
        self.assertIn("用户指定的文件不存在", result.answer)
        self.assertEqual(len(self.requests), 5)

    def test_replanning_has_a_global_limit(self):
        replies = [document(steps=["分析问题"])]
        for number in range(3):
            replies.extend([reply("需要改计划"), document(
                status="replan", reason="信息变化", steps=[f"调整后的步骤 {number}"])])
        result = self.run_plan([*replies, reply("已停止，仍有未完成事项")])
        self.assertIn("未完成", result.answer)
        self.assertIn("2 次重规划上限", result.answer)
        self.assertEqual(sum(event.startswith("[重规划]") for event in self.events), 2)

    def test_invalid_plans_stop_before_tools_run(self):
        invalid = ["not-json", "[]", '{"steps":[]}', '{"steps":[1]}', '{"steps":[" "]}',
                   json.dumps({"steps": ["step"] * 6}), json.dumps({"steps": ["x" * 501]})]
        for content in invalid:
            with self.subTest(content=content):
                self.requests = []
                with patch("recapcli.core.agent.execute_tool") as execute, self.assertRaises(ValueError):
                    self.run_plan([reply(content)])
                self.assertEqual(execute.call_count, 0)

    def test_fenced_json_is_accepted(self):
        result = self.run_plan([
            reply('```json\n{"steps":["回答问题"]}\n```'), reply("答案"), completed(), reply("答案"),
        ])
        self.assertIn("已完成", result.answer)

    def test_invalid_review_cannot_advance_to_another_step(self):
        invalid = [
            {"status": "unknown", "reason": "说明"},
            {"status": "completed", "reason": ""},
            {"status": "completed", "reason": "说明", "steps": ["偷偷增加步骤"]},
            {"status": "replan", "reason": "说明", "steps": []},
        ]
        for review in invalid:
            with self.subTest(review=review):
                self.requests = []
                with patch("recapcli.core.agent.execute_tool") as execute, self.assertRaises(ValueError):
                    self.run_plan([document(steps=["分析问题", "修改文件"]), reply("说明"), document(**review)])
                self.assertEqual(execute.call_count, 0)
                self.assertEqual(len(self.requests), 3)

    def test_planning_review_and_summary_cannot_execute_tools(self):
        malicious = call("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(9)"})
        cases = [
            [malicious],
            [document(steps=["分析问题"]), reply("说明"), malicious],
            [document(steps=["分析问题"]), reply("说明"), completed(), malicious],
        ]
        for replies in cases:
            with self.subTest(requests=len(replies)):
                self.requests = []
                with patch("recapcli.core.agent.execute_tool") as execute, self.assertRaisesRegex(RuntimeError, "禁止调用工具"):
                    self.run_plan(replies, capabilities={"read", "write"})
                self.assertEqual(execute.call_count, 0)
                self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(1)\n")

    def test_execution_still_enforces_read_only_capabilities(self):
        result = self.run_plan([
            document(steps=["修改 app.py"]),
            call("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(9)"}),
            reply("未开启编辑"), document(status="blocked", reason="编辑能力未开启"), reply("未修改"),
        ])
        self.assertIn("未完成", result.answer)
        tool_result = next(message for message in result.messages if message["role"] == "tool")
        self.assertFalse(json.loads(tool_result["content"])["ok"])
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(1)\n")

    def test_model_budget_is_shared_and_last_request_reports_incomplete(self):
        result = self.run_plan([
            document(steps=["读取 app.py", "解释输出"]), call(), reply("读取结束"), reply("尚未完成核对"),
        ], max_steps=4)
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(result.answer.startswith("计划状态：未完成"))
        self.assertIn("0/2", result.answer)
        self.assertIn("预算", result.answer)
        self.assertEqual(self.requests[-1]["tool_choice"], "none")
        self.assertIn("当前阶段：汇总", self.requests[-1]["messages"][0]["content"])

    def test_tool_count_budget_does_not_reset_between_steps(self):
        result = self.run_plan([
            document(steps=["先读一次", "再读一次"]), call(), reply("读完"), completed(),
            call(call_id="second"), reply("预算耗尽，尚未核对第二步"),
        ], limits=AgentLimits(max_tool_calls=2))
        self.assertIn("未完成", result.answer)
        self.assertIn("1/2", result.answer)
        self.assertEqual(sum(message["role"] == "tool" for message in result.messages), 2)
        self.assertEqual(len(self.requests), 6)

    def test_output_budget_is_shared_with_planning(self):
        with patch("recapcli.core.agent.execute_tool", return_value={"ok": True, "content": "x" * 2000}) as execute:
            result = self.run_plan([document(steps=["读取文件"]), call(), reply("输出受限")],
                                   limits=AgentLimits(max_tool_calls=2, max_tool_bytes=300))
        self.assertEqual(execute.call_count, 1)
        self.assertIn("未完成", result.answer)
        tool_result = next(message for message in result.messages if message["role"] == "tool")
        self.assertFalse(json.loads(tool_result["content"])["ok"])

    def test_output_bytes_accumulate_across_completed_steps(self):
        with patch("recapcli.core.agent.execute_tool", return_value={"ok": True, "content": "x" * 450}) as execute:
            result = self.run_plan([
                document(steps=["读取第一部分", "读取第二部分"]), call(), reply("读完第一部分"), completed(),
                call(call_id="second"), reply("输出预算耗尽"),
            ], limits=AgentLimits(max_tool_calls=3, max_tool_bytes=900))
        self.assertEqual(execute.call_count, 2)
        self.assertIn("1/2", result.answer)
        results = [json.loads(message["content"]) for message in result.messages if message["role"] == "tool"]
        self.assertEqual([item["ok"] for item in results], [True, False])
        self.assertEqual(len(self.requests), 6)

    def test_time_budget_does_not_restart_after_planning(self):
        with patch("recapcli.core.agent.time.monotonic", return_value=0) as clock:
            def expire(number):
                if number == 3:
                    clock.return_value = 121

            with self.assertRaisesRegex(RuntimeError, "时间预算"):
                self.run_plan([document(steps=["分析问题"]), reply("说明"), completed()], on_request=expire,
                              limits=AgentLimits(max_seconds=120))
        self.assertEqual(len(self.requests), 3)

    def test_plan_mode_default_budget_supports_more_than_eight_requests(self):
        replies = [document(steps=[f"回答第 {number} 个问题" for number in range(5)])]
        for _ in range(5):
            replies.extend([reply("答案"), completed()])
        result = self.run_plan([*replies, reply("汇总")])
        self.assertEqual(len(self.requests), 12)
        self.assertIn("5/5", result.answer)

    def run_cli(self, argv, replies):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch("recapcli.config.MODELS_FILE", self.workspace / "no-models.toml"), \
                patch("recapcli.config.dotenv_values", return_value={
                    "OPENAI_API_KEY": "offline-test-key", "OPENAI_MODEL": "test-model", "OPENAI_STREAMING": "false"}), \
                patch("recapcli.cli.OpenAI", side_effect=lambda **options: self.client(replies)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([*argv, "--workspace", str(self.workspace), "--banner", "off", "--color", "never"])
        return code, output.getvalue(), errors.getvalue()

    def test_cli_plan_edits_runs_saves_and_continues_in_react_mode(self):
        replies = [
            document(steps=["读取并修改 app.py", "运行验证输出"]), call(),
            call("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"}, "edit"),
            call(call_id="reread"), reply("app.py:1 已核对改为 print(2)"), completed(),
            call("run_command", {"argv": [sys.executable, "app.py"]}, "run"),
            reply("进程输出 2，退出码 0"), completed(), reply("已修改并通过实际运行验证：app.py:1"),
        ]
        code, output, errors = self.run_cli(["修改并验证", "--mode", "plan", "--allow-write", "--allow-exec", "--yes"], replies)
        self.assertEqual(code, 0, errors)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(2)\n")
        self.assertIn("[计划]", output)
        self.assertIn("[核对]", output)
        self.assertIn("[命令输出]", output)
        self.assertIn("2/2", output)
        saved = SessionStore(self.workspace).latest()
        self.assertEqual(len(saved["messages"]), 15)
        results = [json.loads(message["content"]) for message in saved["messages"] if message["role"] == "tool"]
        self.assertEqual(results[-1]["output"].strip(), "2")
        self.assertEqual(results[-1]["returncode"], 0)
        self.assertIn("backup_id", results[1])
        self.requests = []
        code, _, errors = self.run_cli(["刚才改了什么", "--continue"], [reply("app.py 已改为输出 2")])
        self.assertEqual(code, 0, errors)
        self.assertIn(saved["messages"][-1], self.requests[0]["messages"])
        self.assertEqual(len(SessionStore(self.workspace).latest()["messages"]), 17)

    def test_interactive_mode_and_memory_are_used_on_each_turn(self):
        SessionStore(self.workspace).add_memory("使用中文解释", kind="preference")
        replies = []
        for _ in range(2):
            replies.extend([document(steps=["回答问题"]), reply("说明"), completed(), reply("回答")])
        with patch.object(TerminalUI, "read_prompt", side_effect=["第一问", "第二问", "/exit"]):
            code, _, errors = self.run_cli(["--mode", "plan"], replies)
        self.assertEqual(code, 0, errors)
        self.assertIn("当前阶段：制定计划", self.requests[4]["messages"][0]["content"])
        self.assertIn("使用中文解释", self.requests[4]["messages"][0]["content"])
        saved = SessionStore(self.workspace).latest()
        self.assertEqual(sum(message["role"] == "user" for message in saved["messages"]), 2)

    def test_cli_rejects_insufficient_budget_before_model_request(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["问题", "--mode", "plan", "--max-steps", "3"])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
