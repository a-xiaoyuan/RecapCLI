"""复现 DeepSeek 评测只读不写：连续读取把可写任务收成无工具答复。

假模型按 deepseek-chat 的 chat_completions 返回 reasoning_content 和原生 tool_calls。
它会服从「先总结 / 不要调用任何工具 / tool_choice=none」，否则按读、记笔记、再改文件的顺序走。
"""

import json
from pathlib import Path
import tempfile
import unittest

import httpx2
from openai import OpenAI

from recapcli.config import Config
from recapcli.core.agent import run_agent
from recapcli.llm.client import ModelClient
from recapcli.tools.approval import approve_all


def deepseek(content, calls=None, reasoning="先读代码，确认改动点后再写入。"):
    message = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    if calls:
        message["tool_calls"] = calls
    return {"id": "ds", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}]}


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


# 这些字样来自连续读取的提醒和强制收尾；出现时 DeepSeek 会停下来用文字回答。
STOP_MARKERS = ("现在停止读取", "不要调用任何工具", "先基于现有事实总结")


def told_to_stop(body):
    if body.get("tool_choice") == "none":
        return True
    for message in body["messages"]:
        text = message.get("content")
        if isinstance(text, str) and any(marker in text for marker in STOP_MARKERS):
            return True
    return False


class DeepSeekEvalEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.requests = []

    def run_deepseek(self, respond, **options):
        def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            return httpx2.Response(200, json=respond(body, len(self.requests)))

        def factory(**sdk_options):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **sdk_options)

        config = Config("offline-key", "deepseek-chat", "https://api.deepseek.com/v1",
                        protocol="chat_completions", streaming=False, tools=True)
        with ModelClient(config, client_factory=factory) as client:
            return run_agent(client, "deepseek-chat", "把 app.py 的 value 改成 2，并说明改动。",
                             self.workspace, capabilities=("read", "write", "exec"), approver=approve_all,
                             mode="react", on_event=lambda _event: None, **options)

    def tool_names(self, result):
        return [item["function"]["name"] for message in result.messages for item in message.get("tool_calls") or []]

    def test_reads_and_notes_still_reach_the_edit(self):
        # 评测里的轨迹：先读、用 todo_write 做笔记，接着还要改文件。
        # 修之前，第 4 轮后的「先总结」或第 8 轮后的无工具收尾会让它回答「改动尚未开始」。
        def respond(body, index):
            if told_to_stop(body):
                return deepseek("已阅读相关代码并做了笔记。改动尚未开始。", reasoning="现在不能调用工具。")
            if index <= 3:
                return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})],
                                reasoning=f"第 {index} 次读取 app.py")
            if index == 4:
                return deepseek(None, [call("c4", "todo_write", {"todos": [
                    {"content": "把 value 改成 2", "status": "pending"}]})], reasoning="先把改动记下来")
            if index <= 8:
                return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})],
                                reasoning=f"再确认原文，第 {index} 次")
            if index == 9:
                return deepseek(None, [call("c9", "edit_file", {
                    "path": "app.py", "old_text": "value = 1\n", "new_text": "value = 2\n"})],
                    reasoning="信息够了，调用 edit_file。<tool_call>edit_file</tool_call>")
            return deepseek("已把 app.py 的 value 从 1 改成 2。")

        result = self.run_deepseek(respond, max_steps=16)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "value = 2\n")
        self.assertNotIn("改动尚未开始", result.answer)
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.tool_names(result).count("read_file"), 7)
        self.assertIn("todo_write", self.tool_names(result))
        self.assertIn("edit_file", self.tool_names(result))
        edit_request = self.requests[8]
        self.assertNotEqual(edit_request.get("tool_choice"), "none")
        replayed = [message.get("reasoning_content") for message in edit_request["messages"]]
        self.assertIn("先把改动记下来", replayed)
        self.assertNotIn("当前阶段：制定计划", edit_request["messages"][0]["content"])

    def test_reasoning_content_does_not_drop_tool_call_and_limit_returns_after_edit(self):
        # 思考正文里带工具标记，同时有原生 tool_calls：写入必须执行，不能当成文本工具调用丢掉。
        # 一旦已经写入，连续只读仍然要在 8 轮后收尾，避免读完不再结束。
        def respond(body, index):
            if body.get("tool_choice") == "none":
                return deepseek("读取后的总结。")
            if index == 1:
                return deepseek(None, [call("c1", "edit_file", {
                    "path": "app.py", "old_text": "value = 1\n", "new_text": "value = 2\n"})],
                    reasoning="直接改。<｜｜DSML｜｜tool_calls>")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})], reasoning="再读一遍")

        result = self.run_deepseek(respond, max_steps=20)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(result.answer, "读取后的总结。")
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.tool_names(result).count("read_file"), 8)
        self.assertEqual(len(self.requests), 10)
        self.assertEqual(self.requests[-1].get("tool_choice"), "none")
        echoed = next(message for message in self.requests[1]["messages"] if message.get("tool_calls"))
        self.assertEqual(echoed["tool_calls"][0]["function"]["name"], "edit_file")
        self.assertIn("DSML", echoed["reasoning_content"])
        self.assertNotIn("DSML", result.answer)
