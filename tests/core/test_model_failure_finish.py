"""工具已执行后，5xx、断连、超时、上下文超限和输出截断转入收尾；其他模型错误仍按失败处理。"""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import (APIConnectionError, APITimeoutError, AuthenticationError, BadRequestError,
                    InternalServerError, OpenAI)

from recapcli.core.agent import AgentLimits, run_agent
from recapcli.tools.retrieval import _connect


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
    def run_turn(self, *responses, max_steps=None, max_retries=0):
        self.requests = 0
        self.events = []
        self.traces = []

        def handler(request):
            self.requests += 1
            response = responses[self.requests - 1]
            if response == "timeout":
                raise httpx2.ReadTimeout("offline timeout", request=request)
            if response == "disconnect":
                raise httpx2.ConnectError("offline disconnect", request=request)
            return response

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with tempfile.TemporaryDirectory() as workspace, client:
            return run_agent(client, "test-model", "看目录", workspace, on_event=self.events.append,
                             max_steps=max_steps, limits=AgentLimits(max_retries=max_retries),
                             on_trace=lambda kind, data: self.traces.append((kind, data)))

    def executed_tools(self):
        return [data["name"] for kind, data in self.traces if kind == "tool_result"]

    def test_tool_call_text_is_not_returned_as_answer(self):
        marker = '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="read_file">\n</｜｜DSML｜｜tool_calls>'
        result = self.run_turn(text(marker), max_steps=1)
        self.assertEqual(result.status, "incomplete")
        self.assertNotIn("DSML", result.answer)
        self.assertIn("工具调用文本", result.stop_reason)

    def test_consecutive_reads_force_early_summary(self):
        result = self.run_turn(*([tool_call()] * 8), text("已根据读取结果总结。"))
        self.assertEqual(result.status, "returned")
        self.assertEqual(result.answer, "已根据读取结果总结。")
        self.assertEqual(self.requests, 9)
        self.assertTrue(any(kind == "read_summary_reminder" for kind, _ in self.traces))

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

    def test_timeout_after_tools_requests_summary_without_replaying_tools(self):
        result = self.run_turn(tool_call(), "timeout", text("目录已看，分析未完成"))
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.answer, "目录已看，分析未完成")
        self.assertIn("模型请求超时", result.stop_reason)
        self.assertEqual(self.executed_tools(), ["list_directory"])
        final = [data for kind, data in self.traces if kind == "model_request"][-1]
        self.assertFalse(final["tools_allowed"])

    def test_summary_timeout_falls_back_to_facts(self):
        result = self.run_turn(tool_call(), "timeout", "timeout")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("模型请求超时", result.answer)
        self.assertIn("已调用 1 次工具", result.answer)
        self.assertEqual(self.executed_tools(), ["list_directory"])

    def test_timeout_before_any_tool_stays_an_error(self):
        with self.assertRaises(APITimeoutError):
            self.run_turn("timeout")

    def test_timeout_on_last_request_does_not_exceed_request_budget(self):
        result = self.run_turn(tool_call(), "timeout", max_steps=2)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.requests, 2)
        self.assertIn("已调用 1 次工具", result.answer)

    def test_timeout_retries_and_summary_share_the_request_budget(self):
        result = self.run_turn(tool_call(), "timeout", "timeout", text("已收尾"), max_steps=4, max_retries=1)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.requests, 4)
        self.assertEqual(self.executed_tools(), ["list_directory"])

    def test_server_errors_after_tools_request_summary_without_replaying_tools(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                result = self.run_turn(tool_call(), api_error(status, "unavailable"), text("目录已看，分析未完成"))
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.answer, "目录已看，分析未完成")
                self.assertIn(f"模型服务返回 HTTP {status}", result.stop_reason)
                self.assertEqual(self.executed_tools(), ["list_directory"])

    def test_disconnect_after_tools_requests_summary(self):
        result = self.run_turn(tool_call(), "disconnect", text("已收尾"))
        self.assertEqual(result.status, "incomplete")
        self.assertIn("模型连接中断", result.stop_reason)
        self.assertEqual(self.executed_tools(), ["list_directory"])

    def test_summary_failure_after_server_error_falls_back_to_facts(self):
        result = self.run_turn(tool_call(), api_error(502, "bad gateway"), "disconnect")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("模型服务返回 HTTP 502", result.answer)
        self.assertIn("已调用 1 次工具", result.answer)

    def test_server_error_retries_share_the_request_budget(self):
        result = self.run_turn(tool_call(), api_error(502, "x"), api_error(502, "x"), text("已收尾"),
                               max_steps=4, max_retries=1)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(self.requests, 4)
        self.assertEqual(self.executed_tools(), ["list_directory"])

    def test_service_failure_before_any_tool_stays_an_error(self):
        with self.assertRaises(InternalServerError):
            self.run_turn(api_error(502, "bad gateway"))
        with self.assertRaises(APIConnectionError):
            self.run_turn("disconnect")

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

    def test_repository_map_database_error_does_not_stop_the_turn(self):
        failure = sqlite3.OperationalError("unable to open database file")
        with patch("recapcli.tools.code_context.repository_map", side_effect=failure):
            result = self.run_turn(text("你好"))
        self.assertEqual(result.status, "returned")
        context = next(data for kind, data in self.traces if kind == "repository_context")
        self.assertEqual(context["error"], "OperationalError")

    def test_retrieval_index_keeps_temporary_data_in_memory(self):
        with tempfile.TemporaryDirectory() as workspace:
            connection = _connect(Path(workspace).resolve())
            try:
                self.assertEqual(connection.execute("PRAGMA temp_store").fetchone()[0], 2)
            finally:
                connection.close()

    def test_content_filter_after_tools_stays_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "模型响应未完整结束：content_filter"):
            self.run_turn(tool_call(), text("", "content_filter"))


if __name__ == "__main__":
    unittest.main()
