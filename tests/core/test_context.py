import unittest

from recapcli.core.context import estimate_tokens, json_size, json_tokens, select_history


class ContextTests(unittest.TestCase):
    def test_message_limit_keeps_whole_turn_and_tool_pairs(self):
        first = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]
        second = [
            {"role": "user", "content": "读取文件"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "结果"},
            {"role": "assistant", "content": "总结"},
        ]
        self.assertEqual(select_history(first + second, max_messages=5), second)
        self.assertEqual(select_history(first + second, max_messages=3), [])

    def test_byte_budget_counts_utf8_and_does_not_mutate_source(self):
        messages = [{"role": "user", "content": "中" * 100}, {"role": "assistant", "content": "答"}]
        self.assertEqual(select_history(messages, max_bytes=200), [])
        selected = select_history(messages, max_bytes=1000)
        self.assertLessEqual(json_size(selected), 1000)
        selected[0]["content"] = "改动"
        self.assertEqual(messages[0]["content"], "中" * 100)

    def test_incomplete_latest_turn_is_not_sent_as_history(self):
        messages = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"},
                    {"role": "user", "content": "未完成的问题"}]
        self.assertEqual(select_history(messages), [])

    def test_token_estimate_and_token_budget_keep_whole_turns(self):
        self.assertGreater(estimate_tokens("中" * 100), estimate_tokens("a" * 100))
        first = [{"role": "user", "content": "旧问题" * 40}, {"role": "assistant", "content": "旧回答" * 40}]
        second = [{"role": "user", "content": "新问题"}, {"role": "assistant", "content": "新回答"}]
        self.assertEqual(select_history(first + second, max_tokens=10_000), first + second)
        only_latest = select_history(first + second, max_tokens=json_tokens(second) + 2)
        self.assertEqual(only_latest, second)


if __name__ == "__main__":
    unittest.main()
