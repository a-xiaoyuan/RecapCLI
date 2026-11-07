"""离线故障注入：工具已经改过文件之后，再模拟 429、5xx、断连和上下文超限。

不调用真实模型。随机计划由固定种子生成，同一种子可复跑。

    python -X utf8 evals/fault_injection.py
    python -X utf8 evals/fault_injection.py --seed 20261107 --trials 30

统计三项：正常收尾（没有异常，终态为 returned 或 incomplete，且说明非空）、
已执行编辑是否原样留在文件里、工具是否比模型成功发出的调用多执行了一次。
"""

import argparse
import json
import random
import tempfile
from pathlib import Path

import httpx2
from openai import OpenAI

from recapcli.core.agent import AgentLimits, run_agent
from recapcli.tools.approval import approve_all

SEED = 20261107
TRIALS = 30
KINDS = ("429", "5xx", "disconnect", "context_overflow")
SERVER_STATUSES = (500, 502, 503, 504)


def build_schedule(seed=SEED, trials=TRIALS):
    """生成全部试验的计划。只在这里使用随机数，执行阶段不再抽样。"""
    rng = random.Random(seed)
    schedule = []
    for kind in KINDS:
        for index in range(1, trials + 1):
            edit_count = rng.randint(1, 3)
            extra_steps = rng.randint(0, 2)
            steps = ["edit"] * edit_count
            for _ in range(extra_steps):
                steps.append(rng.choice(("edit", "read")))
            max_retries = rng.choice((0, 1))
            schedule.append({
                "kind": kind,
                "trial": index,
                "steps": steps,
                "max_retries": max_retries,
                "retry_also_fails": bool(rng.randrange(2)) if max_retries else False,
                "summary_fails": bool(rng.randrange(2)),
                "status_code": rng.choice(SERVER_STATUSES) if kind == "5xx" else None,
            })
    return schedule


def _completion(message, finish_reason):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "fault-injection",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


def _tool_response(call_id, name, arguments):
    return httpx2.Response(200, json=_completion(
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
        "tool_calls"))


def _text_response(content):
    return httpx2.Response(200, json=_completion({"role": "assistant", "content": content}, "stop"))


def _fault_response(kind, status_code, request):
    if kind == "disconnect":
        raise httpx2.ConnectError("offline disconnect", request=request)
    if kind == "context_overflow":
        return httpx2.Response(400, json={"error": {
            "message": "This model's maximum context length is 8192 tokens.",
            "code": "context_length_exceeded"}})
    status = 429 if kind == "429" else status_code
    message = "rate limit" if status == 429 else "unavailable"
    return httpx2.Response(status, json={"error": {"message": message, "code": None}},
                           headers={"retry-after": "0"})


def _planned_calls(spec):
    """按计划算出将要发出的工具调用，以及这些编辑全部落地后的文件内容。"""
    content = "版本: 0\n"
    calls = []
    for step_index, step in enumerate(spec["steps"]):
        call_id = f"{spec['kind']}-{spec['trial']}-{step_index}"
        if step == "edit":
            new_text = f"版本: {spec['trial']}-{step_index}\n故障: {spec['kind']}\n"
            arguments = {"path": "notes.txt", "old_text": content, "new_text": new_text}
            content = new_text
            name = "edit_file"
        else:
            arguments = {"path": "notes.txt"}
            name = "read_file"
        calls.append({"id": call_id, "name": name, "arguments": arguments})
    return calls, content


class _ScriptedModel:
    def __init__(self, spec, calls):
        self.spec = spec
        self.calls = calls
        self.issued = []
        self.faults = 0
        self.summary_faults = 0

    def __call__(self, request):
        body = json.loads(request.content)
        is_final = body.get("tool_choice") == "none" or not body.get("tools")
        if is_final:
            if self.spec["summary_fails"] and self.summary_faults == 0:
                self.summary_faults += 1
                return _fault_response(self.spec["kind"], self.spec["status_code"], request)
            return _text_response("已根据已有事实收尾。")
        if len(self.issued) < len(self.calls):
            call = self.calls[len(self.issued)]
            self.issued.append(call["id"])
            return _tool_response(call["id"], call["name"], call["arguments"])
        self.faults += 1
        if self.faults == 1 or (self.spec["retry_also_fails"] and self.faults == 2):
            return _fault_response(self.spec["kind"], self.spec["status_code"], request)
        return _text_response("故障已恢复，不再改文件。")


def run_trial(spec):
    calls, expected = _planned_calls(spec)
    model = _ScriptedModel(spec, calls)
    traces = []
    steps = len(spec["steps"])
    max_steps = steps + spec["max_retries"] + 1 + spec["max_retries"] + 2
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "notes.txt").write_text("版本: 0\n", encoding="utf-8")
        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(model)))
        thrown = None
        result = None
        try:
            with client:
                result = run_agent(client, "fault-injection", "请把 notes.txt 改成计划中的版本", workspace,
                                   max_steps=max_steps, on_event=lambda _message: None,
                                   limits=AgentLimits(max_retries=spec["max_retries"]),
                                   capabilities=("read", "write"), approver=approve_all,
                                   on_trace=lambda kind, data: traces.append((kind, data)))
        except Exception as error:  # noqa: BLE001 — 试验要记下引擎抛出的异常，而不是让整批中断
            thrown = error
        actual = (workspace / "notes.txt").read_text(encoding="utf-8")
    executed = [data["call_id"] for kind, data in traces if kind == "tool_start" and not data.get("skipped")]
    answer = "" if result is None else (result.answer or "")
    status = None if result is None else result.status
    normal = thrown is None and status in {"returned", "incomplete"} and bool(answer.strip())
    return {
        "kind": spec["kind"],
        "trial": spec["trial"],
        "steps": spec["steps"],
        "max_retries": spec["max_retries"],
        "retry_also_fails": spec["retry_also_fails"],
        "summary_fails": spec["summary_fails"],
        "status_code": spec["status_code"],
        "status": status,
        "stop_reason": None if result is None else result.stop_reason,
        "exception": None if thrown is None else type(thrown).__name__,
        "normal_finish": normal,
        "preserved": actual == expected,
        "duplicate": executed != [call["id"] for call in calls],
        "issued": [call["id"] for call in calls],
        "executed": executed,
    }


def summarize(trials):
    kinds = []
    for kind in KINDS:
        rows = [item for item in trials if item["kind"] == kind]
        kinds.append({
            "kind": kind,
            "trials": len(rows),
            "normal_finish": sum(item["normal_finish"] for item in rows),
            "preserved": sum(item["preserved"] for item in rows),
            "duplicate_runs": sum(item["duplicate"] for item in rows),
            "returned": sum(item["status"] == "returned" for item in rows),
            "incomplete": sum(item["status"] == "incomplete" for item in rows),
            "exceptions": sum(item["exception"] is not None for item in rows),
        })
    return kinds


def run_suite(seed=SEED, trials=TRIALS):
    schedule = build_schedule(seed, trials)
    results = [run_trial(spec) for spec in schedule]
    return {"seed": seed, "trials_per_kind": trials, "kinds": summarize(results), "results": results}


def format_table(report):
    lines = [
        f"种子 {report['seed']}，每种 {report['trials_per_kind']} 次",
        "",
        "| 故障 | 次数 | 正常收尾 | 编辑保留 | 出现重复执行 | returned | incomplete | 异常 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report["kinds"]:
        lines.append(
            f"| {item['kind']} | {item['trials']} | {item['normal_finish']} | {item['preserved']} | "
            f"{item['duplicate_runs']} | {item['returned']} | {item['incomplete']} | {item['exceptions']} |")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线复跑故障注入")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--json", type=Path, help="把完整结果写到这个路径")
    args = parser.parse_args(argv)
    report = run_suite(args.seed, args.trials)
    print(format_table(report))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
