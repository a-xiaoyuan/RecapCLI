"""短期对话与 SQLite 项目事实：跨会话保留，并按词项相关性召回。"""

import io
import json
from pathlib import Path
import random
import tempfile
import time
import unittest

import httpx2
from openai import OpenAI

from recapcli.application.runtime import run_turn
from recapcli.storage.facts import FactStore, short_term_messages
from recapcli.storage.session import SessionStore
from recapcli.ui.terminal_ui import TerminalUI

SEED = 20261107
FACTS = 40
TOP_K = 3


def measure_recall(seed=SEED, facts=FACTS, k=TOP_K):
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        store = FactStore(workspace)
        planted = []
        order = list(range(facts))
        rng.shuffle(order)
        for index in order:
            key = f"factkey{index:03d}"
            planted.append((key, store.add(f"{key} 负责主题{index} 的项目事实")))
        hits_at_1 = hits_at_k = 0
        started = time.perf_counter()
        for key, item in planted:
            found = store.recall(f"{key} 怎么配置", k=k)
            ids = [row["id"] for row in found]
            if ids and ids[0] == item["id"]:
                hits_at_1 += 1
            if item["id"] in ids:
                hits_at_k += 1
        elapsed = time.perf_counter() - started
        retained = 0
        reopened = FactStore(workspace)
        for key, item in planted:
            found = reopened.recall(key, k=1)
            if found and found[0]["id"] == item["id"]:
                retained += 1
    return {"seed": seed, "facts": facts, "k": k, "hits_at_1": hits_at_1, "hits_at_k": hits_at_k,
            "retained": retained, "recall_seconds": elapsed}


class FactMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()

    def test_short_term_is_only_the_current_dialogue(self):
        store = SessionStore(self.workspace)
        first = store.new_session("test-model")
        first["messages"] = [{"role": "user", "content": "只有甲会话的暗语紫微斗数"},
                             {"role": "assistant", "content": "收到暗语"}]
        store.save(first)
        fact = store.add_fact("发布标识使用 factkeyrecall")
        second = store.new_session("test-model")
        self.assertEqual(short_term_messages(second), [])
        self.assertIn("暗语", short_term_messages(first)[0]["content"])
        self.assertNotIn("暗语", json.dumps(short_term_messages(second), ensure_ascii=False))
        self.assertEqual(SessionStore(self.workspace).recall_facts("factkeyrecall", k=1)[0]["id"], fact["id"])

    def test_turn_injects_recalled_facts_without_the_other_dialogue(self):
        store = SessionStore(self.workspace)
        store.add_fact("缓存键使用 factkeyturn 避免击穿")
        session = store.new_session("test-model")
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "已参考事实"}}]})

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with client:
            answer = run_turn(client, "test-model", self.workspace, store, TerminalUI(io.StringIO(), io.StringIO()),
                              session, "factkeyturn 应该怎么用", auto_compact=False, memory_enabled=True)
        self.assertEqual(answer, "已参考事实")
        prompt = seen[0]["messages"][0]["content"]
        self.assertIn("factkeyturn", prompt)
        self.assertIn("SQLite", prompt)
        self.assertNotIn("暗语", prompt)

    def test_measured_recall_hits_and_survives_reopen(self):
        measured = measure_recall()
        self.assertEqual(measured["facts"], 40)
        self.assertEqual(measured["hits_at_1"], 40)
        self.assertEqual(measured["hits_at_k"], 40)
        self.assertEqual(measured["retained"], 40)


if __name__ == "__main__":
    unittest.main()
