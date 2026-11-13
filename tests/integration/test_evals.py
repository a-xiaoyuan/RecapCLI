"""评测框架：用模拟模型跑通准备、运行、验收和报告，不发真实请求。"""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals import acceptance as evaluator, prompt_comparison, runner  # noqa: E402
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

    def prepare(self, **options):
        run_root = runner.prepare(self.spec, repo=self.repo, runs=self.root / "runs", evaluator=self.evaluator,
                                  model={"model": "offline"}, **options)
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
    def test_expanded_suite_retains_m2_tasks_and_separates_its_report(self):
        original = runner.suite_spec("m2")
        expanded = runner.suite_spec("expanded")
        self.assertEqual(expanded["tasks"][:3], original["tasks"])
        self.assertEqual(len(expanded["tasks"]) * expanded["repetitions"], 14)
        self.assertEqual(expanded["target_commit"], original["target_commit"])
        run_root, manifest = self.prepare(suite="expanded")
        self.assertEqual(manifest["suite"], "expanded")
        report = runner.report(run_root)
        self.assertIn("扩展套件，2 道题，共计划 4 次运行", report)
        self.assertNotIn("基线 2026-11-03", report)

    def test_expanded_fingerprint_includes_delegated_m2_evaluator(self):
        expanded = runner.EXPANDED_EVALUATOR
        original = runner.evaluator_hash(expanded)
        with patch.object(runner, "EVALUATOR", self.evaluator):
            self.assertNotEqual(runner.evaluator_hash(expanded), original)
            self.assertEqual(runner.evaluator_hash(self.evaluator), runner.file_hash(self.evaluator))

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

    def test_precheck_records_preexisting_workspace_test_failures(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_sample.py").write_text(
            "import unittest\n"
            "class Sample(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertEqual(1, 1)\n"
            "    def test_bad(self):\n"
            "        self.fail('预先就坏')\n",
            encoding="utf-8")
        self.commit("带上自带测试")
        self.spec["target_commit"] = self.git("rev-parse", "HEAD").strip()
        run_root, _manifest = self.prepare()
        precheck = runner.read_json(run_root / "precheck.json")
        self.assertEqual(precheck["fix"]["returncode"], 1)
        record = precheck["_workspace_tests"]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["returncode"], 1)
        self.assertEqual(record["installed_recapcli"], runner.installed_recapcli())
        self.assertTrue(any("test_bad" in name for name in record["failures"]))
        self.assertFalse(any("test_ok" in name for name in record["failures"]))
        self.assertTrue((run_root / "precheck-selftest" / "tests" / "test_sample.py").is_file())
        self.assertFalse((run_root / "precheck-workspace" / "tests" / "__pycache__").exists())
        report = runner.report(run_root, baseline_path=None)
        self.assertIn("test_bad", report)
        self.assertIn("预先存在", report)

    def test_prepare_rejects_interpreter_that_already_installs_recapcli(self):
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "recapcli"\nversion = "0.6.0"\n', encoding="utf-8")
        package = self.repo / "recapcli"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        self.commit("带上同名项目")
        self.spec["target_commit"] = self.git("rev-parse", "HEAD").strip()
        with patch.object(runner, "installed_recapcli", return_value="0.7.0"), \
                self.assertRaisesRegex(RuntimeError, "no-install-project"):
            self.prepare()


class RunTests(EvalCase):
    def test_requirement_acceptance_ablation_uses_independent_scoring(self):
        self.spec["tasks"] = self.spec["tasks"][:1]
        self.spec["repetitions"] = 1
        user_checks = self.root / "requirements.json"
        user_checks.write_text(json.dumps({"checks": [
            {"id": "cli-output", "description": "检查程序实际输出",
             "argv": [sys.executable, "-c", 'from pathlib import Path; print(Path("app.txt").read_text())'],
             "output_contains": ["fixed"], "output_excludes": ["broken"]},
        ]}), encoding="utf-8")
        control_dir, control = self.prepare()
        experiment_dir, experiment = self.prepare(agent_acceptance_path=user_checks)
        without = self.run_case(control_dir, control, "fix-1", [text("改好了")])
        with_check = self.run_case(experiment_dir, experiment, "fix-1", [text("改好了"), edit(), text("这次好了")])
        self.assertFalse(runner.score(without)["task_ok"])
        self.assertTrue(runner.score(with_check)["task_ok"])
        self.assertEqual((without["model_requests"], with_check["model_requests"]), (1, 3))
        self.assertEqual(with_check["requirement_acceptance"], {"passed": 1, "failed": 0, "not_run": 0, "total": 1})
        task = runner.read_json(experiment_dir / "results/fix-1/task.json")
        checks = [event["data"] for event in task["events"] if event["kind"] == "acceptance_check"]
        self.assertEqual([entry["status"] for entry in checks], ["failed", "passed"])
        self.assertIn("broken", checks[0]["output"])
        self.assertIn("fixed", checks[1]["output"])
        rendered = runner.report(experiment_dir, compare=control_dir)
        self.assertIn("运行时需求验收", rendered)
        self.assertIn("| fix-1 | — | 1/1 | 0 | 0 |", rendered)
        self.assertIn("| 任务做对 | 0 | 1 |", rendered)

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
        self.assertIsNone(result["invalid_reason"])
        self.assertTrue(scored["task_ok"])
        self.assertFalse(scored["engine_ok"])

    def test_startup_server_error_is_excluded_in_run_report_and_legacy_recheck(self):
        run_root, manifest = self.prepare()
        error = httpx2.Response(502, json={"error": {"message": "gateway"}})
        with patch.object(runner, "acceptance") as accept:
            result = self.run_case(run_root, manifest, "fix-1", [error])
        accept.assert_not_called()
        self.assertEqual(result["agent_status"], "failed")
        self.assertEqual(result["exception"]["status_code"], 502)
        self.assertEqual(result["invalid_reason"], "模型开始工作前 HTTP 502")
        self.assertIsNone(result["acceptance"])
        self.run_case(run_root, manifest, "fix-2", [text("我没改")])
        rendered = runner.report(run_root, baseline_path=None)
        summary = runner.read_json(run_root / "summary.json")
        totals = summary["totals"]
        self.assertEqual((totals["runs"], totals["valid_runs"], totals["invalid_runs"]), (2, 1, 1))
        self.assertEqual((totals["task_ok"], totals["task_pending"], totals["engine_ok"]), (0, 0, 1))
        self.assertEqual(totals["model_requests"], 2)
        self.assertIsNone(summary["results"][0]["task_ok"])
        self.assertIsNone(summary["results"][0]["engine_ok"])
        self.assertFalse(summary["results"][1]["task_ok"])
        self.assertIn("无效（模型开始工作前 HTTP 502）", rendered)

        # 模拟旧版结果：只有错误摘要，且当时仍运行了验收。
        result.pop("invalid_reason")
        result.pop("has_stream_output")
        result["exception"].pop("status_code")
        result["acceptance"] = {"result": {"passed": False, "checks": []}}
        path = run_root / "results/fix-1/result.json"
        runner.write_json(path, result)
        before = path.read_bytes()
        with patch.object(runner, "acceptance", wraps=runner.acceptance) as accept:
            revised = runner.recheck(run_root, evaluator=self.evaluator, baseline_path=None)
        accept.assert_called_once()
        self.assertEqual(Path(accept.call_args.args[2]).name, "fix-2")
        self.assertEqual(runner.read_json(revised / "summary.json")["totals"], totals)
        self.assertEqual(path.read_bytes(), before)

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
        runner.write_json(run_root / "source-reviews.json", {"read-1": {
            "verdict": "passed", "review_method": "ai_source_review",
        }})
        self.assertIn("是（AI 源码复核）", runner.report(run_root, baseline_path=None))
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

    def test_report_recovers_known_usage_from_events_instead_of_old_zero(self):
        run_root, _ = self.finished_run()
        result_path = run_root / "results/fix-1/result.json"
        legacy = runner.read_json(result_path)
        legacy["total_tokens"] = 0
        runner.write_json(result_path, legacy)
        task_path = result_path.with_name("task.json")
        task = runner.read_json(task_path)
        response = next(e for e in task["events"] if e["kind"] == "model_response")
        response["data"]["usage"] = {"total_tokens": 23}
        runner.write_json(task_path, task)
        report = runner.report(run_root, baseline_path=None)
        scored = runner.read_json(run_root / "summary.json")["results"][0]
        self.assertIsNone(scored["total_tokens"])
        self.assertEqual(scored["known_total_tokens"], 23)
        self.assertEqual(scored["usage_missing_requests"], 1)
        self.assertIn("≥23（1 次请求用量未知）", report)
        self.assertEqual(runner.read_json(result_path)["total_tokens"], 0)

    def test_recheck_preserves_original_results_and_separates_evaluator_revision(self):
        run_root, manifest = self.finished_run()
        runner.report(run_root, baseline_path=None)
        originals = [run_root / name for name in ("manifest.json", "summary.json", "report.md")]
        originals += list((run_root / "results").glob("*/*"))
        before = {path: path.read_bytes() for path in originals}
        self.evaluator.write_text(EVALUATOR + "\n# revised\n", encoding="utf-8")
        revised = runner.recheck(run_root, evaluator=self.evaluator, baseline_path=None)
        self.assertEqual({path: path.read_bytes() for path in originals}, before)
        changed = runner.read_json(revised / "manifest.json")
        self.assertEqual(changed["original_acceptance_sha256"], manifest["acceptance_sha256"])
        self.assertNotEqual(changed["acceptance_sha256"], manifest["acceptance_sha256"])
        scored = runner.read_json(revised / "summary.json")["results"]
        self.assertTrue(scored[0]["task_ok"])
        self.assertIn("未重新运行模型", (revised / "report.md").read_text(encoding="utf-8"))

    def test_recheck_refuses_changed_artifact(self):
        run_root, manifest = self.finished_run()
        workspace = Path(manifest["cases"][0]["workspace"])
        (workspace / "app.txt").write_text("changed after evaluation", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "产物已变化"):
            runner.recheck(run_root, evaluator=self.evaluator, baseline_path=None)


class PromptComparisonTests(unittest.TestCase):
    def test_prepare_refuses_dirty_worktree(self):
        with patch.object(runner, "git", return_value=b" M README.md\n"), patch.object(runner, "prepare") as prepare:
            with self.assertRaisesRegex(ValueError, "干净工作区"):
                prompt_comparison.prepare(None)
        prepare.assert_not_called()

    def test_failed_probe_cannot_start_paid_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = root / "probe.json"
            runner.write_json(root / "manifest.json", {"model": {"model": "offline"}})
            runner.write_json(probe, {"ready": False, "error_type": "APIConnectionError"})
            with patch.object(prompt_comparison, "ModelClient") as client:
                with self.assertRaisesRegex(ValueError, "成功探测"):
                    prompt_comparison.run(root, probe)
            client.assert_not_called()


class TokenUsageTests(unittest.TestCase):
    def task(self, *usages, requests=None):
        return {"events": [{"kind": "model_request", "data": {}} for _ in range(len(usages) if requests is None else requests)]
                + [{"kind": "model_response", "data": {"usage": usage}} for usage in usages]}

    def test_complete_usage_is_exact_even_if_zero(self):
        result = runner.token_usage(self.task({"total_tokens": 12}, {"total_tokens": 0}))
        self.assertEqual(result, {"total_tokens": 12, "known_total_tokens": 12, "usage_missing_requests": 0})

    def test_failed_request_keeps_known_lower_bound(self):
        result = runner.token_usage(self.task({"total_tokens": 12}, requests=2))
        self.assertIsNone(result["total_tokens"])
        self.assertEqual(result["known_total_tokens"], 12)
        self.assertEqual(result["usage_missing_requests"], 1)

    def test_missing_usage_and_missing_task_are_not_zero(self):
        self.assertIsNone(runner.token_usage(self.task(None))["total_tokens"])
        self.assertIsNone(runner.token_usage(None)["total_tokens"])

    def test_aggregate_preserves_unknown_and_known_subtotal(self):
        items = []
        for usage in (self.task({"total_tokens": 10}), self.task({"total_tokens": 20}, requests=2)):
            items.append({"task_ok": False, "engine_ok": False, "model_requests": 2, "tool_calls": 0,
                          "seconds": 1, **runner.token_usage(usage)})
        totals = runner.summarize(items)
        self.assertIsNone(totals["total_tokens"])
        self.assertEqual(totals["known_total_tokens"], 30)
        self.assertEqual(totals["usage_missing_requests"], 1)


class InvalidRunTests(unittest.TestCase):
    def test_exclusion_requires_service_error_and_no_model_progress(self):
        result = {"agent_status": "failed", "exception": {"type": "InternalServerError", "status_code": 503}}
        initial = {"events": [{"kind": "model_request"}]}
        self.assertEqual(runner.invalid_run_reason(result, initial), "模型开始工作前 HTTP 503")
        for later in ("model_response", "message", "tool_start"):
            with self.subTest(event=later):
                started = {"events": [*initial["events"], {"kind": later}]}
                self.assertIsNone(runner.invalid_run_reason(result, started))
        self.assertIsNone(runner.invalid_run_reason({**result, "has_stream_output": True}, initial))
        self.assertIsNone(runner.invalid_run_reason({**result, "git_status": " M app.txt"}, initial))
        self.assertIsNone(runner.invalid_run_reason(result, None))
        self.assertIsNone(runner.invalid_run_reason({**result, "exception": {"status_code": 401}}, initial))

    def test_connection_failure_is_invalid_only_before_model_progress(self):
        result = {"agent_status": "failed", "exception": {"type": "APIConnectionError"}}
        initial = {"events": [{"kind": "model_request"}]}
        self.assertEqual(runner.invalid_run_reason(result, initial), "模型开始工作前连接失败")
        started = {"events": [*initial["events"], {"kind": "model_response"}]}
        self.assertIsNone(runner.invalid_run_reason(result, started))
        self.assertIsNone(runner.invalid_run_reason({**result, "has_stream_output": True}, initial))


class RuleAcceptanceTests(unittest.TestCase):
    def record(self, status="loaded"):
        return {"status": status, "layers": [{"source": "pkg/AGENTS.md", "status": "loaded", "bytes": 17}]}

    def test_accurate_root_status_notes_are_allowed(self):
        for text_ in ("根目录未找到 AGENTS.md，子目录规则 · 已加载 17 字节",
                      "根目录 AGENTS.md 未加载，子目录规则 · 已加载 17 字节 · 含 2 个规则文件",
                      "pkg/AGENTS.md · 已加载 17 字节 · 1 个未加载",
                      "子目录规则已加载 · pkg/AGENTS.md · 共 17 字节",
                      "项目规则 · 已加载 17 字节（根目录规则未进入上下文）"):
            with self.subTest(text=text_):
                evaluator.validate_rule_summary(self.record(), text_, 1)

    def test_internal_status_field_does_not_decide_the_result(self):
        evaluator.validate_rule_summary(self.record("missing"), "子目录规则 · 已加载 17 字节", 1)

    def test_text_cannot_claim_the_root_when_only_nested_rule_loaded(self):
        for text_ in ("AGENTS.md · 已加载 17 字节", "AGENTS.md · 已加载 17 字节 · 含 1 个规则文件"):
            with self.subTest(text=text_), self.assertRaisesRegex(AssertionError, "根规则没有加载"):
                evaluator.validate_rule_summary(self.record(), text_, 1)

    def test_text_must_say_what_was_loaded(self):
        with self.assertRaisesRegex(AssertionError, "没有说明"):
            evaluator.validate_rule_summary(self.record(), "规则 · 已加载 17 字节", 1)

    def test_named_file_bytes_need_not_be_the_aggregate(self):
        record = self.record()
        record["layers"].append({"source": "AGENTS.md", "status": "loaded", "bytes": 9})
        evaluator.validate_rule_summary(record, "AGENTS.md · 已加载 9 字节 · 含 2 个规则文件", 2)
        evaluator.validate_rule_summary(record, "规则 · 已加载 26 字节", 2)
        with self.assertRaises(AssertionError):
            evaluator.validate_rule_summary(record, "规则 · 已加载 9 字节", 2)

    def test_wrong_byte_count_and_missing_loaded_text_still_fail(self):
        for text_ in ("根目录未找到 AGENTS.md", "pkg/AGENTS.md · 已加载 999 字节"):
            with self.subTest(text=text_), self.assertRaises(AssertionError):
                evaluator.validate_rule_summary(self.record(), text_, 1)

    def test_no_rules_cannot_be_reported_as_loaded(self):
        with self.assertRaises(AssertionError):
            evaluator.validate_rule_summary({"status": "missing", "layers": []}, "已加载 0 字节", 0)


class AcceptanceIsolationTests(EvalCase):
    def test_precheck_does_not_touch_case_workspaces(self):
        self.evaluator.write_text(EVALUATOR.replace(
            'passed = "fixed"', '(workspace / "left-behind.txt").write_text("x")\npassed = "fixed"'), encoding="utf-8")
        run_root, manifest = self.prepare()
        self.assertTrue((run_root / "precheck-workspace/left-behind.txt").exists())
        for case in manifest["cases"]:
            self.assertFalse((Path(case["workspace"]) / "left-behind.txt").exists())

    def test_repeated_feature_acceptance_gives_the_same_checks(self):
        workspace = self.root / "engine"
        shutil.copytree(ROOT / "recapcli", workspace / "recapcli", ignore=shutil.ignore_patterns("__pycache__"))
        results = [runner.acceptance(runner.EVALUATOR, "feature", workspace)["result"]["checks"] for _ in range(2)]
        self.assertEqual(results[0], results[1])

    def test_rescore_baseline_keeps_source_and_review_results(self):
        run_root, manifest = self.prepare()
        self.run_case(run_root, manifest, "fix-1", [edit(), text("改好了")])
        source = self.root / "baseline.json"
        destination = self.root / "rescored.json"
        rows = [{"id": "fix-1", "task": "fix", "task_ok": False, "checks_passed": 0, "checks_total": 1},
                {"id": "read-1", "task": "read", "task_ok": True, "checks_passed": None, "checks_total": None}]
        runner.write_json(source, {"acceptance_sha256": "old", "results": rows})
        before = source.read_bytes()
        runner.rescore_baseline(run_root, source=source, destination=destination, evaluator=self.evaluator)
        self.assertEqual(source.read_bytes(), before)
        rescored = runner.read_json(destination)
        self.assertEqual(rescored["original_acceptance_sha256"], "old")
        self.assertEqual(rescored["acceptance_sha256"], runner.file_hash(self.evaluator))
        self.assertTrue(rescored["results"][0]["task_ok"])
        self.assertEqual(rescored["results"][1], rows[1])

    def test_rescore_refuses_changed_artifact(self):
        run_root, manifest = self.prepare()
        self.run_case(run_root, manifest, "fix-1", [edit(), text("改好了")])
        (Path(manifest["cases"][0]["workspace"]) / "app.txt").write_text("后来改的", encoding="utf-8")
        source = self.root / "baseline.json"
        runner.write_json(source, {"acceptance_sha256": "old", "results": [
            {"id": "fix-1", "task": "fix", "task_ok": False, "checks_passed": 0, "checks_total": 1}]})
        with self.assertRaisesRegex(RuntimeError, "基线产物已变化"):
            runner.rescore_baseline(run_root, source=source, destination=self.root / "out.json", evaluator=self.evaluator)


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

    def test_original_baseline_is_preserved_as_v1(self):
        baseline = runner.read_json(runner.ORIGINAL_BASELINE)
        self.assertEqual(runner.file_hash(runner.LEGACY_EVALUATOR), baseline["acceptance_sha256"])
        self.assertNotEqual(runner.file_hash(runner.EVALUATOR), baseline["acceptance_sha256"])

    def test_rescored_baseline_uses_current_acceptance(self):
        original = runner.read_json(runner.ORIGINAL_BASELINE)
        rescored = runner.read_json(runner.BASELINE)
        self.assertEqual(rescored["acceptance_sha256"], runner.file_hash(runner.EVALUATOR))
        self.assertEqual(rescored["original_acceptance_sha256"], original["acceptance_sha256"])
        self.assertEqual([item["id"] for item in rescored["results"]], [item["id"] for item in original["results"]])

    def test_run_refuses_without_real_model_flag(self):
        with patch.object(runner, "run_real") as run, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.main(["run"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
