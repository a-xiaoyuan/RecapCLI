"""用户显式配置的需求验收和逐项证据。"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .auto_verify import run_check_command

MAX_ACCEPTANCE_ROUNDS = 3
MAX_CONFIG_BYTES = 65536
MAX_CHECKS = 12
CHECK_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}")


@dataclass(frozen=True)
class AcceptanceCheck:
    id: str
    description: str
    argv: tuple[str, ...]
    exit_code: int
    contains: tuple[str, ...]
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class AcceptanceSuite:
    sha256: str
    checks: tuple[AcceptanceCheck, ...]

    def catalog(self):
        return [{"id": check.id, "description": check.description} for check in self.checks]


def load_acceptance(path):
    try:
        with Path(path).open("rb") as file:
            raw = file.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("配置超过 64 KiB")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or set(data) != {"checks"}:
            raise ValueError("配置必须是仅含 checks 的 JSON 对象")
        items = data["checks"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_CHECKS:
            raise ValueError(f"checks 必须包含 1 到 {MAX_CHECKS} 项")
        checks = tuple(_parse_check(item) for item in items)
        if len({check.id for check in checks}) != len(checks):
            raise ValueError("验收 ID 重复")
        return AcceptanceSuite(hashlib.sha256(raw).hexdigest(), checks)
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"--accept 无法读取配置：{error}") from error


def _parse_check(item):
    if not isinstance(item, dict) or set(item) - {
            "id", "description", "argv", "exit_code", "output_contains", "output_excludes"}:
        raise ValueError("验收项只允许 id、description、argv、exit_code、output_contains、output_excludes")
    identifier = item.get("id")
    if not isinstance(identifier, str) or not CHECK_ID.fullmatch(identifier):
        raise ValueError("验收 ID 必须是 1 到 40 个字母、数字、连字符或下划线，并以字母或数字开头")
    description = item.get("description", identifier)
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise ValueError(f"{identifier} 的 description 必须是 1 到 300 字的说明")
    argv = item.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0].strip() or sum(len(arg.encode("utf-8")) for arg in argv) > 32768):
        raise ValueError(f"{identifier} 的 argv 必须是有效命令参数数组")
    exit_code = item.get("exit_code", 0)
    if type(exit_code) is not int or not 0 <= exit_code <= 255:
        raise ValueError(f"{identifier} 的 exit_code 必须是 0 到 255 的整数")
    patterns = []
    for key in ("output_contains", "output_excludes"):
        values = item.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"{identifier} 的 {key} 必须是非空字符串的数组")
        patterns.append(tuple(values))
    return AcceptanceCheck(identifier, description.strip(), tuple(argv), exit_code, *patterns)


def _failure(check, result):
    if result["status"] != "finished" or not result["ok"]:
        return f"命令未正常结束（{result['status']}）"
    if result["returncode"] != check.exit_code:
        return f"期望退出码 {check.exit_code}，实际 {result['returncode']}"
    output = result["output"]
    if result.get("output_truncated") and (check.contains or check.excludes):
        return "实际输出超出读取上限，无法判断输出断言"
    for expected in check.contains:
        if expected not in output:
            return f"实际输出缺少 {expected[:150]!r}"
    for forbidden in check.excludes:
        if forbidden in output:
            return f"实际输出包含禁止内容 {forbidden[:150]!r}"
    return ""


def run_acceptance(run, attempt):
    ctx = run.ctx
    checks = ctx.acceptance.checks
    run.trace("acceptance_round", attempt=attempt)
    passed = failed = 0
    feedback = []
    for check in checks:
        if ctx.budget.time_low():
            break
        ctx.on_event(f"[验收] {check.id} · {check.description}")
        result = run_check_command(run, check.argv)
        reason = _failure(check, result)
        output = result["output"]
        run.trace("acceptance_check", attempt=attempt, id=check.id, status="failed" if reason else "passed",
                  reason=reason, record_id=result["record_id"], returncode=result["returncode"],
                  output=output[-4000:], output_truncated=result.get("output_truncated", False) or len(output) > 4000)
        if reason:
            failed += 1
            feedback.append(f"{check.id}（{check.description}）未通过：{reason}\n实际输出末尾：{output[-500:]}")
        else:
            passed += 1
    not_run = len(checks) - passed - failed
    label = acceptance_label(passed, failed, not_run)
    ctx.on_event(f"[验收] {label}")
    return not failed and not not_run, label, "\n".join(feedback)[:3500], not_run


def acceptance_label(passed, failed, not_run):
    return f"需求验收 {passed}/{passed + failed + not_run} 通过，{failed} 项失败，{not_run} 项未运行"


def summarize_acceptance(events):
    """取最近一次仍适用于当前产物的结果。早先的证据留在事件记录中。"""
    catalog, results, attempt = None, {}, None
    for event in events:
        data = event["data"]
        if event["kind"] == "acceptance_config":
            catalog = data
        elif event["kind"] == "acceptance_round":
            attempt, results = data["attempt"], {}
        elif event["kind"] == "acceptance_check":
            results[data["id"]] = data
        elif event["kind"] == "acceptance_invalidated":
            attempt, results = None, {}
    if catalog is None:
        return None
    checks = [{**check, **results.get(check["id"], {"status": "not_run"})} for check in catalog["checks"]]
    counts = {status: sum(check["status"] == status for check in checks) for status in ("passed", "failed", "not_run")}
    return {**counts, "total": len(checks), "attempt": attempt, "sha256": catalog["sha256"], "checks": checks}
