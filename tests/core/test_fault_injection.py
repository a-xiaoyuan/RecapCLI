"""故障注入可复跑：固定种子下，工具执行后的四类故障都应正常收尾。"""

import unittest

from evals.fault_injection import SEED, TRIALS, run_suite


class FaultInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_suite(SEED, TRIALS)

    def test_each_fault_runs_thirty_times_from_the_fixed_seed(self):
        self.assertEqual(self.report["seed"], 20261107)
        self.assertEqual(self.report["trials_per_kind"], 30)
        self.assertEqual([item["kind"] for item in self.report["kinds"]],
                         ["429", "5xx", "disconnect", "context_overflow"])
        self.assertEqual([item["trials"] for item in self.report["kinds"]], [30, 30, 30, 30])

    def test_post_tool_faults_finish_keep_edits_and_do_not_replay(self):
        for item in self.report["kinds"]:
            with self.subTest(kind=item["kind"]):
                self.assertEqual(item["normal_finish"], 30)
                self.assertEqual(item["preserved"], 30)
                self.assertEqual(item["duplicate_runs"], 0)
                self.assertEqual(item["exceptions"], 0)

    def test_outcomes_follow_the_seeded_schedule(self):
        from evals.fault_injection import build_schedule
        self.assertEqual(build_schedule(SEED, TRIALS), build_schedule(SEED, TRIALS))
        for row in self.report["results"]:
            # 上下文超限是 HTTP 400，引擎不重试；其余故障在仅有的一次重试成功时正常返回。
            recovers = row["kind"] != "context_overflow" and row["max_retries"] == 1 and not row["retry_also_fails"]
            self.assertEqual(row["status"], "returned" if recovers else "incomplete")
            self.assertIsNone(row["exception"])
            self.assertEqual(row["executed"], row["issued"])


if __name__ == "__main__":
    unittest.main()
