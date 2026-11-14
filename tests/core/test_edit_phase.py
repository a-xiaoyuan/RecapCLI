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

    def test_understanding_tasks_keep_full_tools_and_summarize_without_edit_nags(self):
        """关掉写入阶段的理解题按只读处理：不催促修改，工具不收窄，第 8 轮转入总结。"""
        def respond(body, index):
            texts = "\n".join(system_texts(body))
            if body.get("tool_choice") == "none" or "现在停止读取" in texts:
                return deepseek("这是理解题，不修改文件。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond, allow_edit_phase=False)
        self.assertEqual(result.answer, "这是理解题，不修改文件。")
        self.assertEqual(result.status, "returned")
        self.assertEqual(len(self.requests), 9)
        self.assertEqual(self.requests[-1].get("tool_choice"), "none")
        for body in self.requests[:-1]:
            self.assertIn("search_text", tool_names(body))
        texts = [text for body in self.requests for text in system_texts(body)]
        self.assertFalse(any("下一步请调用 edit_file" in text for text in texts))
        self.assertFalse(any("尚未写入文件" in text for text in texts))
        self.assertEqual([event for event in self.events if "请开始修改" in event], [])
        self.assertIn("[提醒] 已连续读取 4 轮，请先总结再继续", self.events)

    def tool_results(self):
        """最后一次请求里每个工具调用 ID 对应的结果。"""
        return {message["tool_call_id"]: json.loads(message["content"])
                for message in self.requests[-1]["messages"] if message.get("role") == "tool"}

    def test_tools_dropped_from_the_request_are_not_executed(self):
        """收窄后模型仍调用列表外的搜索工具：不执行，回填错误并保持调用配对。"""
        (self.workspace / "notes.txt").write_text("needle\n", encoding="utf-8")

        def respond(body, index):
            if index == 10:
                return deepseek(None, [call("search", "search_text", {"query": "needle"}),
                                       call("read", "read_file", {"path": "app.py"})])
            if index > 10:
                return deepseek("没有修改。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        self.run_turn(respond)
        self.assertNotIn("search_text", tool_names(self.requests[9]))
        results = self.tool_results()
        self.assertFalse(results["search"]["ok"])
        self.assertEqual(results["search"]["status"], "tool_not_offered")
        self.assertIn("search_text", results["search"]["error"])
        self.assertNotIn("matches", results["search"])
        self.assertTrue(results["read"].get("ok", True))
        self.assertNotIn("tool_not_offered", json.dumps(results["read"], ensure_ascii=False))

    def test_offered_tools_still_run_normally(self):
        def respond(body, index):
            if index == 1:
                return deepseek(None, [call("search", "search_text", {"query": "value"})])
            return deepseek("找到了。")

        result = self.run_turn(respond)
        self.assertEqual(result.status, "returned")
        found = self.tool_results()["search"]
        self.assertTrue(found["ok"])
        self.assertNotEqual(found.get("status"), "tool_not_offered")

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

    # 重复调用被拒的工具要及时收尾；没写文件却声称改好了，要如实标注。

    def test_repeated_rejected_calls_switch_to_final_summary(self):
        def respond(body, index):
            if body.get("tool_choice") == "none":
                return deepseek("还没有修改。")
            if index > 8:
                return deepseek(None, [call(f"c{index}", "search_text", {"query": "value"})])
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond)
        self.assertEqual(len(self.requests), 12)
        self.assertEqual(self.requests[-1].get("tool_choice"), "none")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("连续 3 次调用本轮未提供的工具", result.stop_reason)
        self.assertIn("[收尾] 模型连续 3 次调用本轮未提供的工具，转入最终汇总", self.events)

    def test_one_rejected_round_does_not_stop_the_turn(self):
        def respond(body, index):
            if index == 9:
                return deepseek(None, [call("s", "search_text", {"query": "value"})])
            if index == 10:
                return deepseek(None, [call("e", "edit_file", {"path": "app.py", "old_text": "value = 1",
                                                               "new_text": "value = 2"})])
            if index > 10:
                return deepseek("已修改 app.py。")
            return deepseek(None, [call(f"c{index}", "read_file", {"path": "app.py"})])

        result = self.run_turn(respond)
        self.assertEqual(result.status, "returned")
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "value = 2\n")
        self.assertNotIn("[核对]", result.answer)
        self.assertNotIn("未通过编辑工具写入", result.stop_reason)

    def test_claimed_changes_without_writes_are_flagged(self):
        def respond(body, index):
            if index == 1:
                return deepseek(None, [call("r", "read_file", {"path": "app.py"})])
            return deepseek("已完成修改，改动如下：把 value 改成了 2。")

        result = self.run_turn(respond)
        self.assertEqual(result.status, "returned")
        self.assertTrue(result.answer.startswith("已完成修改"))
        self.assertTrue(result.answer.endswith("上面描述的改动并未写入工作区。"))
        self.assertIn("回答声称已修改，但本轮未通过编辑工具写入文件", result.stop_reason)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_plain_answers_and_understanding_tasks_are_not_flagged(self):
        def respond(body, index):
            return deepseek("value 当前是 1，不需要修改。")

        self.assertNotIn("[核对]", self.run_turn(respond).answer)
        self.requests.clear()

        def claim(body, index):
            return deepseek("已完成分析，链路如下。")

        result = self.run_turn(claim, allow_edit_phase=False)
        self.assertNotIn("[核对]", result.answer)
        result = self.run_turn(claim, read_only=True)
        self.assertNotIn("[核对]", result.answer)
