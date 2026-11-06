"""评测框架：用模拟模型跑通准备、运行、验收和报告，不发真实请求。"""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals import runner  # noqa: E402
from recapcli.core.agent import AgentLimits  # noqa: E402

EVALUATOR = """import json, sys
from pathlib import Path
task, workspace = sys.argv[1], Path(sys.argv[2])
passed = "fixed" in (workspace / "app.txt").read_text(encoding="utf-8")
print(json.dumps({"category": task, "passed": passed, "checks": [{"name": "fixed", "passed": passed}]}))
sys.exit(0 if passed else 1)
"""


def completion(message, finish_reason):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "offline",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


def text(content):
    return httpx2.Response(200, json=completion({"role": "assistant", "content": content}, "stop"))


def edit():
    arguments = json.dumps({"path": "app.txt", "old_text": "broken", "new_text": "fixed"})
    return httpx2.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [{
        "id": "call", "type": "function", "function": {"name": "edit_file", "arguments": arguments}}]}, "tool_calls"))


class EvalCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "app.txt").write_text("broken", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".recapcli/\n", encoding="utf-8")
        self.commit("目标")
        self.target = self.git("rev-parse", "HEAD").strip()
        (self.repo / "later.txt").write_text("引擎后来才有的文件", encoding="utf-8")
        self.commit("引擎")
        self.evaluator = self.root / "evaluator.py"
        self.evaluator.write_text(EVALUATOR, encoding="utf-8")
        self.spec = {"target_commit": self.target, "repetitions": 2, "max_model_requests": 4,
                     "limits": {"max_context_tokens": 43690, "max_tool_calls": 8, "max_seconds": 60,
                                "request_timeout": 10, "max_retries": 0},
                     "tasks": [{"id": "fix", "kind": "acceptance", "prompt": "修复"},
                               {"id": "read", "kind": "review", "prompt": "说明"}]}

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], capture_output=True, text=True,
                              check=True).stdout

    def commit(self, message):
        self.git("add", "--all")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--no-gpg-sign", "-m", message)

    def prepare(self):
        run_root = runner.prepare(self.spec, repo=self.repo, runs=self.root / "runs", evaluator=self.evaluator,
                                  model={"model": "offline"})
        return run_root, runner.read_json(run_root / "manifest.json")

    def run_case(self, run_root, manifest, identity, replies):
        queue = list(replies)

        def handler(request):
            return queue.pop(0)

        case = next(item for item in manifest["cases"] if item["id"] == identity)
        with OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
            return runner.run_case(run_root, manifest, case, client, "offline", evaluator=self.evaluator)


class PrepareTests(EvalCase):
    def test_workspaces_come_from_target_not_engine_head(self):
        run_root, manifest = self.prepare()
        self.assertEqual([case["id"] for case in manifest["cases"]], ["fix-1", "fix-2", "read-1", "read-2"])
        self.assertEqual(manifest["engine_commit"], self.git("rev-parse", "HEAD").strip())
        self.assertNotEqual(manifest["engine_commit"], manifest["target_commit"])
        for case in manifest["cases"]:
            workspace = Path(case["workspace"])
            self.assertFalse((workspace / "later.txt").exists())
            self.assertEqual(runner.fingerprint(workspace), manifest["target_fingerprint"])
            self.assertFalse(runner.git(workspace, "status", "--porcelain=v1"))
        self.assertEqual(runner.read_json(run_root / "precheck.json")["fix"]["returncode"], 1)

    def test_task_that_already_passes_is_refused(self):
        (self.repo / "app.txt").write_text("fixed", encoding="utf-8")
        self.commit("已修复")
        self.spec["target_commit"] = self.git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(RuntimeError, "fix"):
            self.prepare()

    def test_dirty_engine_is_recorded_and_reported(self):
        (self.repo / "later.txt").write_text("未提交", encoding="utf-8")
        run_root, manifest = self.prepare()
        self.assertIn("later.txt", manifest["engine_status"])
        self.assertIn("未提交改动", runner.report(run_root, baseline_path=None))


class RunTests(EvalCase):
    def test_success_records_diff_and_both_scores(self):
        run_root, manifest = self.prepare()
        result = self.run_case(run_root, manifest, "fix-1", [edit(), text("改好了")])
        scored = runner.score(result)
        self.assertTrue(scored["task_ok"])
        self.assertTrue(scored["engine_ok"])
        self.assertEqual((scored["checks_passed"], scored["checks_total"]), (1, 1))
        self.assertEqual(scored["model_requests"], 2)
        self.assertIn("+fixed", (run_root / "results/fix-1/changes.diff").read_text(encoding="utf-8"))

    def test_correct_fix_with_failed_engine_counts_separately(self):
        run_root, manifest = self.prepare()
        result = self.run_case(run_root, manifest, "fix-1", [edit(), httpx2.Response(500, json={"error": {"message": "坏了"}})])
        scored = runner.score(result)
        self.assertTrue(scored["task_ok"])
        self.assertFalse(scored["engine_ok"])

    def test_engine_finished_but_task_wrong(self):
        run_root, manifest = self.prepare()
        scored = runner.score(self.run_case(run_root, manifest, "fix-1", [text("我没改")]))
        self.assertFalse(scored["task_ok"])
        self.assertTrue(scored["engine_ok"])

    def test_changed_evaluator_stops_the_run(self):
        run_root, manifest = self.prepare()
        self.evaluator.write_text(EVALUATOR + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "验收脚本"):
            self.run_case(run_root, manifest, "fix-1", [text("好")])

    def test_outside_directories_are_rejected(self):
        approvals = []
        workspace = self.root / "workspace"
        approver = runner.workspace_approver(workspace, approvals)
        request = type("Request", (), {"tool": "run_command", "params": {"cwd": str(self.root)}})
        self.assertEqual(approver(request), "rejected")
        request.params = {"cwd": "."}
        self.assertEqual(approver(request), "approved")
        request.tool = "access_directory"
        self.assertEqual(approver(request), "rejected")


class ReportTests(EvalCase):
    def finished_run(self):
        run_root, manifest = self.prepare()
        self.run_case(run_root, manifest, "fix-1", [edit(), text("改好了")])
        self.run_case(run_root, manifest, "read-1", [text("说明")])
        return run_root, manifest

    def test_review_task_stays_pending_until_reviewed(self):
        run_root, _ = self.finished_run()
        self.assertIn("待核对", runner.report(run_root, baseline_path=None))
        totals = runner.read_json(run_root / "summary.json")["totals"]
        self.assertEqual((totals["runs"], totals["task_ok"], totals["task_pending"]), (2, 1, 1))
        runner.write_json(run_root / "source-reviews.json", {"read-1": {"verdict": "passed"}})
        runner.report(run_root, baseline_path=None)
        totals = runner.read_json(run_root / "summary.json")["totals"]
        self.assertEqual((totals["task_ok"], totals["task_pending"], totals["both_ok"]), (2, 0, 2))

    def test_comparison_warns_when_baseline_differs(self):
        run_root, manifest = self.finished_run()
        baseline = runner.read_json(runner.BASELINE)
        path = self.root / "baseline.json"
        runner.write_json(path, baseline)
        text_ = runner.report(run_root, baseline_path=path)
        self.assertIn("基线 2026-11-03", text_)
        self.assertIn("不是同一份字节", text_)
        self.assertIn("验收脚本和基线不同", text_)
        baseline.update(target_fingerprint=manifest["target_fingerprint"], acceptance_sha256=manifest["acceptance_sha256"])
        runner.write_json(path, baseline)
        self.assertNotIn("**注意", runner.report(run_root, baseline_path=path))


class CommittedFilesTests(unittest.TestCase):
    def test_spec_limits_are_valid_for_current_engine(self):
        spec = runner.read_json(runner.SPEC)
        AgentLimits(**spec["limits"])
        self.assertEqual([task["id"] for task in spec["tasks"]], ["understanding", "bugfix", "feature"])

    def test_baseline_keeps_recorded_outcome_without_local_details(self):
        text_ = runner.BASELINE.read_text(encoding="utf-8")
        self.assertNotIn("C:\\\\", text_)
        self.assertNotIn("provider_host", text_)
        totals = runner.summarize(json.loads(text_)["results"])
        self.assertEqual((totals["runs"], totals["both_ok"], totals["model_requests"], totals["tool_calls"]), (6, 0, 77, 154))
        self.assertEqual(totals["total_tokens"], 1584578)

    def test_baseline_acceptance_is_the_committed_script(self):
        baseline = runner.read_json(runner.BASELINE)
        self.assertEqual(runner.file_hash(runner.EVALUATOR), baseline["acceptance_sha256"])

    def test_run_refuses_without_real_model_flag(self):
        with patch.object(runner, "run_real") as run, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.main(["run"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
