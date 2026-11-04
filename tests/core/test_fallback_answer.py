"""收尾请求发不出去时，结束说明里的改动文件必须来自工具实际结果。"""

import json
from pathlib import Path
import tempfile
import unittest

import httpx2
from openai import OpenAI

from recapcli.core.agent import _edited_paths, run_agent
from recapcli.tools.approval import approve_all, fail_closed


def tool_reply(name, arguments):
    message = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}]}


class FallbackAnswerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        # 参数远超任务记录里的截断长度，这正是旧实现漏报的情形。
        self.old_text = "x" * 400
        (self.workspace / "long.txt").write_text(self.old_text, encoding="utf-8")

    def run_edit(self, approver):
        arguments = {"path": "long.txt", "old_text": self.old_text, "new_text": "y"}
        # 第二次是最后一次请求：模型仍要调用工具且没有正文，只能用事实拼结束说明。
        replies = [tool_reply("edit_file", arguments), tool_reply("edit_file", arguments)]
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(200, json=replies[len(requests) - 1])

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with client:
            return run_agent(client, "test-model", "修改", self.workspace, max_steps=2,
                             capabilities=("read", "write"), approver=approver, on_event=lambda event: None)

    def test_long_edit_is_listed(self):
        result = self.run_edit(approve_all)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("编辑过的文件：long.txt", result.answer)

    def test_rejected_edit_is_not_listed(self):
        result = self.run_edit(fail_closed)
        self.assertEqual(result.status, "incomplete")
        self.assertNotIn("编辑过的文件", result.answer)
        self.assertEqual((self.workspace / "long.txt").read_text(encoding="utf-8"), self.old_text)

    def test_result_shapes(self):
        partial = {"ok": False, "files": [{"path": "a.py", "changed": True}, {"path": "b.py", "changed": False}]}
        self.assertEqual(_edited_paths("edit_files", partial), ["a.py"])
        self.assertEqual(_edited_paths("undo_edit", {"files": [{"path": "c.py", "restored": True}]}), ["c.py"])
        self.assertEqual(_edited_paths("restore_file", {"path": "d.py", "restored": True}), ["d.py"])
        self.assertEqual(_edited_paths("edit_file", {"ok": False, "status": "rejected"}), [])
        self.assertEqual(_edited_paths("read_file", {"path": "e.py", "changed": True}), [])


if __name__ == "__main__":
    unittest.main()
