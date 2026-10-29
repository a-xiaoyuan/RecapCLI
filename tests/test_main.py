import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import OpenAI

from revocli.core.agent import AgentLimits, run_agent
from revocli.core.context import json_size, select_history
from revocli.cli import main
from revocli.storage.session import SessionStore
from revocli.ui.terminal_ui import TerminalUI
from revocli.tools import execute_tool
from revocli.tools.filesystem import MAX_FILE_BYTES


def reply(content=None, calls=None, finish=None):
    return {
        "id": "test-completion", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": finish or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
    }


def tool_call(call_id="call-1", path="README.md", name="read_file", arguments=None):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": arguments if arguments is not None else json.dumps({"path": path}),
    }}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("启动：python app.py", encoding="utf-8")

    def run_with_replies(self, replies, max_steps=8, on_request=None, **options):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if on_request:
                on_request(requests[-1])
            return httpx2.Response(200, json=replies[len(requests) - 1])

        with OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
            self.last_result = run_agent(client, "test-model", "读取 README.md", self.workspace,
                                         max_steps=max_steps, on_event=lambda event: None, **options)
        return self.last_result.answer, requests

    def test_file_result_is_sent_back_with_matching_call_id(self):
        answer, requests = self.run_with_replies([
            reply(calls=[tool_call()]), reply("使用 python app.py 启动"),
        ])
        self.assertEqual(answer, "使用 python app.py 启动")
        self.assertEqual(len(requests), 2)
        history = requests[1]["messages"]
        self.assertEqual(history[-2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(history[-1]["tool_call_id"], "call-1")
        result = json.loads(history[-1]["content"])
        self.assertEqual(result["content"], "启动：python app.py")
        self.assertEqual(result["reference"], "README.md:1")

    def test_multiple_calls_and_another_tool_round(self):
        _, requests = self.run_with_replies([
            reply(calls=[tool_call("first"), tool_call("second", path="missing.txt")]),
            reply(calls=[tool_call("third")]), reply("完成"),
        ])
        results = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in results], ["first", "second", "third"])
        self.assertEqual([json.loads(message["content"])["ok"] for message in results], [True, False, True])

    def test_direct_answer_needs_one_request(self):
        answer, requests = self.run_with_replies([reply("你好")])
        self.assertEqual(answer, "你好")
        self.assertEqual(len(requests), 1)

    def test_invalid_arguments_return_to_model_as_error(self):
        _, requests = self.run_with_replies([
            reply(calls=[tool_call(arguments="not-json")]), reply("参数错误，未读取文件"),
        ])
        self.assertFalse(json.loads(requests[1]["messages"][-1]["content"])["ok"])

    def test_model_request_limit_stops_endless_tool_calls(self):
        with self.assertRaisesRegex(RuntimeError, "2 次模型请求上限"):
            self.run_with_replies([reply(calls=[tool_call()])] * 2, max_steps=2)

    def test_last_request_reserves_answer_and_does_not_execute_more_tools(self):
        with patch("revocli.core.agent.execute_tool", return_value={"ok": True, "content": "file"}) as tool:
            _, requests = self.run_with_replies([reply(calls=[tool_call()]), reply("根据文件总结")], max_steps=2)
        self.assertEqual(requests[-1]["tool_choice"], "none")
        self.assertEqual(tool.call_count, 1)
        with patch("revocli.core.agent.execute_tool") as tool, self.assertRaises(RuntimeError):
            self.run_with_replies([reply(calls=[tool_call()])], max_steps=1)
        tool.assert_not_called()

    def test_tool_count_rejects_oversized_batch_before_any_execution(self):
        with patch("revocli.core.agent.execute_tool") as tool, self.assertRaisesRegex(RuntimeError, "工具数量"):
            self.run_with_replies([reply(calls=[tool_call("a"), tool_call("b")])],
                                  limits=AgentLimits(max_tool_calls=1))
        tool.assert_not_called()

    def test_tool_count_budget_allows_final_summary(self):
        _, requests = self.run_with_replies([reply(calls=[tool_call()]), reply("总结")],
                                          limits=AgentLimits(max_tool_calls=1))
        self.assertEqual(requests[-1]["tool_choice"], "none")

    def test_output_budget_returns_paired_error_and_stops_further_execution(self):
        limits = AgentLimits(max_tool_calls=2, max_tool_bytes=1000)
        with patch("revocli.core.agent.execute_tool", return_value={"ok": True, "content": "中" * 2000}) as tool:
            _, requests = self.run_with_replies(
                [reply(calls=[tool_call("a"), tool_call("b")]), reply("请缩小读取范围")], limits=limits)
        results = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in results], ["a", "b"])
        self.assertTrue(all(not json.loads(message["content"])["ok"] for message in results))
        self.assertLessEqual(sum(len(message["content"].encode("utf-8")) for message in results), 1000)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(requests[-1]["tool_choice"], "none")

    def test_history_is_immutable_and_result_contains_only_new_turn(self):
        history = [{"role": "user", "content": "前问"}, {"role": "assistant", "content": "前答"}]
        before = json.loads(json.dumps(history))
        self.run_with_replies([reply("本轮回答")], history=history)
        self.assertEqual(history, before)
        self.assertEqual([message["content"] for message in self.last_result.messages], ["读取 README.md", "本轮回答"])

    def test_context_budget_drops_old_turns_as_tool_results_grow(self):
        history = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "x" * 4200}]
        with patch("revocli.core.agent.execute_tool", return_value={"ok": True, "content": "y" * 3500}):
            _, requests = self.run_with_replies([reply(calls=[tool_call()]), reply("完成")],
                                              history=history, limits=AgentLimits(max_context_bytes=6500))
        self.assertTrue(any(message.get("content") == "旧问题" for message in requests[0]["messages"]))
        self.assertFalse(any(message.get("content") == "旧问题" for message in requests[1]["messages"]))
        self.assertTrue(all(json_size(request["messages"]) <= 6500 for request in requests))

    def test_oversized_prompt_stops_before_model(self):
        with OpenAI(api_key="offline", base_url="https://offline.invalid/v1") as client, \
                patch.object(client.chat.completions, "create") as create, self.assertRaisesRegex(RuntimeError, "上下文"):
            run_agent(client, "test", "中" * 2000, self.workspace, limits=AgentLimits(max_context_bytes=1000))
        create.assert_not_called()

    def test_tool_output_also_fits_current_turn_context_budget(self):
        with patch("revocli.core.agent.execute_tool", return_value={"ok": True, "content": "x" * 10000}):
            _, requests = self.run_with_replies([reply(calls=[tool_call()]), reply("请缩小读取范围")],
                                              limits=AgentLimits(max_context_bytes=3500))
        self.assertLessEqual(json_size(requests[1]["messages"]), 3500)
        self.assertFalse(json.loads(requests[1]["messages"][-1]["content"])["ok"])
        self.assertEqual(requests[1]["tool_choice"], "none")

    def test_oversized_tool_request_is_rejected_before_execution(self):
        with patch("revocli.core.agent.execute_tool") as tool, self.assertRaisesRegex(RuntimeError, "工具请求本身"):
            self.run_with_replies([reply(calls=[tool_call(arguments=json.dumps({"path": "x" * 3000}))])],
                                  limits=AgentLimits(max_context_bytes=3500))
        tool.assert_not_called()

    def test_time_budget_stops_after_slow_response_before_tool_execution(self):
        with patch("revocli.core.agent.time") as clock, patch("revocli.core.agent.execute_tool") as tool:
            clock.monotonic.return_value = 0
            with self.assertRaisesRegex(RuntimeError, "时间预算"):
                self.run_with_replies([reply(calls=[tool_call()])],
                                      on_request=lambda request: setattr(clock.monotonic, "return_value", 121))
        tool.assert_not_called()

    def test_duplicate_tool_ids_are_rejected_before_execution(self):
        with patch("revocli.core.agent.execute_tool") as tool, self.assertRaisesRegex(RuntimeError, "调用 ID"):
            self.run_with_replies([reply(calls=[tool_call(), tool_call()])])
        tool.assert_not_called()

    def test_truncated_response_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "length"):
            self.run_with_replies([reply("未完成的回答", finish="length")])

    def test_empty_response_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "没有返回正文"):
            self.run_with_replies([reply()])

    def test_tool_rejects_invalid_input_and_outside_path(self):
        (self.root / "outside.txt").write_text("outside", encoding="utf-8")
        cases = [
            ("read_file", '{"path":"../outside.txt"}'),
            ("read_file", '{"path":123}'),
            ("read_file", '{"path":"README.md","extra":true}'),
            ("read_file", '[]'),
            ("read_file", '{"path":"missing.txt"}'),
            ("other_tool", '{}'),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertFalse(execute_tool(name, arguments, self.workspace)["ok"])

    def test_size_and_encoding_errors_are_structured(self):
        (self.workspace / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        (self.workspace / "binary.txt").write_bytes(b"\xff")
        for filename in ("large.txt", "binary.txt"):
            with self.subTest(filename=filename):
                self.assertFalse(execute_tool("read_file", json.dumps({"path": filename}), self.workspace)["ok"])

    def test_read_file_supports_line_range(self):
        result = execute_tool("read_file", json.dumps(
            {"path": "README.md", "start_line": 1, "end_line": 1}), self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "启动：python app.py")

    def test_search_text_finds_matches_with_location(self):
        (self.workspace / "pkg").mkdir()
        (self.workspace / "pkg" / "app.py").write_text(
            "def run():\n    return '启动服务'\n", encoding="utf-8")
        result = execute_tool("search_text", json.dumps({"query": "启动服务"}), self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["path"], "pkg/app.py")
        self.assertEqual(result["matches"][0]["line"], 2)

    def test_search_text_respects_case_sensitivity(self):
        (self.workspace / "code.py").write_text("Hello\nhello\n", encoding="utf-8")
        insensitive = execute_tool("search_text", json.dumps({"query": "HELLO"}), self.workspace)
        sensitive = execute_tool("search_text", json.dumps(
            {"query": "HELLO", "case_sensitive": True}), self.workspace)
        self.assertEqual(len(insensitive["matches"]), 2)
        self.assertEqual(sensitive["matches"], [])

    def test_list_directory_returns_tree_and_honours_depth(self):
        (self.workspace / "pkg").mkdir()
        (self.workspace / "pkg" / "deep").mkdir()
        (self.workspace / "pkg" / "deep" / "file.py").write_text("x", encoding="utf-8")
        shallow = execute_tool("list_directory", json.dumps({"max_depth": 1}), self.workspace)
        deep = execute_tool("list_directory", json.dumps({"max_depth": 3}), self.workspace)
        self.assertTrue(shallow["ok"])
        self.assertIn("pkg/", shallow["tree"])
        self.assertNotIn("file.py", shallow["tree"])
        self.assertIn("file.py", deep["tree"])

    def test_search_and_tree_reject_unknown_arguments(self):
        for name, arguments in [
            ("search_text", {"query": "x", "extra": True}),
            ("list_directory", {"path": ".", "extra": True}),
            ("read_file", {"path": "README.md", "start_line": "1"}),
        ]:
            with self.subTest(name=name):
                self.assertFalse(execute_tool(name, json.dumps(arguments), self.workspace)["ok"])

    def test_tool_cannot_read_revocli_credentials(self):
        local_config = self.workspace / ".env"
        local_config.write_text("OPENAI_API_KEY=private-test-key\n", encoding="utf-8")
        with patch("revocli.tools.filesystem.ENV_FILE", local_config):
            result = execute_tool("read_file", '{"path":".env"}', self.workspace)
        self.assertFalse(result["ok"])
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_missing_configuration_stops_before_model_request(self):
        with patch.dict("os.environ", {}, clear=True), patch("revocli.config.dotenv_values", return_value={}), patch("revocli.cli.OpenAI") as client:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["你好", "--workspace", str(self.workspace)])
        self.assertEqual(error.exception.code, 2)
        client.assert_not_called()

    def test_cli_loads_own_config_and_environment_key_takes_priority(self):
        app_config = self.root / ".env"
        app_config.write_text(
            "OPENAI_BASE_URL=https://app.local.invalid/v1\nOPENAI_MODEL=local-model\n"
            "OPENAI_API_KEY=file-test-key\n", encoding="utf-8",
        )
        (self.workspace / ".env").write_text(
            "OPENAI_BASE_URL=https://wrong.invalid/v1\n", encoding="utf-8",
        )
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(200, json=reply("配置已读取"))

        original_client = OpenAI

        def client_factory(**kwargs):
            return original_client(
                **kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            )

        with patch.dict("os.environ", {"OPENAI_API_KEY": "environment-test-key"}, clear=True):
            with patch("revocli.config.ENV_FILE", app_config), patch("revocli.cli.OpenAI", side_effect=client_factory):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = main(["你好", "--workspace", str(self.workspace)])
        self.assertEqual(result, 0)
        self.assertEqual(requests[0].url.host, "app.local.invalid")
        self.assertEqual(requests[0].headers["authorization"], "Bearer environment-test-key")
        self.assertEqual(json.loads(requests[0].content)["model"], "local-model")


class ConversationTests(unittest.TestCase):
    """多轮对话、会话恢复与长期记忆。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.config = {"OPENAI_API_KEY": "offline-test-key", "OPENAI_MODEL": "test-model"}

    def run_main(self, argv, replies):
        requests = []
        self.requests = requests
        self.output, self.errors = io.StringIO(), io.StringIO()

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        def factory(**kwargs):
            return OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1",
                          max_retries=0,
                          http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

        with patch.dict("os.environ", {}, clear=True), \
                patch("revocli.config.dotenv_values", return_value=self.config), \
                patch("revocli.cli.OpenAI", side_effect=factory):
            with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
                code = main(argv)
        return code, requests

    def test_allow_web_enables_tools_in_model_request(self):
        base = ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"]
        for flags, expected in (([], False), (["--allow-web"], True)):
            with self.subTest(flags=flags):
                code, requests = self.run_main(["你好", *base, *flags], [reply("你好")])
                self.assertEqual(code, 0)
                names = {tool["function"]["name"] for tool in requests[0]["tools"]}
                self.assertEqual({"web_search", "fetch_url"}.issubset(names), expected)

    def test_continue_reuses_previous_session(self):
        base = ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"]
        code, first = self.run_main(["第一问", *base], [reply("第一答")])
        self.assertEqual(code, 0)
        code, second = self.run_main(["第二问", "--continue", *base], [reply("第二答")])
        self.assertEqual(code, 0)
        self.assertEqual(len(second), 1)
        sent = second[0]["messages"]
        contents = [message.get("content") for message in sent]
        self.assertIn("第一问", contents)
        self.assertIn("第一答", contents)
        self.assertIn("第二问", contents)

        sessions = list((self.workspace / ".revocli" / "sessions").glob("*.json"))
        self.assertTrue(sessions)
        saved = json.loads(sessions[0].read_text(encoding="utf-8"))
        self.assertEqual(len(saved["messages"]), 4)

    def test_memory_is_injected_into_system_prompt(self):
        SessionStore(self.workspace).add_memory("项目使用 uv 管理依赖")
        code, requests = self.run_main(
            ["uv 依赖怎么管理", "--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
            [reply("好的")],
        )
        self.assertEqual(code, 0)
        system = requests[0]["messages"][0]["content"]
        self.assertIn("项目使用 uv 管理依赖", system)

    def test_find_read_edit_run_and_reference_in_one_agent_turn(self):
        path = self.workspace / "app.py"
        path.write_text("print(1)\n", encoding="utf-8")
        calls = [
            ("find_files", {"pattern": "app.py"}),
            ("read_file", {"path": "app.py"}),
            ("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"}),
            ("run_command", {"argv": [sys.executable, "app.py"]}),
            ("read_file", {"path": "app.py"}),
        ]
        replies = [reply(calls=[tool_call(f"call-{number}", name=name, arguments=json.dumps(arguments))])
                   for number, (name, arguments) in enumerate(calls)]
        code, requests = self.run_main(
            ["修改并验证", "--workspace", str(self.workspace), "--allow-write", "--allow-exec", "--yes", "--banner", "off"],
            [*replies, reply("`app.py:1` 已改为输出 2，运行退出码为 0。")])
        self.assertEqual(code, 0, self.errors.getvalue())
        self.assertEqual(path.read_text(encoding="utf-8"), "print(2)\n")
        definitions = {item["function"]["name"] for item in requests[0]["tools"]}
        self.assertTrue({"find_files", "edit_file", "restore_file", "run_command"} <= definitions)
        results = [json.loads(message["content"]) for message in requests[-1]["messages"] if message["role"] == "tool"]
        self.assertEqual(results[1]["reference"], "app.py:1")
        self.assertIn("backup_id", results[2])
        self.assertEqual(results[3]["returncode"], 0)
        self.assertEqual(results[3]["output"].strip(), "2")
        self.assertIn("[差异]", self.output.getvalue())
        self.assertIn("[命令输出]", self.output.getvalue())
        self.assertEqual(len(SessionStore(self.workspace).latest()["messages"]), 12)

    def test_model_cannot_enable_disabled_write_tool(self):
        code, requests = self.run_main(
            ["你好", "--workspace", str(self.workspace), "--banner", "off"],
            [reply(calls=[tool_call(name="edit_file", arguments=json.dumps({
                "path": "new.py", "old_text": "", "new_text": "unexpected"}))]), reply("编辑未开启")])
        self.assertEqual(code, 0)
        self.assertFalse((self.workspace / "new.py").exists())
        self.assertNotIn("edit_file", {item["function"]["name"] for item in requests[0]["tools"]})
        self.assertFalse(json.loads(requests[1]["messages"][-1]["content"])["ok"])

    def test_edit_receipt_survives_model_failure(self):
        path = self.workspace / "app.py"
        path.write_text("old", encoding="utf-8")
        code, requests = self.run_main(
            ["修改", "--workspace", str(self.workspace), "--allow-write", "--yes", "--banner", "off"],
            [reply(calls=[tool_call(name="edit_file", arguments=json.dumps({
                "path": "app.py", "old_text": "old", "new_text": "new"}))]), reply("unfinished", finish="length")])
        self.assertEqual(code, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "new")
        self.assertEqual(len(list((self.workspace / ".revocli/backups").glob("*.json"))), 1)
        self.assertIn("备份 ID", self.output.getvalue())
        self.assertEqual(list(SessionStore(self.workspace).sessions_dir.glob("*.json")), [])

    def test_interactive_multi_turn_and_remember_command(self):
        prompts = ["/remember preference 项目使用 uv 管理依赖", "第一问", "第二问", "/exit"]
        with patch.object(TerminalUI, "read_prompt", side_effect=prompts):
            code, requests = self.run_main(
                ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
                [reply("回答一"), reply("回答二")],
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        self.assertIn("项目使用 uv 管理依赖", requests[0]["messages"][0]["content"])
        second_turn = [message.get("content") for message in requests[1]["messages"]]
        self.assertIn("第一问", second_turn)
        self.assertIn("回答一", second_turn)
        self.assertIn("第二问", second_turn)
        self.assertTrue((self.workspace / ".revocli" / "memory.json").is_file())

    def test_history_trim_does_not_start_with_orphan_tool_message(self):
        messages = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
            {"role": "assistant", "content": "done"},
        ]
        self.assertEqual(select_history(messages, max_messages=2), [])

    def test_long_conversation_keeps_full_archive_while_context_is_bounded(self):
        store = SessionStore(self.workspace)
        session = store.new_session("test-model")
        session["messages"] = [message for number in range(41) for message in (
            {"role": "user", "content": f"问题{number}"}, {"role": "assistant", "content": f"回答{number}"})]
        store.save(session)
        code, requests = self.run_main(
            ["新问题", "--continue", "--no-auto-compact", "--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
            [reply("新回答")])
        self.assertEqual(code, 0)
        saved = store.load(session["id"])
        self.assertEqual(len(saved["messages"]), 84)
        self.assertEqual(saved["messages"][0]["content"], "问题0")
        self.assertNotIn("问题0", [message.get("content") for message in requests[0]["messages"]])

    def test_empty_exit_creates_no_session_and_sessions_command_uses_no_model(self):
        with patch.object(TerminalUI, "read_prompt", side_effect=["/sessions", "/exit"]):
            code, requests = self.run_main(["--workspace", str(self.workspace), "--banner", "off"], [])
        self.assertEqual(code, 0)
        self.assertEqual(requests, [])
        self.assertEqual(list(SessionStore(self.workspace).sessions_dir.glob("*.json")), [])

    def test_memory_update_and_omission_are_visible(self):
        store = SessionStore(self.workspace)
        store.add_memory("A" * 2000, kind="preference")
        item = store.add_memory("B" * 2000, kind="preference")
        with patch.object(TerminalUI, "read_prompt", side_effect=[
            f"/memory-update {item['id']} 最新事实", "/sessions", "你好", "/exit"]):
            code, requests = self.run_main(["--workspace", str(self.workspace), "--banner", "off"], [reply("你好")])
        self.assertEqual(code, 0)
        self.assertIn("最新事实", requests[0]["messages"][0]["content"])
        self.assertIn("已更新记忆", self.output.getvalue())
        store.add_memory("C" * 2000, kind="preference")
        self.run_main(["你好", "--workspace", str(self.workspace), "--banner", "off"], [reply("你好")])
        self.assertIn("省略 1 条", self.output.getvalue())

    def test_session_options_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), patch("revocli.cli.OpenAI") as client:
            main(["--continue", "--session", "test"])
        client.assert_not_called()

    def test_invalid_budget_flags_stop_before_model(self):
        for argument, value in [("--max-tool-calls", "0"), ("--max-tool-bytes", "1"),
                                ("--context-bytes", "0"), ("--max-seconds", "nan")]:
            with self.subTest(argument=argument), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit), patch("revocli.cli.OpenAI") as client:
                main(["你好", argument, value])
            client.assert_not_called()

    def test_single_turn_displays_answer_when_save_fails(self):
        with patch.object(SessionStore, "save", side_effect=PermissionError("offline save error")):
            code, requests = self.run_main(
                ["第一问", "--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
                [reply("已经生成的回答")],
            )
        self.assertEqual(code, 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("已经生成的回答", self.output.getvalue())
        self.assertIn("会话未保存", self.errors.getvalue())

    def test_interactive_save_retry_does_not_repeat_model_request(self):
        real_save = SessionStore.save
        attempts = 0

        def fail_first_save(store, session):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("offline save error")
            return real_save(store, session)

        with patch.object(SessionStore, "save", new=fail_first_save), \
                patch.object(TerminalUI, "read_prompt", side_effect=["第一问", "/save", "第二问", "/exit"]):
            code, requests = self.run_main(
                ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
                [reply("第一答"), reply("第二答")],
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        self.assertIn("第一答", self.output.getvalue())
        self.assertIn("会话未保存", self.errors.getvalue())
        self.assertIn("会话已保存", self.output.getvalue())
        self.assertIn("第一答", [message.get("content") for message in requests[1]["messages"]])
        saved = SessionStore(self.workspace).latest()
        self.assertEqual([message["content"] for message in saved["messages"]], ["第一问", "第一答", "第二问", "第二答"])

    def test_failed_new_or_exit_keeps_current_session(self):
        real_save = SessionStore.save
        for command in ["/new", "/exit"]:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                attempts = 0

                def fail_command_save(store, session):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 2:
                        raise PermissionError("offline command save error")
                    return real_save(store, session)

                with patch.object(SessionStore, "save", new=fail_command_save), \
                        patch.object(TerminalUI, "read_prompt", side_effect=["第一问", command, "/save", "第二问", "/exit"]):
                    code, requests = self.run_main(
                        ["--workspace", folder, "--banner", "off", "--color", "never"],
                        [reply("第一答"), reply("第二答")],
                    )
                self.assertEqual(code, 0)
                self.assertEqual(len(requests), 2)
                self.assertIn("第一答", [message.get("content") for message in requests[1]["messages"]])
                store = SessionStore(folder)
                self.assertEqual(len(store.list_sessions()), 1)
                self.assertEqual(len(store.latest()["messages"]), 4)

    def test_invalid_session_stops_before_model_request(self):
        store = SessionStore(self.workspace)
        store.path_for("broken").write_text('{"messages": []}', encoding="utf-8")
        with self.assertRaises(SystemExit) as error:
            self.run_main(
                ["下一问", "--session", "broken", "--workspace", str(self.workspace), "--banner", "off"], [],
            )
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.requests, [])
        self.assertIn("会话文件格式不正确", self.errors.getvalue())

    def test_store_initialization_error_has_no_traceback(self):
        with patch("revocli.cli.SessionStore", side_effect=PermissionError("offline init error")), \
                self.assertRaises(SystemExit) as error:
            self.run_main(["你好", "--workspace", str(self.workspace), "--banner", "off"], [])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.requests, [])
        self.assertIn("offline init error", self.errors.getvalue())

    def test_session_restore_io_errors_stop_before_model_request(self):
        for method, arguments in [("load", ["--session", "example"]), ("latest", ["--continue"])]:
            with self.subTest(method=method), \
                    patch.object(SessionStore, method, side_effect=PermissionError("offline restore error")), \
                    self.assertRaises(SystemExit) as error:
                self.run_main(["你好", "--workspace", str(self.workspace), "--banner", "off", *arguments], [])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(self.requests, [])
            self.assertIn("offline restore error", self.errors.getvalue())
            self.assertNotIn("Traceback", self.errors.getvalue())

    def test_input_end_reports_failure_when_conversation_cannot_be_saved(self):
        for end in [EOFError, KeyboardInterrupt]:
            with self.subTest(end=end.__name__), \
                    patch.object(TerminalUI, "read_prompt", side_effect=["第一问", end]), \
                    patch.object(SessionStore, "save", side_effect=PermissionError("offline save error")):
                code, requests = self.run_main(
                    ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
                    [reply("已经显示的回答")],
                )
            self.assertEqual(code, 1)
            self.assertEqual(len(requests), 1)
            self.assertIn("已经显示的回答", self.output.getvalue())
            self.assertIn("会话未保存", self.errors.getvalue())
            self.assertIn("/save", self.output.getvalue())
            self.assertEqual(list((self.workspace / ".revocli" / "sessions").glob("*.json")), [])

    def test_interrupt_during_final_save_is_not_success(self):
        with patch.object(TerminalUI, "read_prompt", side_effect=["第一问", EOFError]), \
                patch.object(SessionStore, "save", side_effect=KeyboardInterrupt):
            code, requests = self.run_main(
                ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"], [reply("回答")],
            )
        self.assertEqual(code, 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("保存已取消", self.errors.getvalue())

    def test_corrupt_memory_commands_keep_interactive_loop_running(self):
        store = SessionStore(self.workspace)
        original = b"{broken memory"
        store.memory_path.write_bytes(original)
        with patch.object(TerminalUI, "read_prompt", side_effect=["/memory", "/remember hi", "/forget all", "你好", "/exit"]):
            code, requests = self.run_main(
                ["--workspace", str(self.workspace), "--no-memory", "--banner", "off", "--color", "never"],
                [reply("可以继续提问")],
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 1)
        self.assertIn("可以继续提问", self.output.getvalue())
        self.assertEqual(self.errors.getvalue().count("命令执行失败"), 3)
        self.assertEqual(store.memory_path.read_bytes(), original)

    def test_failed_model_turn_does_not_leak_partial_tool_history(self):
        (self.workspace / "README.md").write_text("offline content", encoding="utf-8")
        prompts = ["第一问", "失败的问题", "继续提问", "/exit"]
        with patch.object(TerminalUI, "read_prompt", side_effect=prompts):
            code, requests = self.run_main(
                ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"],
                [reply("第一答"), reply(calls=[tool_call()]), reply("不完整回答", finish="length"), reply("继续回答")],
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 4)
        self.assertEqual([message.get("content") for message in requests[-1]["messages"]][1:],
                         ["第一问", "第一答", "继续提问"])
        saved = SessionStore(self.workspace).latest()
        self.assertEqual([message["content"] for message in saved["messages"]], ["第一问", "第一答", "继续提问", "继续回答"])


if __name__ == "__main__":
    unittest.main()
