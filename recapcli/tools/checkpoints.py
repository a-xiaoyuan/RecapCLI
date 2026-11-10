"""每轮写文件前的检查点。同一轮里每个文件只记下第一次改动前的原文。"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import re
import uuid

from ..base.files import state_directory, write_json
from ..base.paths import ToolError

_active = ContextVar("turn_checkpoint", default=None)
_ID = re.compile(r"[0-9a-f]{32}")
_sequence = 0


def _stamp():
    global _sequence
    _sequence += 1
    return datetime.now().astimezone().isoformat(timespec="microseconds"), _sequence


def _digest(content):
    return hashlib.sha256(content).hexdigest()


@contextmanager
def turn_checkpoint(workspace):
    """包住一轮任务。这一轮没有写文件时不落盘。"""
    created_at, sequence = _stamp()
    record = {"id": uuid.uuid4().hex, "created_at": created_at, "sequence": sequence,
              "files": {}, "saved": False}
    token = _active.set(record)
    try:
        yield record
    finally:
        try:
            _seal(Path(workspace).resolve(), record)
        finally:
            _active.reset(token)


def note_before_write(relative, before):
    """本轮第一次改到这个文件时记下原文。没有活动检查点时什么都不做。"""
    record = _active.get()
    if record is None or relative in record["files"]:
        return
    record["files"][relative] = {
        "before_base64": base64.b64encode(before).decode("ascii") if before is not None else None,
        "before_sha256": _digest(before) if before is not None else None,
    }


def _seal(workspace, record):
    if not record["files"]:
        return
    from .editing import _read_bytes, _target
    for relative, item in record["files"].items():
        target = _target(workspace, relative)
        current = _read_bytes(target) if target.is_file() else None
        item["after_sha256"] = _digest(current) if current is not None else None
    payload = {"schema_version": 1, "id": record["id"], "created_at": record["created_at"],
               "sequence": record["sequence"], "files": record["files"]}
    write_json(state_directory(workspace, "checkpoints") / f"{record['id']}.json", payload)
    record["saved"] = True


def _records(workspace):
    root = state_directory(workspace, "checkpoints")
    records = []
    for path in root.glob("*.json"):
        if path.is_symlink() or path.is_junction():
            raise ToolError("检查点文件不能是链接")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not _ID.fullmatch(str(data.get("id", ""))):
            raise ToolError(f"检查点格式不正确：{path.name}")
        if not isinstance(data.get("files"), dict) or not data["files"]:
            raise ToolError(f"检查点没有文件记录：{path.name}")
        records.append(data)
    return records


_NO_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _created(item):
    try:
        moment = datetime.fromisoformat(str(item.get("created_at") or ""))
    except ValueError:
        return _NO_TIME
    return moment if moment.tzinfo is not None else moment.astimezone()


def _order(item):
    """先按时间；sequence 是进程内计数，重启后归零，只用来区分同一时刻。"""
    return (_created(item), item.get("sequence") or 0)


def latest_checkpoint(workspace):
    pending = [item for item in _records(workspace) if not item.get("rewound_at")]
    if not pending:
        return None
    return max(pending, key=_order)


def find_checkpoint(workspace, checkpoint_id=None):
    if not checkpoint_id:
        return latest_checkpoint(workspace)
    matches = [item for item in _records(workspace) if item.get("id") == checkpoint_id and not item.get("rewound_at")]
    return matches[0] if matches else None


def rewind_checkpoint(workspace, checkpoint_id=None):
    """把检查点里的文件恢复到本轮写之前。当前内容对不上结束时的校验就整次拒绝。"""
    from .editing import _read_bytes, _replace_file, _target
    workspace = Path(workspace).resolve()
    records = _records(workspace)
    pending = [item for item in records if not item.get("rewound_at")]
    if checkpoint_id in (None, ""):
        if not pending:
            raise ToolError("没有可回退的检查点")
        data = max(pending, key=_order)
    else:
        if not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id):
            raise ToolError("检查点 ID 必须是 32 位十六进制")
        matches = [item for item in pending if item["id"] == checkpoint_id]
        if not matches:
            raise ToolError("没有可回退的检查点")
        data = matches[0]
    checked = []
    for relative, item in data["files"].items():
        target = _target(workspace, relative)
        current = _read_bytes(target) if target.is_file() else None
        current_hash = _digest(current) if current is not None else None
        if current_hash != item.get("after_sha256"):
            raise ToolError(f"{relative} 已有后续变化，未回退；请先核对文件")
        checked.append((relative, item, target, current))
    restored = []
    for relative, item, target, current in checked:
        encoded = item.get("before_base64")
        if encoded is None:
            if target.is_file():
                target.unlink()
            restored.append({"path": relative, "restored": True, "deleted": True})
            continue
        if not isinstance(encoded, str):
            raise ToolError("检查点原文格式不正确")
        original = base64.b64decode(encoded, validate=True)
        if _digest(original) != item.get("before_sha256"):
            raise ToolError("检查点原文校验失败")
        _replace_file(workspace, relative, current, original)
        restored.append({"path": relative, "restored": True, "deleted": False})
    data["rewound_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(state_directory(workspace, "checkpoints") / f"{data['id']}.json", data)
    return {"checkpoint_id": data["id"], "files": restored,
            "summary": f"已回退检查点 {data['id']}，恢复 {len(restored)} 个文件。"}
