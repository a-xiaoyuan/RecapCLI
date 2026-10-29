from copy import deepcopy
import json
import unittest

import httpx2
from openai import OpenAI

from recapcli.llm import assistant_record, complete, portable_messages, validate_provider_state


TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "读取", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}}]


def response_items(*items, status="completed", usage=True):
    result = {"id": "resp-test", "object": "response", "created_at": 1, "model": "model-test",
              "status": status, "output": list(items)}
    if usage:
        result["usage"] = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                           "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0},
                           "output_tokens_details": {"reasoning_tokens": 1}}
    return result


def message(text="完成", phase=None):
    result = {"type": "message", "id": "msg-1", "status": "completed", "role": "assistant",
              "content": [{"type": "output_text", "text": text, "annotations": []}]}
    if phase:
        result["phase"] = phase
    return result


def call(call_id="call-1"):
    return {"type": "function_call", "id": "fc-1", "call_id": call_id,
            "name": "read_file", "arguments": '{"path":"README.md"}', "status": "completed"}


def reasoning():
    return {"type": "reasoning", "id": "rs-1", "summary": [{"type": "summary_text", "text": "private summary"}],
            "content": [{"type": "reasoning_text", "text": "private reasoning"}], "encrypted_content": "opaque-state"}


class LLMTests(unittest.TestCase):
    def client(self, replies):
        requests = []

        def handler(request):
            requests.append((request.url.path, json.loads(request.content)))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        sdk = OpenAI(api_key="offline-key", base_url="https://offline.invalid/v1", max_retries=0,
                     http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        self.addCleanup(sdk.close)
        return sdk, requests

    def test_chat_keeps_tool_protocol_and_strips_private_fields(self):
        sdk, requests = self.client([{"id": "chat-1", "object": "chat.completion", "created": 1,
                                      "model": "model-test", "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": "好"}}]}])
        messages = [{"role": "user", "content": "读取"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"}}], "provider_state": {"secret": "drop"}},
                    {"role": "tool", "content": "正文", "tool_call_id": "call-1"}]
        before = deepcopy(messages)
        result = complete(sdk, "chat_completions", model="model-test", messages=messages, tools=TOOLS,
                          tools_allowed=False, origin="chat")
        self.assertEqual(result.choices[0].message.content, "好")
        self.assertEqual(requests[0][0], "/v1/chat/completions")
        self.assertNotIn("provider_state", requests[0][1]["messages"][1])
        self.assertEqual(requests[0][1]["tool_choice"], "none")
        self.assertEqual(requests[0][1]["messages"][-1]["tool_call_id"], "call-1")
        self.assertEqual(messages, before)

    def test_responses_roundtrip_reasoning_phase_usage_and_tool_result(self):
        sdk, requests = self.client([response_items(reasoning(), message("先读取", "commentary"), call()),
                                     response_items(message("完成", "final_answer"))])
        messages = [{"role": "system", "content": "助手"}, {"role": "user", "content": "读取"}]
        first = complete(sdk, "responses", model="model-test", messages=messages, tools=TOOLS, origin="one")
        record = assistant_record(first.choices[0].message)
        self.assertEqual(record["tool_calls"][0]["id"], "call-1")
        state_text = json.dumps(record)
        self.assertNotIn("private summary", state_text)
        self.assertNotIn("private reasoning", state_text)
        self.assertIn("opaque-state", state_text)
        validate_provider_state(record["provider_state"])
        self.assertEqual(first.usage.prompt_tokens, 10)
        self.assertEqual(first.usage.completion_tokens, 4)
        self.assertEqual(first.usage.total_tokens, 14)
        self.assertEqual(first.usage.prompt_tokens_details.cached_tokens, 2)
        messages.extend([record, {"role": "tool", "tool_call_id": "call-1", "content": "源代码"}])
        before = deepcopy(messages)
        second = complete(sdk, "responses", model="model-test", messages=messages, tools=TOOLS,
                          tools_allowed=False, origin="one")
        self.assertEqual(second.choices[0].message.content, "完成")
        self.assertEqual(requests[0][0], "/v1/responses")
        request = requests[1][1]
        self.assertFalse(request["store"])
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])
        self.assertFalse(request["tools"][0]["strict"])
        self.assertEqual(request["tool_choice"], "none")
        self.assertEqual(request["input"][-1], {"type": "function_call_output", "call_id": "call-1", "output": "源代码"})
        self.assertEqual(request["input"][2]["type"], "reasoning")
        self.assertEqual(request["input"][3]["phase"], "commentary")
        self.assertEqual(messages, before)

    def test_switch_origin_keeps_ordinary_history_without_opaque_state(self):
        sdk, requests = self.client([response_items(reasoning(), call()), response_items(message())])
        messages = [{"role": "user", "content": "读代码"}]
        first = complete(sdk, "responses", model="model-test", messages=messages, tools=TOOLS, origin="one")
        messages += [assistant_record(first.choices[0].message),
                     {"role": "tool", "tool_call_id": "call-1", "content": "源代码"},
                     {"role": "assistant", "content": "旧回答"}, {"role": "user", "content": "继续"}]
        before = deepcopy(messages)
        complete(sdk, "responses", model="model-test", messages=messages, origin="two")
        sent = json.dumps(requests[1][1], ensure_ascii=False)
        self.assertNotIn("opaque-state", sent)
        self.assertNotIn('"type": "function_call"', sent)
        self.assertIn("源代码", sent)
        self.assertIn("旧回答", sent)
        self.assertIn("历史工具请求", sent)
        self.assertEqual(messages, before)

    def test_chat_tool_history_is_portable_to_responses(self):
        sdk, requests = self.client([response_items(message())])
        messages = [{"role": "user", "content": "读代码"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "x", "content": "旧结果"},
                    {"role": "assistant", "content": "旧结论"}, {"role": "user", "content": "继续"}]
        complete(sdk, "responses", model="model-test", messages=messages, origin="one")
        self.assertTrue(all("function_call" != item.get("type") for item in requests[0][1]["input"]))
        self.assertIn("旧结果", json.dumps(requests[0][1], ensure_ascii=False))

    def test_incomplete_refusal_unknown_or_malformed_output_is_rejected(self):
        cases = [response_items(message(), status="incomplete"),
                 response_items({"type": "message", "id": "m", "role": "assistant", "status": "completed",
                                 "content": [{"type": "refusal", "refusal": "拒绝"}]}),
                 response_items({"type": "web_search_call", "id": "s", "status": "completed", "action": {"type": "search", "query": "x"}}),
                 response_items({"type": "function_call", "name": "read_file", "arguments": "{}"}),
                 response_items({"type": "reasoning", "id": "r", "summary": []}),
                 response_items(call(), call())]
        for payload in cases:
            with self.subTest(payload=payload):
                sdk, requests = self.client([payload])
                with self.assertRaises((ValueError, RuntimeError)):
                    complete(sdk, "responses", model="model-test", messages=[{"role": "user", "content": "读"}], origin="one")
                self.assertEqual(len(requests), 1)

    def test_usage_absence_stays_unknown_and_no_tools_are_invented(self):
        sdk, requests = self.client([response_items(message(), usage=False)])
        result = complete(sdk, "responses", model="model-test", messages=[{"role": "user", "content": "你好"}],
                          tools_allowed=False, origin="one")
        self.assertIsNone(result.usage)
        self.assertNotIn("tools", requests[0][1])

    def test_summary_projection_and_state_validation(self):
        sdk, _ = self.client([response_items(reasoning(), message())])
        result = complete(sdk, "responses", model="model-test", messages=[{"role": "user", "content": "读"}], origin="one")
        record = assistant_record(result.choices[0].message)
        self.assertNotIn("opaque-state", json.dumps(portable_messages([record])))
        bad = deepcopy(record["provider_state"])
        bad["items"][0]["content"] = "private"
        with self.assertRaises(ValueError):
            validate_provider_state(bad)


if __name__ == "__main__":
    unittest.main()
