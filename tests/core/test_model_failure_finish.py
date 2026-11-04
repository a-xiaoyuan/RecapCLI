"""工具已执行后，上下文超限和输出截断转入收尾；其他模型错误仍按失败处理。"""

import tempfile
import unittest

import httpx2
from openai import AuthenticationError, BadRequestError, OpenAI

from recapcli.core.agent import run_agent


def completion(message, finish_reason):
    return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
                                      "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]})


def tool_call(name="list_directory", finish_reason="tool_calls"):
    return completion({"role": "assistant", "content": None, "tool_calls": [{
        "id": "call", "type": "function", "function": {"name": name, "arguments": "{}"}}]}, finish_reason)


def text(content, finish_reason="stop"):
    return completion({"role": "assistant", "content": content}, finish_reason)


def api_error(status, message, code=None):
    return httpx2.Response(status, json={"error": {"message": message, "code": code}})


OVERFLOW = api_error(400, "This model's maximum context length is 8192 tokens.", "context_length_exceeded")


class ModelFailureFinishTests(unittest.TestCase):
    def run_turn(self, *responses):
        self.requests = 0
        self.events = []
        self.traces = []

        def handler(request):
            self.requests += 1
            return responses[self.requests - 1]

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with tempfile.TemporaryDirectory() as workspace, client:
            return run_agent(client, "test-model", "看目录", workspace, on_event=self.events.append,
                             on_trace=lambda kind, data: self.traces.append((kind, data)))

    def executed_tools(self):
        return [data["name"] for kind, data in self.traces if kind == "tool_result"]

    def test_context_overflow_after_tools_requests_final_summary(self):
        result = self.run_turn(tool_call(), OVERFLOW, text("目录已看完，分析未完成。"))
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.answer, "目录已看完，分析未完成。")
        self.assertIn("服务端报告上下文超限", result.stop_reason)
        self.assertEqual(self.requests, 3)

    def test_repeated_context_overflow_falls_back_to_recorded_facts(self):
        result = self.run_turn(tool_call(), OVERFLOW, OVERFLOW)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("服务端报告上下文超限", result.answer)
        self.assertIn("已调用 1 次工具", result.answer)
        self.assertTrue(any(kind == "final_summary_failed" for kind, _ in self.traces))

    def test_context_overflow_message_without_code_is_recognized(self):
        response = api_error(400, "Input is too long for requested model: prompt is too long")
        result = self.run_turn(tool_call(), response, text("已收尾"))
        self.assertEqual(result.status, "incomplete")

    def test_context_overflow_before_any_tool_stays_an_error(self):
        with self.assertRaises(BadRequestError):
            self.run_turn(OVERFLOW)

    def test_other_model_errors_after_tools_stay_errors(self):
        with self.assertRaises(BadRequestError):
            self.run_turn(tool_call(), api_error(400, "unsupported parameter: temperature"))
        with self.assertRaises(AuthenticationError):
            self.run_turn(tool_call(), api_error(401, "invalid api key"))

    def test_length_after_tools_requests_final_summary(self):
        result = self.run_turn(tool_call(), text("写到一半", "length"), text("已收尾"))
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.answer, "已收尾")
        self.assertIn("模型输出达到长度上限", result.stop_reason)

    def test_truncated_tool_call_is_not_executed(self):
        result = self.run_turn(tool_call(), tool_call("read_file", "length"), text("已收尾"))
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.executed_tools(), ["list_directory"])

    def test_length_before_any_tool_stays_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "模型响应未完整结束：length"):
            self.run_turn(text("写到一半", "length"))

    def test_content_filter_after_tools_stays_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "模型响应未完整结束：content_filter"):
            self.run_turn(tool_call(), text("", "content_filter"))


if __name__ == "__main__":
    unittest.main()
