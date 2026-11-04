import unittest

from recapcli.core.context import TokenMeter, estimate_tokens, json_tokens, select_history


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

    def test_token_budget_and_selection_does_not_mutate_source(self):
        messages = [{"role": "user", "content": "中" * 100}, {"role": "assistant", "content": "答"}]
        self.assertEqual(select_history(messages, max_tokens=50), [])
        selected = select_history(messages, max_tokens=1000)
        self.assertEqual(len(selected), 2)
        selected[0]["content"] = "改动"
        self.assertEqual(messages[0]["content"], "中" * 100)

    def test_meter_calibrates_with_reported_prompt_tokens(self):
        meter = TokenMeter()
        messages = [{"role": "user", "content": "a" * 20000}]
        estimate = json_tokens(messages)
        meter.observe(estimate, {"prompt_tokens": int(estimate * 1.5)})
        self.assertAlmostEqual(meter.ratio, 1.5, places=2)
        self.assertGreater(meter.count(messages), estimate)
        meter.observe(100, {"prompt_tokens": 1000})  # 太短的请求不参与校准
        self.assertAlmostEqual(meter.ratio, 1.5, places=2)
        meter.observe(estimate, {"prompt_tokens": estimate * 10})
        self.assertEqual(meter.ratio, TokenMeter.MAX_RATIO)

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
