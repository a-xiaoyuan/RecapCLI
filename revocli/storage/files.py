"""本地 JSON 的原子写入，以及操作记录目录。"""

import json
import os
from pathlib import Path
import uuid


DATA_DIRECTORY = ".revocli"
LEGACY_DATA_DIRECTORY = ".archer"


def data_root(workspace):
    """新工作区使用新名称；旧数据原位兼容，拒绝静默拆成两份。"""
    workspace = Path(workspace).resolve()
    candidates = [workspace / DATA_DIRECTORY, workspace / LEGACY_DATA_DIRECTORY]
    for candidate in candidates:
        if (candidate.is_symlink() or candidate.is_junction()
                or not candidate.resolve().is_relative_to(workspace)):
            raise ValueError("运行数据目录不能是链接或位于工作区之外")
    existing = [candidate for candidate in candidates if candidate.exists()]
    if len(existing) > 1:
        raise ValueError("同时存在 .revocli 与旧版 .archer 数据目录，请先核对并合并或移走其中一份；未自动覆盖")
    return existing[0] if existing else candidates[0]


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
    root = data_root(workspace)
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
