"""结构化事件：文本与旧事件逐字相同，同时带 kind / data / agent，并能 JSON 往返。"""

import json
from pathlib import Path
import tempfile
import unittest

import httpx2
from openai import OpenAI

from recapcli.config import Config
from recapcli.core import events
from recapcli.core.agent import run_agent
from recapcli.core.events import UiEvent, tag_role
from recapcli.llm.client import ModelClient
from recapcli.tools.approval import approve_all


def reply(content, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}]}


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class UiEventTests(unittest.TestCase):
    def test_text_matches_legacy_strings(self):
        self.assertEqual(events.tool_start("read_file", "a.py"), "[工具] read_file · a.py")
        self.assertEqual(events.tool_start("run_command"), "[工具] run_command")
        self.assertEqual(events.diff("-a\n+b"), "[差异]\n-a\n+b")
        self.assertEqual(events.command_output("ok"), "[命令输出]\nok")
        self.assertEqual(events.approval_notice("已批准"), "[审批] 已批准")
        self.assertEqual(events.operation("修改 a.py"), "[操作] 修改 a.py")

    def test_json_round_trip_for_every_kind(self):
        samples = [
            events.tool_start("read_file", "a.py"), events.operation("改了"), events.diff("-a\n+b"),
            events.command_output("输出"), events.todo("1. 做事", [{"text": "做事"}]),
            events.approval_notice("拒绝"), events.tool_result("[结果] 成功", ok=True), events.notice("[提醒] 看这里"),
        ]
        for event in samples:
            with self.subTest(kind=event.kind):
                again = UiEvent.from_json(event.to_json())
                self.assertEqual((again.kind, str(again), again.data, again.agent),
                                 (event.kind, str(event), event.data, event.agent))

    def test_role_prefix_keeps_structure(self):
        tagged = tag_role(events.diff("-a\n+b"), "worker:1")
        self.assertEqual(tagged, "[worker:1] [差异]\n-a\n+b")
        self.assertEqual((tagged.kind, tagged.agent, tagged.data), ("diff", "worker:1", {"diff": "-a\n+b"}))
        self.assertEqual(tag_role("普通文本", "review"), "[review] 普通文本")
        self.assertNotIsInstance(tag_role("普通文本", "review"), UiEvent)


class AgentEventTests(unittest.TestCase):
    def test_edit_turn_emits_structured_events_in_order(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            (workspace / "a.txt").write_text("old", encoding="utf-8")
            sent = []

            def handler(request):
                sent.append(1)
                if len(sent) == 1:
                    return httpx2.Response(200, json=reply(None, [call("c1", "edit_file", {
                        "path": "a.txt", "old_text": "old", "new_text": "new"})]))
                return httpx2.Response(200, json=reply("已修改。"))

            def factory(**options):
                return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **options)

            config = Config("offline-key", "m", "https://example.invalid/v1",
                            protocol="chat_completions", streaming=False, tools=True)
            collected = []
            with ModelClient(config, client_factory=factory) as client:
                run_agent(client, "m", "把 a.txt 改成 new。", workspace, capabilities=("read", "write"),
                          approver=approve_all, mode="react", on_event=collected.append)
            structured = [item for item in collected if isinstance(item, UiEvent)]
            kinds = [item.kind for item in structured]
            self.assertEqual(kinds[0], "tool_start")
            self.assertIn("diff", kinds)
            self.assertEqual(kinds[-1], "tool_result")
            self.assertLess(kinds.index("tool_start"), kinds.index("diff"))
            self.assertTrue(structured[-1].data["ok"])
            self.assertTrue(all(isinstance(item, str) for item in collected))


if __name__ == "__main__":
    unittest.main()
