"""状态面板里预算用量的文字格式。"""

import unittest

from recapcli.tui.status import budget_text, duration_text


class BudgetTextTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(budget_text(12, 100), "12 / 100 次")
        self.assertEqual(budget_text(3, 0), "3 次")

    def test_seconds_switch_to_minutes(self):
        self.assertEqual(duration_text(5.9), "5s")
        self.assertEqual(duration_text(65), "1m05s")
        self.assertEqual(budget_text(65, 1800, seconds=True), "1m05s / 30m00s")
        self.assertEqual(budget_text(-3, 0, seconds=True), "0s")


if __name__ == "__main__":
    unittest.main()
