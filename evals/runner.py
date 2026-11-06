"""在固定的目标仓库上跑真实任务：引擎是当前代码，目标是 tasks.json 固定的提交。

    python -X utf8 evals/runner.py prepare
    python -X utf8 evals/runner.py run --real-model [--case bugfix-1]
    python -X utf8 evals/runner.py report

结果分两项计分：任务做对了没有（task_ok）、引擎是否正常结束（engine_ok）。
收尾改进只影响后者，分开才不会把它算成解题能力的提升。
"""

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import traceback
from urllib.parse import urlsplit

EVALS = Path(__file__).resolve().parent
REPO = EVALS.parent
sys.path.insert(0, str(REPO))

from openai import OpenAI  # noqa: E402
from recapcli.application.runtime import run_turn  # noqa: E402
from recapcli.config import load_config  # noqa: E402
from recapcli.core.agent import AgentLimits  # noqa: E402
from recapcli.llm.client import ModelClient, api_error_message  # noqa: E402
from recapcli.storage.session import SessionStore  # noqa: E402
from recapcli.storage.tasks import TaskStore  # noqa: E402
from recapcli.tools.commands import close_commands  # noqa: E402

SPEC = EVALS / "tasks.json"
EVALUATOR = EVALS / "acceptance.py"
RUNS = EVALS / "runs"
ORIGINAL_BASELINE = EVALS / "baselines" / "2026-11-03.json"
BASELINE = EVALS / "baselines" / "2026-11-03-v3.json"
LEGACY_EVALUATOR = EVALS / "baselines" / "acceptance-v1.py"
ACCEPTANCE_TIMEOUT_SECONDS = 120
BOUNDARY = "这是少量固定任务的重复运行，只能说明这组任务上的变化，不能外推成通用成功率。"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def git(workspace, *args):
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "maintenance.auto=false",
                           "-C", str(workspace), *args], capture_output=True, check=True, timeout=60).stdout


def _normalized(path):
    """统一换行：同一份内容在 Windows 和 Linux 上哈希相同。"""
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def fingerprint(workspace):
    digest = hashlib.sha256()
    for raw in git(workspace, "ls-files", "-z").split(b"\0"):
        if raw:
            digest.update(raw + b"\0" + _normalized(Path(workspace) / os.fsdecode(raw)))
    return digest.hexdigest()


def file_hash(path):
    return hashlib.sha256(_normalized(path)).hexdigest()


def create_workspace(repo, commit, destination):
    """导出目标提交并新建独立仓库；工作区里没有历史，也没有评测材料。"""
    destination.mkdir(parents=True)
    # 固定 LF 导出，换一台机器工作区字节也一样。
    archive = git(repo, "-c", "core.autocrlf=false", "archive", "--format=tar", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    git(destination, "init", "-q")
    git(destination, "config", "core.autocrlf", "false")
    git(destination, "add", "--all")
    git(destination, "-c", "user.name=evals", "-c", "user.email=evals@example.invalid",
        "commit", "-q", "--no-gpg-sign", "-m", "evaluation baseline")


def acceptance(evaluator, task, workspace):
    completed = subprocess.run([sys.executable, "-X", "utf8", "-I", str(evaluator), task, str(workspace)],
                               cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=ACCEPTANCE_TIMEOUT_SECONDS)
    result = {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    try:
        result["result"] = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    return result


def token_usage(task):
    """按逐请求事实累计已知用量；缺失请求的用量保留未知，不用零填补。"""
    if task is None:
        return {"total_tokens": None, "known_total_tokens": 0, "usage_missing_requests": None}
    events = task["events"]
    requests = sum(event["kind"] == "model_request" for event in events)
    responses = [event["data"].get("usage") for event in events if event["kind"] == "model_response"]
    known = [usage["total_tokens"] for usage in responses if isinstance(usage, dict)
             and type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0]
    complete = len(known) == len(responses) == requests
    return {"total_tokens": sum(known) if complete else None, "known_total_tokens": sum(known),
            "usage_missing_requests": max(0, requests - len(known))}


def prepare(spec, *, repo=REPO, runs=RUNS, evaluator=EVALUATOR, model=None):
    """准备每一例的工作区，并在调用模型前确认验收确实不通过。"""
    run_root = Path(runs) / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root.mkdir(parents=True)
    manifest = {"schema_version": 2, "created_at": datetime.now().astimezone().isoformat(),
                "engine_commit": git(repo, "rev-parse", "HEAD").decode().strip(),
                "engine_status": git(repo, "status", "--porcelain=v1").decode(),
                "target_commit": spec["target_commit"], "model": model or {},
                "mode": "react", "capabilities": ["read", "write", "exec"], "memory_enabled": False,
                "auto_compact": False, "max_model_requests": spec["max_model_requests"],
                "limits": asdict(AgentLimits(**spec["limits"])), "acceptance_sha256": file_hash(evaluator),
                "tasks": spec["tasks"], "cases": [], "target_fingerprint": None}
    for task in spec["tasks"]:
        for repeat in range(1, spec["repetitions"] + 1):
            identity = f"{task['id']}-{repeat}"
            workspace = run_root / "workspaces" / identity
            create_workspace(repo, spec["target_commit"], workspace)
            current = fingerprint(workspace)
            manifest["target_fingerprint"] = manifest["target_fingerprint"] or current
            if current != manifest["target_fingerprint"]:
                raise RuntimeError("各例的初始工作区不一致")
            manifest["cases"].append({"id": identity, "task": task["id"], "kind": task["kind"], "repeat": repeat,
                                      "workspace": str(workspace)})
    write_json(run_root / "manifest.json", manifest)
    # 预检用单独的工作区：验收会留下文件，不能影响之后真实用例的得分。
    probe = run_root / "precheck-workspace"
    create_workspace(repo, spec["target_commit"], probe)
    precheck = {task["id"]: acceptance(evaluator, task["id"], probe)
                for task in spec["tasks"] if task["kind"] == "acceptance"}
    write_json(run_root / "precheck.json", precheck)
    already = [task for task, result in precheck.items() if result["returncode"] == 0]
    if already:
        raise RuntimeError("这些任务在模型开始前就已通过，评测没有意义：" + "、".join(already))
    (Path(runs) / "latest.txt").write_text(str(run_root), encoding="utf-8")
    return run_root


class RunLog:
    def __init__(self, path):
        self.file = path.open("w", encoding="utf-8")

    def info(self, value):
        self.file.write(str(value) + "\n")
        self.file.flush()

    event = info
    error = info

    def task_update(self, task):
        pass

    def stream_update(self, kind, value):
        if kind in {"start", "end", "abort"}:
            self.info(f"stream:{kind}")


def workspace_approver(workspace, approvals):
    """工作区内自动批准，外部目录和外部命令目录一律拒绝。"""
    def approver(request):
        outcome = "rejected" if request.tool == "access_directory" else "approved"
        if request.tool == "run_command":
            cwd = Path(request.params.get("cwd") or workspace)
            if not cwd.is_absolute():
                cwd = workspace / cwd
            if not cwd.resolve().is_relative_to(workspace):
                outcome = "rejected"
        approvals.append({"tool": request.tool, "outcome": outcome})
        return outcome
    return approver


def run_case(run_root, manifest, case, client, model, *, evaluator=EVALUATOR):
    identity = case["id"]
    out = Path(run_root) / "results" / identity
    out.mkdir(parents=True)
    workspace = Path(case["workspace"]).resolve()
    if fingerprint(workspace) != manifest["target_fingerprint"] or git(workspace, "status", "--porcelain=v1"):
        raise RuntimeError(f"{identity}：初始工作区已变化")
    if file_hash(evaluator) != manifest["acceptance_sha256"]:
        raise RuntimeError("验收脚本已变化")
    prompt = next(item["prompt"] for item in manifest["tasks"] if item["id"] == case["task"])
    limits = AgentLimits(**manifest["limits"])
    store = SessionStore(workspace)
    session = store.new_session(model)
    log = RunLog(out / "agent.log")
    approvals = []
    started = time.monotonic()
    answer, error = "", None
    try:
        answer = run_turn(client, model, workspace, store, log, session, prompt,
                          max_steps=manifest["max_model_requests"], memory_enabled=False, limits=limits,
                          capabilities=tuple(manifest["capabilities"]), mode=manifest["mode"],
                          auto_compact=manifest["auto_compact"], approver=workspace_approver(workspace, approvals))
        store.save(session)
    except Exception as exception:
        message = (api_error_message(exception, model) if type(exception).__module__.startswith("openai")
                   else str(exception))
        error = {"type": type(exception).__name__, "message": message}
        (out / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        close_commands(workspace)
        log.file.close()
    elapsed = time.monotonic() - started
    (out / "answer.md").write_text(answer or "", encoding="utf-8")
    tasks = TaskStore(workspace, create=False)
    found = tasks.list_tasks()
    task = tasks.get(found[0]["id"]) if found else None
    write_json(out / "task.json", task)
    events = task["events"] if task else []
    if case["kind"] == "acceptance":
        checked = acceptance(evaluator, case["task"], workspace)
    else:
        checked = None
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all").decode("utf-8", "replace")
    git(workspace, "add", "--intent-to-add", "--all")
    (out / "changes.diff").write_bytes(git(workspace, "diff", "--binary", "HEAD"))
    result = {"id": identity, "task": case["task"], "kind": case["kind"], "repeat": case["repeat"],
              "task_id": task["id"] if task else None, "agent_status": task["status"] if task else None,
              "stop_reason": task["stop_reason"] if task else None, "exception": error,
              "seconds": round(elapsed, 3), "model_requests": task["model_requests"] if task else None,
              "tool_calls": sum(event["kind"] == "tool_start" for event in events),
              **token_usage(task),
              "approvals": approvals, "git_status": status, "acceptance": checked,
              "acceptance_unchanged": file_hash(evaluator) == manifest["acceptance_sha256"]}
    write_json(out / "result.json", result)
    return result


def score(result, review=None):
    """task_ok 为 None 表示还在等人工核对，不算通过也不算失败。"""
    engine_ok = result["agent_status"] == "returned" and not result["exception"]
    scored = {key: result[key] for key in ("id", "task", "repeat", "agent_status", "stop_reason", "model_requests",
                                           "tool_calls", "seconds", "total_tokens")}
    scored.update(known_total_tokens=result.get("known_total_tokens", result["total_tokens"] or 0),
                  usage_missing_requests=result.get("usage_missing_requests", 0))
    scored.update(engine_ok=engine_ok, checks_passed=None, checks_total=None)
    if result["kind"] == "review":
        scored["task_ok"] = None if review is None else review["verdict"] == "passed"
        return scored
    parsed = (result["acceptance"] or {}).get("result")
    scored["task_ok"] = bool(parsed and parsed["passed"])
    if parsed:
        scored.update(checks_passed=sum(item["passed"] for item in parsed["checks"]), checks_total=len(parsed["checks"]))
    return scored


def summarize(results):
    return {"runs": len(results),
            "task_ok": sum(item["task_ok"] is True for item in results),
            "task_pending": sum(item["task_ok"] is None for item in results),
            "engine_ok": sum(item["engine_ok"] for item in results),
            "both_ok": sum(item["task_ok"] is True and item["engine_ok"] for item in results),
            "model_requests": sum(item["model_requests"] or 0 for item in results),
            "tool_calls": sum(item["tool_calls"] for item in results),
            "seconds": round(sum(item["seconds"] for item in results), 1),
            "total_tokens": sum(item["total_tokens"] for item in results)
                            if all(item["total_tokens"] is not None for item in results) else None,
            "known_total_tokens": sum(item.get("known_total_tokens", item["total_tokens"] or 0) for item in results),
            "usage_missing_requests": (sum(item.get("usage_missing_requests", 0) for item in results)
                                       if all(item.get("usage_missing_requests", 0) is not None for item in results) else None)}


def _mark(value):
    return {True: "是", False: "否", None: "待核对"}[value]


def _tokens(item):
    if item["total_tokens"] is not None:
        return str(item["total_tokens"])
    missing = item.get("usage_missing_requests")
    detail = f"{missing} 次请求用量未知" if missing is not None else "用量记录不完整"
    return f"≥{item.get('known_total_tokens', 0)}（{detail}）"


def render_report(manifest, results, totals, baseline=None):
    lines = ["# RecapCLI 评测报告", "", f"引擎提交：`{manifest['engine_commit']}`",
             f"目标提交：`{manifest['target_commit']}`", f"模型：`{manifest['model'].get('model', '未记录')}`", ""]
    lines += [f"验收脚本指纹：`{manifest['acceptance_sha256']}`", ""]
    if manifest.get("recheck_of"):
        lines += ["这是已有产物的离线重评分，未重新运行模型或修复后的引擎；原始结果保持不变。", ""]
    if manifest["engine_status"]:
        lines += ["**注意：引擎工作区有未提交改动，这份结果不能对应到单个提交。**", ""]
    lines += ["| 用例 | 任务做对 | 引擎正常结束 | 验收项 | 终态 | 请求 / 工具 | 耗时 | tokens |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in results:
        checks = f"{item['checks_passed']}/{item['checks_total']}" if item["checks_total"] else "-"
        lines.append(f"| {item['id']} | {_mark(item['task_ok'])} | {_mark(item['engine_ok'])} | {checks} | "
                     f"{item['agent_status']} | {item['model_requests']} / {item['tool_calls']} | "
                     f"{item['seconds']:.1f}s | {_tokens(item)} |")
    labels = (("runs", "运行次数"), ("task_ok", "任务做对"), ("task_pending", "待人工核对"), ("engine_ok", "引擎正常结束"),
              ("both_ok", "两项都通过"), ("model_requests", "模型请求"), ("tool_calls", "工具调用"),
              ("seconds", "Agent 耗时（秒）"), ("total_tokens", "累计 tokens"))
    if baseline is None:
        lines += ["", "| 指标 | 本次 |", "| --- | --- |"]
        lines += [f"| {label} | {_tokens(totals) if key == 'total_tokens' else totals[key]} |" for key, label in labels]
    else:
        before = summarize(baseline["results"])
        lines += ["", f"| 指标 | 基线 {baseline['date']} | 本次 |", "| --- | --- | --- |"]
        lines += [f"| {label} | {_tokens(before) if key == 'total_tokens' else before[key]} | "
                  f"{_tokens(totals) if key == 'total_tokens' else totals[key]} |" for key, label in labels]
        if {item["id"] for item in baseline["results"]} != {item["id"] for item in results}:
            lines += ["", "两侧用例集合不同，累计用量、耗时和通过数量不能直接作为改善幅度。"]
        if baseline["target_fingerprint"] != manifest["target_fingerprint"]:
            lines += ["", "**注意：目标工作区和基线不是同一份字节，对比要打折扣。**"]
        if baseline["acceptance_sha256"] != manifest["acceptance_sha256"]:
            lines += ["", "**注意：验收脚本和基线不同，两边的结果不能直接比较。**"]
        if baseline.get("original_acceptance_sha256"):
            lines += ["", "基线分数是用当前验收离线重评的，原始分数保存在 `baselines/2026-11-03.json`。"]
    lines += ["", BOUNDARY, ""]
    return "\n".join(lines)


def report(run_root, *, baseline_path=BASELINE):
    """只读已有结果，不调用模型。人工核对写在运行目录的 source-reviews.json。"""
    run_root = Path(run_root)
    manifest = read_json(run_root / "manifest.json")
    review_path = run_root / "source-reviews.json"
    reviews = read_json(review_path) if review_path.exists() else {}
    results = []
    for case in manifest["cases"]:
        path = run_root / "results" / case["id"] / "result.json"
        if path.exists():
            result = read_json(path)
            task_path = path.with_name("task.json")
            if task_path.exists():
                result.update(token_usage(read_json(task_path)))
            results.append(score(result, reviews.get(case["id"])))
    totals = summarize(results)
    baseline = read_json(baseline_path) if baseline_path is not None and Path(baseline_path).exists() else None
    text = render_report(manifest, results, totals, baseline)
    write_json(run_root / "summary.json", {"totals": totals, "results": results})
    (run_root / "report.md").write_text(text, encoding="utf-8")
    return text


def recheck(run_root, *, evaluator=EVALUATOR, baseline_path=BASELINE):
    """用新版验收离线重评已有产物；单独存放结果，不覆写原始运行。"""
    run_root = Path(run_root).resolve()
    manifest = read_json(run_root / "manifest.json")
    output = run_root / "rechecks" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True)
    revised = {**manifest, "recheck_of": str(run_root),
               "original_acceptance_sha256": manifest["acceptance_sha256"],
               "acceptance_sha256": file_hash(evaluator)}
    write_json(output / "manifest.json", revised)
    reviews = run_root / "source-reviews.json"
    if reviews.exists():
        write_json(output / reviews.name, read_json(reviews))
    for case in manifest["cases"]:
        source = run_root / "results" / case["id"]
        if not (source / "result.json").exists():
            continue
        result = read_json(source / "result.json")
        task_path = source / "task.json"
        if task_path.exists():
            result.update(token_usage(read_json(task_path)))
        else:
            # 没有逐请求记录时无法纠正旧零值，明确保留未知。
            result.update(token_usage(None))
        if case["kind"] == "acceptance":
            workspace = Path(case["workspace"])
            saved_diff = (source / "changes.diff").read_bytes()
            if (git(workspace, "diff", "--binary", "HEAD") != saved_diff
                    or git(workspace, "ls-files", "--others", "--exclude-standard", "-z")):
                raise RuntimeError(f"{case['id']}：产物已变化，不能按原运行重评")
            result["acceptance"] = acceptance(evaluator, case["task"], workspace)
            if file_hash(evaluator) != revised["acceptance_sha256"]:
                raise RuntimeError("验收脚本在重评期间发生变化，结果未保存")
        result.update(source_result=str(source / "result.json"), acceptance_unchanged=True)
        destination = output / "results" / case["id"]
        destination.mkdir(parents=True)
        write_json(destination / "result.json", result)
    report(output, baseline_path=baseline_path)
    return output


def rescore_baseline(run_dir, *, source=ORIGINAL_BASELINE, destination=BASELINE, evaluator=EVALUATOR):
    """用当前验收离线重评基线产物，另存新文件；原始基线和人工核对结论不变。"""
    run_dir = Path(run_dir).resolve()
    baseline = read_json(source)
    results = []
    for item in baseline["results"]:
        if item["checks_total"] is None:
            results.append(item)
            continue
        workspace = run_dir / "workspaces" / item["id"]
        saved_diff = (run_dir / "results" / item["id"] / "changes.diff").read_bytes()
        # 按目标提交比，不用 HEAD：基线工作区后来多过提交，文件内容才是要核对的。
        if git(workspace, "diff", "--binary", baseline.get("target_commit", "HEAD")) != saved_diff:
            raise RuntimeError(f"{item['id']}：基线产物已变化，不能重评")
        parsed = acceptance(evaluator, item["task"], workspace).get("result")
        if parsed is None:
            raise RuntimeError(f"{item['id']}：验收没有返回结果")
        results.append({**item, "task_ok": parsed["passed"], "checks_total": len(parsed["checks"]),
                        "checks_passed": sum(check["passed"] for check in parsed["checks"])})
    write_json(destination, {**baseline, "acceptance_sha256": file_hash(evaluator),
                             "original_acceptance_sha256": baseline["acceptance_sha256"], "results": results})
    return destination


def model_identity(config):
    return {"model": config.model, "profile": config.profile, "protocol": config.protocol,
            "provider_host": urlsplit(config.base_url or "https://api.openai.com").hostname,
            "streaming": config.streaming}


def latest_run(runs=RUNS):
    return Path((Path(runs) / "latest.txt").read_text(encoding="utf-8"))


def run_real(case_id=None):
    run_root = latest_run()
    manifest = read_json(run_root / "manifest.json")
    cases = [case for case in manifest["cases"] if case_id in (None, case["id"])]
    if not cases:
        raise ValueError(f"没有用例 {case_id}")
    for case in cases:
        config = load_config()
        if model_identity(config) != manifest["model"]:
            raise RuntimeError("模型配置和准备时不一致")
        print(f"开始 {case['id']}", flush=True)
        with ModelClient(config, client_factory=OpenAI) as client:
            result = run_case(run_root, manifest, case, client, config.model)
        print(f"结束 {case['id']}：{result['agent_status']}，{result['seconds']:.1f} 秒", flush=True)


def build_parser():
    parser = argparse.ArgumentParser(description="在固定目标仓库上运行 RecapCLI 评测")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare", help="准备工作区，不调用模型")
    run = actions.add_parser("run", help="调用真实模型运行，会产生费用")
    run.add_argument("--real-model", action="store_true", help="确认调用真实模型")
    run.add_argument("--case", help="只运行一例，如 bugfix-1")
    actions.add_parser("report", help="汇总已有结果，不调用模型")
    recheck_parser = actions.add_parser("recheck", help="离线重评已有产物，另存结果，不调用模型")
    recheck_parser.add_argument("--run-dir", type=Path, help="原运行目录，默认最近一次运行")
    rescore = actions.add_parser("rescore-baseline", help="用当前验收离线重评基线产物，不调用模型")
    rescore.add_argument("--run-dir", type=Path, required=True, help="基线运行目录")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    # 语义检索走另一个付费接口，评测里不启用。
    os.environ.update(RECAPCLI_EMBEDDING_MODEL="", RECAPCLI_EMBEDDING_BASE_URL="", PYTHONUTF8="1")
    if args.action == "prepare":
        print(prepare(read_json(SPEC), model=model_identity(load_config())))
    elif args.action == "run":
        if not args.real_model:
            parser.error("run 会调用真实模型并产生费用；确认后加 --real-model")
        run_real(args.case)
    elif args.action == "recheck":
        print(recheck(args.run_dir or latest_run()))
    elif args.action == "rescore-baseline":
        print(rescore_baseline(args.run_dir))
    else:
        print(report(latest_run()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
