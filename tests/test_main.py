import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import OpenAI

from main import MAX_FILE_BYTES, execute_tool, main, run_agent


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

    def run_with_replies(self, replies, max_steps=8):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        with OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
            answer = run_agent(client, "test-model", "读取 README.md", self.workspace,
                               max_steps=max_steps, on_event=lambda event: None)
        return answer, requests

    def test_file_result_is_sent_back_with_matching_call_id(self):
        answer, requests = self.run_with_replies([
            reply(calls=[tool_call()]), reply("使用 python app.py 启动"),
        ])
        self.assertEqual(answer, "使用 python app.py 启动")
        self.assertEqual(len(requests), 2)
        history = requests[1]["messages"]
        self.assertEqual(history[-2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(history[-1]["tool_call_id"], "call-1")
        self.assertEqual(json.loads(history[-1]["content"]), {"ok": True, "content": "启动：python app.py"})

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

    def test_tool_cannot_read_archer_credentials(self):
        local_config = self.workspace / ".env"
        local_config.write_text("OPENAI_API_KEY=private-test-key\n", encoding="utf-8")
        with patch("main.ENV_FILE", local_config):
            result = execute_tool("read_file", '{"path":".env"}', self.workspace)
        self.assertFalse(result["ok"])
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_missing_configuration_stops_before_model_request(self):
        with patch.dict("os.environ", {}, clear=True), patch("main.dotenv_values", return_value={}), patch("main.OpenAI") as client:
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
            with patch("main.ENV_FILE", app_config), patch("main.OpenAI", side_effect=client_factory):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = main(["你好", "--workspace", str(self.workspace)])
        self.assertEqual(result, 0)
        self.assertEqual(requests[0].url.host, "app.local.invalid")
        self.assertEqual(requests[0].headers["authorization"], "Bearer environment-test-key")
        self.assertEqual(json.loads(requests[0].content)["model"], "local-model")


if __name__ == "__main__":
    unittest.main()
