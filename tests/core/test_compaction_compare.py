"""固定长会话压缩后，上下文变小，代码题和依赖旧事实的追问仍然通过。"""

import json
import unittest

from recapcli.base.tokens import json_tokens

from evals.compaction_compare import FACTS, FIXTURE, build_fixture_messages, compare, load_fixture


class CompactionCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = compare()

    def test_fixture_is_checked_in_and_facts_are_not_in_the_tail(self):
        history = load_fixture()
        self.assertEqual(history, build_fixture_messages())
        self.assertEqual(len(history), 32)
        self.assertEqual(FIXTURE.name, "long_session_2026-11.json")
        blob = json.dumps(history[-8:], ensure_ascii=False)
        for fact in FACTS:
            self.assertNotIn(fact, blob)

    def test_compaction_shrinks_context_and_keeps_the_task(self):
        result = self.result
        self.assertGreater(result["slice_tokens_before"], result["slice_tokens_after"])
        self.assertGreater(result["request_tokens_before"], result["request_tokens_after"])
        self.assertGreater(result["engine_before_bytes"], result["engine_after_bytes"])
        self.assertTrue(result["workspace_digest_unchanged"])
        self.assertTrue(result["task_pass_before"])
        self.assertTrue(result["task_pass_after"])
        self.assertTrue(result["facts_in_compact_payload"])
        self.assertTrue(result["facts_in_followup_request"])
        self.assertTrue(result["continuation_pass"])
        self.assertTrue(result["uncompacted_continuation_pass"])
        self.assertEqual(result["slice_tokens_before"], json_tokens(load_fixture()[:result["covered_messages"]]))


if __name__ == "__main__":
    unittest.main()
