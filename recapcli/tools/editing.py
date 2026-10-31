"""精确文本编辑、差异预览和按备份恢复；不执行任意补丁脚本。"""

import base64
from datetime import datetime
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from ..storage.files import data_root, state_directory, write_json
from .filesystem import ToolError, _relative, _resolve

MAX_EDIT_BYTES = 1024 * 1024
MAX_DIFF_BYTES = 8192
PROTECTED_DIR_NAMES = {".git", ".recapcli", ".revocli", ".archer"}


def _target(workspace, path):
    workspace = Path(workspace).resolve()
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path 必须是非空相对路径")
    relative = Path(path)
    if relative.drive or relative.root or ".." in relative.parts:
        raise ToolError("编辑路径必须相对工作区，不能包含 ..")
    candidate = workspace
    for part in relative.parts:
        candidate = candidate / part
        if part.rstrip(" .").casefold() in PROTECTED_DIR_NAMES:
            raise ToolError("禁止直接编辑 Git 元数据或 RecapCLI 运行数据")
        if os.name == "nt" and (":" in part or candidate.is_reserved()):
            raise ToolError("不能编辑 Windows 设备名或备用数据流路径")
        if candidate.is_symlink() or candidate.is_junction():
            raise ToolError("禁止通过符号链接或目录联接编辑文件")
    target = _resolve(workspace, path, must_exist=False)
    if any(part.casefold() in PROTECTED_DIR_NAMES for part in target.relative_to(workspace).parts):
        raise ToolError("禁止编辑内部数据路径")
    if target.exists() and not target.is_file():
        raise ToolError("编辑目标不是普通文件")
    return target


def _read_bytes(target):
    with target.open("rb") as file:
        content = file.read(MAX_EDIT_BYTES + 1)
    if len(content) > MAX_EDIT_BYTES:
        raise ToolError("编辑文件不能超过 1 MiB")
    return content


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _replace_file(workspace, path, before, after):
    target = _target(workspace, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.recapcli-", dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(after)
        target = _target(workspace, path)
        current = _read_bytes(target) if target.exists() else None
        if current != before:
            raise ToolError("文件在编辑期间发生变化，未覆盖；请重新读取")
        if target.exists():
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _diff(before, after, path):
    chunks = []
    size = 0
    for line in unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                             fromfile=f"a/{path}", tofile=f"b/{path}"):
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        data = line.encode("utf-8")
        if size + len(data) > MAX_DIFF_BYTES:
            chunks.append(data[:MAX_DIFF_BYTES - size].decode("utf-8", errors="ignore"))
            return "".join(chunks), True
        chunks.append(line)
        size += len(data)
    return "".join(chunks), False


def _plan_edit(workspace, path, old_text, new_text, replace_all=False, *, before=...):
    """计算一次精确替换，不写文件、不建备份。"""
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise ToolError("old_text 和 new_text 必须是字符串")
    if not isinstance(replace_all, bool):
        raise ToolError("replace_all 必须是布尔值")
    if max(len(old_text.encode("utf-8")), len(new_text.encode("utf-8"))) > MAX_EDIT_BYTES:
        raise ToolError("编辑文本不能超过 1 MiB")
    target = _target(workspace, path)
    if before is ...:
        before = _read_bytes(target) if target.exists() else None
    text = before.decode("utf-8-sig") if before is not None else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    old = old_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    new = new_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    if before is None or not text:
        if old:
            raise ToolError("文件不存在或为空；创建内容时 old_text 必须为空")
        updated, count = new, 1
    else:
        if not old:
            raise ToolError("现有非空文件的 old_text 不能为空，请先读取并提供精确原文")
        count = text.count(old)
        if not count:
            raise ToolError("未找到 old_text，文件未修改；请重新读取并使用精确原文")
        if count > 1 and not replace_all:
            raise ToolError(f"old_text 匹配 {count} 处，请提供更完整的原文或显式 replace_all")
        updated = text.replace(old, new, -1 if replace_all else 1)
    encoding = "utf-8-sig" if before is not None and before.startswith(b"\xef\xbb\xbf") else "utf-8"
    after = updated.encode(encoding)
    if len(after) > MAX_EDIT_BYTES:
        raise ToolError("编辑后的文件不能超过 1 MiB")
    relative = _relative(workspace, target)
    diff, truncated = ("", False)
    if after != before:
        diff, truncated = _diff(text, updated, relative)
    return {"path": relative, "before": before, "after": after, "changed": after != before,
            "created": before is None, "replacements": count, "diff": diff, "diff_truncated": truncated,
            "fingerprint": _hash(before) if before is not None else "absent"}


def edit_file(workspace, path, old_text, new_text, replace_all=False, *, on_record=None):
    """现有文件默认只替换唯一匹配；创建文件时 old_text 必须为空。"""
    return _commit_edit(workspace, _plan_edit(workspace, path, old_text, new_text, replace_all), on_record)


def _commit_edit(workspace, planned, on_record=None, group_id=None):
    relative = planned["path"]
    if not planned["changed"]:
        return {"path": relative, "changed": False, "summary": f"{relative} 内容未变化"}
    before, after = planned["before"], planned["after"]
    backup_id = uuid.uuid4().hex
    backup = {"schema_version": 1, "id": backup_id, "path": relative, "group_id": group_id or backup_id,
              "created_at": datetime.now().astimezone().isoformat(),
              "before_base64": base64.b64encode(before).decode("ascii") if before is not None else None,
              "before_sha256": _hash(before) if before is not None else None,
              "after_sha256": _hash(after)}
    write_json(state_directory(workspace, "backups") / f"{backup_id}.json", backup)
    if on_record is not None:
        on_record("backup", backup_id)
    _replace_file(workspace, relative, before, after)
    return {"path": relative, "changed": True, "created": planned["created"], "replacements": planned["replacements"],
            "backup_id": backup_id, "diff": planned["diff"], "diff_truncated": planned["diff_truncated"],
            "summary": f"已修改 {relative}，备份 ID：{backup_id}"}


def _plan_edits(workspace, edits):
    if not isinstance(edits, list) or not 1 <= len(edits) <= 20:
        raise ToolError("edits 必须包含 1 到 20 项精确替换")
    plans = {}
    for edit in edits:
        if (not isinstance(edit, dict) or not {"path", "old_text", "new_text"} <= edit.keys()
                or set(edit) - {"path", "old_text", "new_text", "replace_all"}):
            raise ToolError("每项编辑须包含 path、old_text、new_text，可选 replace_all")
        target = _target(workspace, edit["path"])
        previous = plans.get(target)
        planned = _plan_edit(workspace, **edit, before=previous["after"] if previous else ...)
        if previous:
            original = previous["before"]
            planned.update(before=original, fingerprint=previous["fingerprint"], created=previous["created"],
                           replacements=previous["replacements"] + planned["replacements"],
                           changed=original != planned["after"])
            planned["diff"], planned["diff_truncated"] = _diff(
                original.decode("utf-8-sig") if original is not None else "",
                planned["after"].decode("utf-8-sig"), planned["path"])
        plans[target] = planned
    return list(plans.values())


def edit_files(workspace, edits, *, on_record=None):
    """一次审批一组编辑；先验证全部替换，再逐文件写入并保存各自收据。"""
    plans = _plan_edits(workspace, edits)
    group_id, files, diffs = uuid.uuid4().hex, [], []
    failure = None
    for planned in plans:
        try:
            receipt = _commit_edit(workspace, planned, on_record, group_id)
            diffs.append(receipt.pop("diff", ""))
            files.append(receipt)
        except (OSError, ValueError) as error:
            failure = f"批量编辑在 {planned['path']} 停止：{error}；已完成的文件与备份见 files，后续未执行。"
            break
    backups = [item["backup_id"] for item in files if item.get("changed")]
    diff = "\n".join(diffs).encode("utf-8")
    return {**({"ok": False, "error": failure} if failure else {}),
            "files": files, "group_id": group_id, "backup_ids": backups,
            "diff": diff[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore"),
            "diff_truncated": len(diff) > MAX_DIFF_BYTES or any(item.get("diff_truncated") for item in files),
            "summary": f"已编辑 {len(backups)} 个文件，操作组：{group_id}；undo_edit 可撤销本组。"}


def _undo_records(workspace, group_id=None):
    if group_id is not None and (not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-f]{32}", group_id)):
        raise ToolError("group_id 必须是完整的 32 位操作组 ID")
    records = []
    for path in (data_root(workspace) / "backups").glob("*.json"):
        if path.is_symlink() or path.is_junction() or path.stat().st_size > MAX_EDIT_BYTES * 2:
            raise ToolError("备份文件类型或大小不正确")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not {"id", "path", "after_sha256", "created_at"} <= data.keys():
            raise ToolError(f"备份记录格式不正确：{path.name}")
        if not data.get("restored_at") and (group_id is None or data.get("group_id", data["id"]) == group_id):
            records.append(data)
    if not records:
        raise ToolError("没有可撤销的编辑")
    latest = max(records, key=lambda item: item["created_at"])
    group = latest.get("group_id", latest["id"])
    return [item for item in reversed(records) if item.get("group_id", item["id"]) == group]


def undo_edit(workspace, group_id=None):
    records = _undo_records(workspace, group_id)
    for item in records:
        target = _target(workspace, item["path"])
        if not target.is_file() or _hash(_read_bytes(target)) != item["after_sha256"]:
            raise ToolError(f"{item['path']} 已有后续变化，未撤销；请先核对文件")
    files, failure = [], None
    for item in records:
        try:
            files.append(restore_file(workspace, item["id"]))
        except (OSError, ValueError) as error:
            failure = f"恢复 {item['path']} 时停止：{error}；已完成项见 files。"
            break
    group = records[0].get("group_id", records[0]["id"])
    diff = "\n".join(item.pop("diff") for item in files).encode("utf-8")
    return {**({"ok": False, "error": failure} if failure else {}),
            "files": files, "group_id": group, "diff": diff[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore"),
            "diff_truncated": len(diff) > MAX_DIFF_BYTES or any(item["diff_truncated"] for item in files),
            "summary": f"操作组 {group} 已恢复 {len(files)} 个文件。"}


def restore_file(workspace, backup_id):
    """仅在目标仍等于该次编辑结果时恢复，避免覆盖后续修改。"""
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise ToolError("backup_id 格式不正确")
    record = state_directory(workspace, "backups") / f"{backup_id}.json"
    if record.is_symlink() or record.stat().st_size > MAX_EDIT_BYTES * 2:
        raise ToolError("备份文件类型或大小不正确")
    data = json.loads(record.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("id") != backup_id:
        raise ToolError("备份格式不正确")
    if (not {"path", "before_base64", "before_sha256", "after_sha256"} <= data.keys()
            or not isinstance(data["after_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["after_sha256"])
            or (data["before_base64"] is None and data["before_sha256"] is not None)):
        raise ToolError("备份字段不完整或校验值不正确")
    target = _target(workspace, data.get("path"))
    current = _read_bytes(target)
    if _hash(current) != data.get("after_sha256"):
        raise ToolError("文件已有后续修改，未覆盖；请先核对当前内容")
    encoded = data.get("before_base64")
    original = b""
    if encoded is None:
        target.unlink()
    else:
        if not isinstance(encoded, str):
            raise ToolError("备份原文格式不正确")
        original = base64.b64decode(encoded, validate=True)
        if len(original) > MAX_EDIT_BYTES or _hash(original) != data.get("before_sha256"):
            raise ToolError("备份原文校验失败")
        _replace_file(workspace, data["path"], current, original)
    data["restored_at"] = datetime.now().astimezone().isoformat()
    write_json(record, data)
    diff, truncated = _diff(current.decode("utf-8-sig"), original.decode("utf-8-sig"), data["path"])
    return {"path": data["path"], "restored": True, "backup_id": backup_id,
            "diff": diff, "diff_truncated": truncated,
            "summary": f"已恢复 {data['path']}，备份 ID：{backup_id}"}
