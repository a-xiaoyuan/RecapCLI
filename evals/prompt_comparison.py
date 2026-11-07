"""在固定引擎上比较一行提示词，准备和复核均可离线运行。"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI  # noqa: E402

from evals import runner  # noqa: E402
from recapcli.config import load_config  # noqa: E402
from recapcli.core import agent  # noqa: E402
from recapcli.llm.client import ModelClient  # noqa: E402
from recapcli.llm.streaming import chat_stream  # noqa: E402

BASE_ENGINE = "6b2392fa16b64775d454a849cf0a23047d155388"
ENGINE_INPUTS = ("recapcli", "pyproject.toml", "uv.lock", "evals/tasks.json", "evals/acceptance.py")
RUNS = runner.RUNS / "prompt-comparison"
ANCHOR = "- 验证优先跑与改动相关的最小测试，不要一上来跑全量；按项目约定（AGENTS.md）选择命令。argv 不经 shell，Python 用提供的解释器路径。"
CANDIDATE = "- 改完要运行真实场景并看输出内容，不能只断言关键词；自己补的测试通过不等于结果正确。"
ORDER = (("control", 1), ("candidate", 1), ("control", 2), ("candidate", 2))


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_engine_commit():
    if runner.git(runner.REPO, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("请先提交本地改动，再从干净工作区准备或运行对照")
    return runner.git(runner.REPO, "rev-parse", "HEAD").decode().strip()


def _engine_inputs():
    if runner.git(runner.REPO, "status", "--porcelain=v1", "--untracked-files=all", "--", *ENGINE_INPUTS):
        raise ValueError("引擎、依赖或评测输入有本地改动")
    baseline = {}
    for path in ENGINE_INPUTS:
        base = runner.git(runner.REPO, "rev-parse", f"{BASE_ENGINE}:{path}").decode().strip()
        current = runner.git(runner.REPO, "rev-parse", f"HEAD:{path}").decode().strip()
        if current != base:
            raise ValueError(f"{path} 与固定引擎 {BASE_ENGINE[:7]} 不同")
        baseline[path] = base
    baseline["evals/runner.py"] = runner.file_hash(runner.EVALS / "runner.py")
    return baseline


def _prompts():
    control = agent.BASE_SYSTEM_PROMPT
    if control.count(ANCHOR) != 1 or CANDIDATE in control:
        raise ValueError("固定引擎的提示词锚点已变化")
    return {"control": control, "candidate": control.replace(ANCHOR, f"{ANCHOR}\n{CANDIDATE}")}


def prepare(config):
    commit = _clean_engine_commit()
    inputs = _engine_inputs()
    prompts = _prompts()
    spec = runner.read_json(runner.SPEC)
    spec = {**spec, "repetitions": len(ORDER), "tasks": [item for item in spec["tasks"] if item["id"] == "bugfix"]}
    root = runner.prepare(spec, runs=RUNS, model=runner.model_identity(config))
    manifest = runner.read_json(root / "manifest.json")
    if manifest["engine_commit"] != commit or manifest["engine_status"]:
        raise ValueError("准备时工作区发生变化，请从干净提交重新准备")
    for case, (variant, repeat) in zip(manifest["cases"], ORDER, strict=True):
        case.update(id=f"{variant}-{repeat}", variant=variant, repeat=repeat)
    manifest["experiment"] = {
        "engine_baseline": BASE_ENGINE,
        "engine_inputs": inputs,
        "prompt_sha256": {variant: _hash(prompt) for variant, prompt in prompts.items()},
        "candidate_line": CANDIDATE,
        "order": [case["id"] for case in manifest["cases"]],
        "decision_rule": "候选组 task_ok 更多，且 engine_ok 不少于对照组",
        "cost_estimate_tokens": [1600000, 1900000],
    }
    runner.write_json(root / "manifest.json", manifest)
    return root


def probe(config):
    identity = runner.model_identity(config)
    now = datetime.now(timezone.utc)
    root = runner.RUNS / "probes" / now.strftime("%Y%m%d-%H%M%S-%fZ")
    root.mkdir(parents=True)
    evidence = {"model": identity, "started_at": now.isoformat(), "ready": False, "request_count": 1,
                "output_token_limit": 64, "retries": 0}
    started = time.monotonic()
    messages = [{"role": "user", "content": "Reply only OK."}]
    try:
        with OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0, timeout=20.0) as sdk:
            if identity["protocol"] == "responses":
                response = sdk.responses.create(model=config.model, input="Reply only OK.", max_output_tokens=64)
                evidence.update(ready=response.status == "completed" and bool(response.output_text.strip()),
                                finish=response.status, total_tokens=response.usage.total_tokens if response.usage else None)
            else:
                if identity["streaming"]:
                    response = chat_stream(sdk, model=config.model, messages=messages, options={"max_tokens": 64},
                                           timeout=20, on_delta=None, check=None)
                else:
                    response = sdk.chat.completions.create(model=config.model, messages=messages, max_tokens=64)
                choice = response.choices[0]
                evidence.update(ready=choice.finish_reason == "stop" and bool((choice.message.content or "").strip()),
                                finish=choice.finish_reason, total_tokens=response.usage.total_tokens if response.usage else None)
    except Exception as error:
        evidence.update(error_type=type(error).__name__, http_status=getattr(error, "status_code", None))
    evidence["seconds"] = round(time.monotonic() - started, 3)
    path = root / "result.json"
    runner.write_json(path, evidence)
    return path, evidence["ready"]


def summarize_comparison(root, manifest):
    groups = {"control": [], "candidate": []}
    for case in manifest["cases"]:
        path = root / "results" / case["id"] / "result.json"
        if path.exists():
            groups[case["variant"]].append(runner.score(runner.read_result(path)))
    totals = {variant: runner.summarize(results) for variant, results in groups.items()}
    complete = all(totals[variant]["valid_runs"] == 2 and totals[variant]["invalid_runs"] == 0 for variant in groups)
    supported = None
    if complete:
        supported = (totals["candidate"]["task_ok"] > totals["control"]["task_ok"]
                     and totals["candidate"]["engine_ok"] >= totals["control"]["engine_ok"])
    return {"completed": complete, "groups": totals, "supports_candidate": supported}


def run(root, probe_path):
    manifest = runner.read_json(root / "manifest.json")
    evidence = runner.read_json(probe_path)
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(evidence.get("started_at", ""))
    except (TypeError, ValueError):
        age = None
    if (evidence.get("ready") is not True or evidence.get("model") != manifest["model"] or age is None
            or not timedelta(0) <= age <= timedelta(minutes=30)):
        raise ValueError("需要同一模型端点最近 30 分钟内的成功探测")
    if manifest.get("engine_status") or _clean_engine_commit() != manifest["engine_commit"]:
        raise ValueError("请从当前干净提交重新准备对照目录")
    prompts = _prompts()
    experiment = manifest["experiment"]
    expected = [(f"{variant}-{repeat}", variant, repeat, "bugfix") for variant, repeat in ORDER]
    actual = [(case["id"], case["variant"], case["repeat"], case["task"]) for case in manifest["cases"]]
    if actual != expected or experiment["engine_baseline"] != BASE_ENGINE:
        raise ValueError("运行清单与固定的四例对照设计不一致")
    if experiment["prompt_sha256"] != {variant: _hash(prompt) for variant, prompt in prompts.items()}:
        raise ValueError("提示词与准备时不一致")
    if (root / "results").exists():
        raise ValueError("已有用例产物，请保留本轮并重新准备，避免重放")
    control = prompts["control"]
    for case in manifest["cases"]:
        if _engine_inputs() != experiment["engine_inputs"]:
            raise ValueError("引擎与准备时不一致")
        config = load_config()
        if runner.model_identity(config) != manifest["model"]:
            raise ValueError("模型配置与准备时不一致")
        print(f"开始 {case['id']}", flush=True)
        try:
            agent.BASE_SYSTEM_PROMPT = prompts[case["variant"]]
            with ModelClient(config, client_factory=OpenAI) as client:
                result = runner.run_case(root, manifest, case, client, config.model)
        finally:
            agent.BASE_SYSTEM_PROMPT = control
        comparison = summarize_comparison(root, manifest)
        comparison["stop_reason"] = result.get("invalid_reason")
        runner.write_json(root / "comparison.json", comparison)
        runner.report(root, baseline_path=None)
        if comparison["stop_reason"]:
            print(f"停止：{comparison['stop_reason']}", flush=True)
            break
    return comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定 6b2392f 的 bugfix 提示词对照")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("probe", help="只发一条有限输出的探测请求")
    actions.add_parser("prepare", help="离线准备四份工作区和对照清单")
    execute = actions.add_parser("run", help="按固定顺序调用真实模型")
    execute.add_argument("--run-dir", type=Path, required=True)
    execute.add_argument("--probe", type=Path, required=True)
    execute.add_argument("--real-model", action="store_true")
    report = actions.add_parser("report", help="只读重算已有对照结论")
    report.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ.update(RECAPCLI_EMBEDDING_MODEL="", RECAPCLI_EMBEDDING_BASE_URL="", PYTHONUTF8="1")
    if args.action == "prepare":
        print(prepare(load_config()))
    elif args.action == "probe":
        path, ready = probe(load_config())
        print(path)
        return 0 if ready else 1
    elif args.action == "run":
        if not args.real_model:
            parser.error("run 会产生费用，请加 --real-model")
        comparison = run(args.run_dir.resolve(), args.probe)
        print(json.dumps(comparison, ensure_ascii=False))
        return 0 if comparison["completed"] else 1
    else:
        manifest = runner.read_json(args.run_dir / "manifest.json")
        print(json.dumps(summarize_comparison(args.run_dir, manifest), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
