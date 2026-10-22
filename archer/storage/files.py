"""本地 JSON 的原子写入，以及操作记录目录。"""

import json
import os
from pathlib import Path
import uuid


def write_json(path, data):
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def state_directory(workspace, name):
    """操作记录只写入真实工作区内，不跟随数据目录链接。"""
    workspace = Path(workspace).resolve()
    root = workspace / ".archer"
    directory = root / name
    for candidate in (root, directory):
        if (candidate.is_symlink() or candidate.is_junction()
                or not candidate.resolve().is_relative_to(workspace)):
            raise ValueError("操作记录目录不能是链接或位于工作区之外")
        candidate.mkdir(exist_ok=True)
    ignore = root / ".gitignore"
    try:
        with ignore.open("x", encoding="utf-8") as file:
            file.write("*\n")
    except FileExistsError:
        pass
    return directory
