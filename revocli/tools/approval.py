"""写入和命令的逐次人工审批。

批准绑定工具名、工作区、最终参数和相关文件内容指纹。
批准后立刻重算；参数或指纹变了就作废，不执行。
澄清、附和和无法识别的答复都不是批准。
"""

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time

from ..storage.files import data_root
from .editing import _hash, _plan_edit, _read_bytes, _target
from .filesystem import ToolError, _relative

APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CANCELLED = "cancelled"
INVALIDATED = "invalidated"
NEEDS_APPROVAL = "needs_approval"

_APPROVER_OUTCOMES = {APPROVED, REJECTED, EXPIRED, CANCELLED, NEEDS_APPROVAL}
_COMMANDS = {
    "approve": APPROVED,
    "/approve": APPROVED,
    "批准": APPROVED,
    "reject": REJECTED,
    "/reject": REJECTED,
    "拒绝": REJECTED,
    "/拒绝": REJECTED,
    "cancel": CANCELLED,
    "/cancel": CANCELLED,
    "取消": CANCELLED,
    "/取消": CANCELLED,
    "/批准": APPROVED,
}
_REASONS = {
    APPROVED: "已批准",
    REJECTED: "已拒绝，未执行",
    EXPIRED: "审批已过期，未执行",
    CANCELLED: "审批已取消，未执行",
    INVALIDATED: "批准已失效：参数或文件指纹已变化，未执行",
    NEEDS_APPROVAL: "非交互模式没有批准，未执行；请在终端中交互运行，或显式传入 --yes",
}
_BACKUP_ID = re.compile(r"[0-9a-f]{32}")
_HINT_LIMIT = 1500
_DISPLAY_LIMIT = 200


class ApprovalRequest:
    """一次待批动作。params 是展示用副本，改它会使批准失效。"""

    def __init__(self, tool, workspace, params, fingerprints, summary, binding, time_remaining=None):
        self.tool = tool
        self.workspace = workspace
        self.params = params
        self.fingerprints = fingerprints
        self.summary = summary
        self.binding = binding
        self.time_remaining = time_remaining
        self.check = None


def approve_all(request):
    """显式跳过提问。执行前仍复核参数和文件指纹。"""
    return APPROVED


def fail_closed(request):
    """没有交互审批人时不执行。"""
    return NEEDS_APPROVAL


def interpret_approval_line(line):
    """只接受完整的批准、拒绝或取消命令。其他文字返回 None。"""
    if not isinstance(line, str):
        return None
    return _COMMANDS.get(" ".join(line.strip().split()).lower())


def normalize_outcome(value):
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _APPROVER_OUTCOMES:
            return token
    return None


def binding_key(tool, workspace, params, fingerprints):
    try:
        raw = json.dumps({
            "fingerprints": fingerprints,
            "params": params,
            "tool": tool,
            "workspace": str(workspace),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical(params):
    try:
        return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _binding_fingerprints(items):
    compact = [{"fingerprint": str(item.get("fingerprint")), "path": str(item.get("path"))} for item in items]
    compact.sort(key=lambda item: (item["path"], item["fingerprint"]))
    return compact


def _file_fingerprint(workspace, relative):
    label = relative if isinstance(relative, str) else "?"
    try:
        target = _target(workspace, relative)
        label = _relative(workspace, target)
    except (ToolError, OSError, ValueError):
        return {"path": label, "fingerprint": "unavailable"}
    if not target.exists():
        return {"path": label, "fingerprint": "absent"}
    try:
        return {"path": label, "fingerprint": _hash(_read_bytes(target))}
    except ToolError:
        try:
            size = target.stat().st_size
            with target.open("rb") as handle:
                head = handle.read(1024 * 1024)
            return {"path": label, "fingerprint": f"oversize:{size}:{_hash(head)}"}
        except OSError:
            return {"path": label, "fingerprint": "unreadable"}
    except OSError:
        return {"path": label, "fingerprint": "unreadable"}


def _command_relative(cwd, arg):
    if not isinstance(arg, str) or not arg or arg.startswith("-") or len(arg) > 240:
        return None
    if any(char in arg for char in "\x00\n\r\t ;|&<>"):
        return None
    path = Path(arg)
    if path.is_absolute() or path.drive or ".." in path.parts:
        return None
    if not (path.suffix or "/" in arg or "\\" in arg):
        return None
    base = Path(cwd) if isinstance(cwd, str) and cwd not in {"", "."} else Path()
    if base.is_absolute() or base.drive or ".." in base.parts:
        return None
    relative = base / path
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return relative.as_posix()


def _command_fingerprints(workspace, params):
    cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else "."
    argv = params.get("argv") if isinstance(params.get("argv"), list) else []
    items = []
    seen = set()
    for arg in argv:
        relative = _command_relative(cwd, arg)
        if relative is None or relative in seen:
            continue
        seen.add(relative)
        items.append(_file_fingerprint(workspace, relative))
    return items


def _restore_fingerprints(workspace, params):
    backup_id = params.get("backup_id")
    if not isinstance(backup_id, str) or not _BACKUP_ID.fullmatch(backup_id):
        return [{"path": "backup", "fingerprint": "invalid"}]
    try:
        record = data_root(workspace) / "backups" / f"{backup_id}.json"
        label = record.relative_to(Path(workspace).resolve()).as_posix()
    except (OSError, ValueError):
        return [{"path": "backup", "fingerprint": "unavailable"}]
    if not record.exists():
        return [{"path": label, "fingerprint": "absent"}]
    if record.is_symlink() or record.is_junction():
        return [{"path": label, "fingerprint": "symlink"}]
    try:
        if record.stat().st_size > 2 * 1024 * 1024:
            return [{"path": label, "fingerprint": f"oversize:{record.stat().st_size}"}]
        blob = record.read_bytes()
    except OSError:
        return [{"path": label, "fingerprint": "unreadable"}]
    items = [{"path": label, "fingerprint": _hash(blob)}]
    try:
        data = json.loads(blob.decode("utf-8"))
        target = data.get("path") if isinstance(data, dict) else None
    except (UnicodeError, json.JSONDecodeError, ValueError):
        target = None
    if isinstance(target, str):
        items.append(_file_fingerprint(workspace, target))
    return items


def collect_fingerprints(tool, workspace, params):
    try:
        if tool == "edit_file":
            path = params.get("path")
            return [_file_fingerprint(workspace, path if isinstance(path, str) else "")]
        if tool == "restore_file":
            return _restore_fingerprints(workspace, params)
        if tool == "run_command":
            return _command_fingerprints(workspace, params)
    except (OSError, ValueError, TypeError):
        return [{"path": tool, "fingerprint": "unavailable"}]
    return []


def _display(value, limit=_DISPLAY_LIMIT):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…（共{len(value)}字符，批准绑定完整参数）"
    if isinstance(value, list):
        shown = [_display(item, limit) for item in value[:32]]
        if len(value) > 32:
            shown.append(f"…（共{len(value)}项）")
        return shown
    if isinstance(value, dict):
        return {str(key): _display(item, limit) for key, item in value.items()}
    return value


def _edit_hint(workspace, params):
    try:
        planned = _plan_edit(workspace, params.get("path"), params.get("old_text", ""),
                             params.get("new_text", ""), params.get("replace_all", False))
    except (ToolError, OSError, ValueError, TypeError) as error:
        return f"执行前检查未通过：{error}"
    if not planned["changed"]:
        return "按当前文件内容，这次替换不会改变文件。"
    hint = planned["diff"] or "将修改文件。"
    if planned["diff_truncated"]:
        hint += "\n（差异已截断）"
    return hint


def _action_hint(tool, workspace, params):
    if tool == "edit_file":
        return _edit_hint(workspace, params)
    if tool == "restore_file":
        return "将按备份恢复目标文件；目标或备份已变化时不会覆盖。"
    if tool == "run_command":
        return "将在工作区内以前台进程运行 argv，不经过 shell。"
    return ""


def build_summary(tool, workspace, params, fingerprints):
    fingerprint_text = "\n".join(f"{item['path']} {item['fingerprint']}" for item in fingerprints)
    if not fingerprint_text:
        fingerprint_text = "无相关文件；批准只绑定工作区与最终参数"
    hint = _action_hint(tool, workspace, params)
    if len(hint) > _HINT_LIMIT:
        hint = hint[:_HINT_LIMIT] + "\n…（差异显示已截断，批准仍绑定完整参数和指纹）"
    lines = [
        "[审批] 需要明确批准后才会执行",
        f"工具：{tool}",
        f"工作区：{workspace}",
        "参数：" + json.dumps(_display(params), ensure_ascii=False, sort_keys=True),
        "文件指纹：",
        fingerprint_text,
    ]
    if hint:
        lines.extend(["差异提示：", hint])
    lines.append("请输入 approve、reject 或 cancel。其他文字不会被当作批准。")
    return "\n".join(lines)


def _reason(outcome, token):
    if token is None and outcome == REJECTED:
        return "无法识别的审批结果，未执行"
    return _REASONS[outcome]


def authorize_action(tool, workspace, params, *, approver, on_approval=None, time_remaining=None):
    """返回 None 表示可以按调用方持有的原参数执行，否则返回未执行的结构化结果。"""
    sealed = deepcopy(params)
    original_fp = _binding_fingerprints(collect_fingerprints(tool, workspace, sealed))
    key = binding_key(tool, workspace, sealed, original_fp)
    request = ApprovalRequest(
        tool, str(workspace), deepcopy(sealed), deepcopy(original_fp),
        build_summary(tool, workspace, sealed, original_fp), key, time_remaining,
    )
    try:
        raw = approver(request)
    except (KeyboardInterrupt, SystemExit):
        raise
    token = normalize_outcome(raw)
    outcome = token or REJECTED
    fresh_fp = _binding_fingerprints(collect_fingerprints(tool, workspace, sealed))
    params_changed = _canonical(request.params) != _canonical(sealed)
    files_changed = fresh_fp != original_fp
    if outcome == APPROVED and (binding_key(tool, workspace, request.params, fresh_fp) != key
                                or params_changed or files_changed):
        outcome = INVALIDATED
    reason = _reason(outcome, None if outcome == INVALIDATED else token)
    if on_approval is not None:
        digest = _canonical(sealed)
        on_approval({
            "tool": tool,
            "outcome": outcome,
            "reason": reason,
            "workspace": str(workspace),
            "fingerprints": fresh_fp if outcome == INVALIDATED else original_fp,
            "params_sha256": hashlib.sha256(digest.encode("utf-8")).hexdigest() if digest is not None else None,
            "params_changed": params_changed,
            "files_changed": files_changed,
        })
    if outcome == APPROVED:
        return None
    return {"ok": False, "status": outcome, "approval": outcome, "error": f"{reason}（{tool}）",
            "tool": tool, "fingerprints": fresh_fp if outcome == INVALIDATED else original_fp}


def read_line_with_timeout(timeout, stream=None):
    """等待一行输入。超时返回 None，输入关闭抛出 EOFError。"""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        return None
    stream = sys.stdin if stream is None else stream
    if os.name != "nt":
        import select
        try:
            ready, _, _ = select.select([stream], [], [], timeout)
        except (OSError, ValueError):
            return _read_line_thread(timeout)
        if not ready:
            return None
        line = stream.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")
    return _read_line_thread(timeout)


def _read_line_thread(timeout):
    # Windows 的 select 不能等待控制台。超时后该线程仍可能占着下一次输入。
    box = {}

    def target():
        try:
            box["line"] = input()
        except EOFError:
            box["eof"] = True
        except BaseException as error:
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    if box.get("eof"):
        raise EOFError
    return box.get("line", "")


class InteractiveApprover:
    """交互批准。只认 approve / reject / cancel（及对应中文和斜杠命令）。"""

    def __init__(self, ui, timeout=120, reader=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or not 0 < timeout <= 3600):
            raise ValueError("审批等待时间必须是大于 0 且不超过 3600 的秒数")
        self.ui = ui
        self.timeout = float(timeout)
        self.reader = reader or read_line_with_timeout

    def _limit(self, request):
        limit = self.timeout
        remaining = getattr(request, "time_remaining", None)
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            limit = min(limit, float(remaining))
        return limit

    def __call__(self, request):
        self.ui.show_approval(request.summary)
        limit = self._limit(request)
        self.ui.info(f"等待上限 {limit:g} 秒。请输入 approve、reject 或 cancel。")
        if limit <= 0:
            self.ui.info("审批已过期，未执行。")
            return EXPIRED
        deadline = time.monotonic() + limit
        while True:
            check = getattr(request, "check", None)
            if callable(check):
                check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.ui.info("审批已过期，未执行。")
                return EXPIRED
            started = time.monotonic()
            try:
                line = self.reader(min(remaining, 0.5))
            except EOFError:
                self.ui.info("审批已取消，未执行。")
                return CANCELLED
            if line is None:
                elapsed = time.monotonic() - started
                slice_wait = min(remaining, 0.5)
                if elapsed < slice_wait:
                    time.sleep(slice_wait - elapsed)
                continue
            outcome = interpret_approval_line(line)
            if outcome == APPROVED:
                return APPROVED
            if outcome == REJECTED:
                self.ui.info("已拒绝，未执行。")
                return REJECTED
            if outcome == CANCELLED:
                self.ui.info("已取消，未执行。")
                return CANCELLED
            self.ui.info("这段文字不是批准或拒绝。请只输入 approve、reject 或 cancel。")
