"""可写任务一直只读时，提醒只在第 4 轮和第 8 轮各出现一次，之后收窄工具。

假模型和 DeepSeek 评测剧本一样走 chat_completions。能力只有 read，或调用方声明只读时，
仍在第 8 轮强制总结，不把工具收成写入。
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


EDIT_TOOLS = {
    "edit_file", "edit_files", "undo_edit", "rewind_checkpoint", "restore_file",
    "run_command", "command_status", "command_input", "stop_command",
    "read_file", "todo_write",
}
DROPPED = {
    "repository_map", "search_text", "search_symbols", "search_code_index",
    "find_files", "find_references", "list_directory",
}


def deepseek(content, calls=None):
    message = {"role": "assistant", "content": content, "reasoning_content": "继续看代码。"}
    if calls:
        message["tool_calls"] = calls
    return {"id": "ds", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}]}


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def tool_names(body):
    return [item["function"]["name"] for item in body.get("tools") or []]


def system_texts(body):
    return [message.get("content") or "" for message in body["messages"]
            if message.get("role") == "system" and isinstance(message.get("content"), str)]


class EditPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.requests = []
        self.events = []

    def run_turn(self, respond, **options):
        def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            return httpx2.Response(200, json=respond(body, len(self.requests)))

        def factory(**sdk_options):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **sdk_options)

        config = Config("offline-key", "deepseek-chat", "https://api.deepseek.com/v1",
                        protocol="chat_completions", streaming=False, tools=True)
        with ModelClient(config, client_factory=factory) as client:
            return run_agent(client, "deepseek-chat", "把 app.py 的 value 改成 2。",
                             self.workspace, capabilities=options.pop("capabilities", ("read", "write", "exec")),
                             approver=approve_all, mode="react", on_event=self.events.append,
                             max_steps=options.pop("max_steps", 20), **options)

    def test_write_task_reminds_twice_then_narrows_tools_and_allows_text(self):
        state = {"saw_deadline": False}

        def respond(body, index):
            texts = "\n".join(system_texts(body))
            if "下一步请调用 edit_file" in texts:
                state["saw_deadline"] = True
                return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py", "start_line": 1, "end_line": 1})])
            if state["saw_deadline"]:
                return deepseek("确实无法修改：没有可以替换的目标。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond)
        gentle = [text for body in self.requests for text in system_texts(body) if "尚未写入文件" in text]
        deadline = [text for body in self.requests for text in system_texts(body) if "下一步请调用 edit_file" in text]
        self.assertEqual(len(gentle), 1)
        self.assertEqual(len(deadline), 1)
        self.assertIn("app.py", deadline[0])
        self.assertEqual([event for event in self.events if "请开始修改" in event],
                         ["[提醒] 已连续读取 4 轮，请开始修改"])
        self.assertEqual([event for event in self.events if "请改为写入" in event],
                         ["[提醒] 已连续读取 8 轮，请改为写入"])
        deadline_index = next(index for index, body in enumerate(self.requests)
                              if any("下一步请调用 edit_file" in text for text in system_texts(body)))
        self.assertEqual(deadline_index, 8)
        self.assertIn("search_text", tool_names(self.requests[deadline_index - 1]))
        for body in self.requests[deadline_index:]:
            self.assertNotEqual(body.get("tool_choice"), "none")
            self.assertNotEqual(body.get("tool_choice"), "required")
            names = set(tool_names(body))
            self.assertTrue(names <= EDIT_TOOLS, names)
            self.assertTrue({"edit_file", "read_file", "todo_write", "run_command"} <= names)
            self.assertFalse(names & DROPPED)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "value = 1\n")
        self.assertIn("确实无法修改", result.answer)
        self.assertEqual(result.status, "returned")

    def test_read_only_capability_still_forces_summary_at_round_eight(self):
        def respond(body, index):
            texts = "\n".join(system_texts(body))
            if body.get("tool_choice") == "none" or "现在停止读取" in texts:
                return deepseek("基于已读内容的总结。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond, capabilities=("read",))
        self.assertEqual(result.answer, "基于已读内容的总结。")
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.requests[-1].get("tool_choice"), "none")
        closing = "\n".join(message.get("content") or "" for message in self.requests[-1]["messages"]
                            if isinstance(message.get("content"), str))
        self.assertIn("现在停止读取", closing)
        self.assertIn("search_text", tool_names(self.requests[-2]))
        self.assertNotEqual(self.requests[-2].get("tool_choice"), "none")
        self.assertFalse(any("下一步请调用 edit_file" in text for body in self.requests for text in system_texts(body)))
        self.assertEqual(len(self.requests), 9)

    def test_read_only_flag_forces_summary_even_if_write_is_enabled(self):
        def respond(body, index):
            texts = "\n".join(system_texts(body))
            if body.get("tool_choice") == "none" or "现在停止读取" in texts:
                return deepseek("这是只读说明。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond, read_only=True)
        self.assertEqual(result.answer, "这是只读说明。")
        self.assertEqual(result.status, "returned")
        self.assertEqual(self.requests[-1].get("tool_choice"), "none")
        self.assertEqual(len(self.requests), 9)
        narrowed = [body for body in self.requests
                    if body.get("tool_choice") != "none" and "search_text" not in tool_names(body)]
        self.assertEqual(narrowed, [])

    def test_callers_can_keep_understanding_tasks_on_the_full_toolset(self):
        def respond(body, index):
            if index >= 10:
                return deepseek("这是理解题，不修改文件。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond, allow_edit_phase=False)
        self.assertEqual(result.answer, "这是理解题，不修改文件。")
        self.assertIn("search_text", tool_names(self.requests[8]))
        self.assertNotEqual(self.requests[8].get("tool_choice"), "none")
        self.assertFalse(any("下一步请调用 edit_file" in text for body in self.requests for text in system_texts(body)))
        self.assertGreaterEqual(sum("请开始修改" in event for event in self.events), 2)

    def test_low_remaining_budget_narrows_before_round_eight(self):
        def respond(body, index):
            if index >= 4:
                return deepseek("预算不多，先说明还没改。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        self.run_turn(respond, max_steps=8)
        names = set(tool_names(self.requests[2]))
        self.assertTrue(names <= EDIT_TOOLS, names)
        self.assertIn("edit_file", names)
        self.assertNotIn("search_text", names)
        self.assertNotEqual(self.requests[2].get("tool_choice"), "required")
        self.assertNotEqual(self.requests[2].get("tool_choice"), "none")
