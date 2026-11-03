"""读取工作区根目录与子目录的开发约定。"""

import hashlib
from pathlib import Path

from ..tools.filesystem import ToolError, _resolve


MAX_RULE_BYTES = 8 * 1024
MAX_RULE_FILES = 20
SKIP_DIRECTORIES = {".git", ".recapcli", ".revocli", ".archer", ".venv", "node_modules", "__pycache__"}


def _read_rule(workspace, relative, max_bytes):
    source = relative.as_posix()
    scope = relative.parent.as_posix() if relative.parent.as_posix() != "." else "."
    record = {"source": source, "scope": scope}
    try:
        target = _resolve(workspace, source, must_exist=False)
        with target.open("rb") as file:
            content = file.read(max_bytes + 1)
        if len(content) > max_bytes:
            return {**record, "status": "skipped", "reason": f"超过 {max_bytes} 字节的规则预算，未加载"}
        text = content.decode("utf-8-sig").strip()
    except FileNotFoundError:
        return {**record, "status": "missing"}
    except UnicodeError:
        return {**record, "status": "skipped", "reason": "文件不是 UTF-8 文本"}
    except (OSError, ToolError) as error:
        return {**record, "status": "skipped", "reason": str(error)}
    return {**record, "status": "loaded" if text else "empty", "text": text,
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def load_project_rules(workspace, max_bytes=MAX_RULE_BYTES):
    """只读取根目录 AGENTS.md；保留既有单文件接口。"""
    return _read_rule(workspace, Path("AGENTS.md"), max_bytes)


def load_rule_layers(workspace, max_bytes=MAX_RULE_BYTES, max_files=MAX_RULE_FILES):
    """读取根目录和子目录的 AGENTS.md，按根目录优先返回。"""
    workspace = Path(workspace).resolve()
    layers = []
    root = _read_rule(workspace, Path("AGENTS.md"), max_bytes)
    if root["status"] != "missing":
        layers.append(root)
    total = root.get("bytes", 0)
    files = 0
    candidates = []
    try:
        for path in workspace.rglob("AGENTS.md"):
            relative = path.relative_to(workspace)
            if relative.as_posix() == "AGENTS.md":
                continue
            if any(part in SKIP_DIRECTORIES for part in relative.parts):
                continue
            candidates.append(relative)
    except OSError:
        return layers
    for relative in sorted(candidates, key=lambda item: (len(item.parts), item.as_posix())):
        if files >= max_files:
            break
        remaining = max(0, max_bytes - total)
        record = _read_rule(workspace, relative, remaining)
        files += 1
        if record["status"] == "skipped":
            layers.append(record)
            continue
        if record["status"] in {"loaded", "empty"}:
            layers.append(record)
            total += record.get("bytes", 0)
    return layers


def project_rules_description(rules):
    if rules is None:
        return "本轮未记录项目规则；普通提问时读取根目录 AGENTS.md"
    status = rules["status"]
    layers = rules.get("layers", [])
    if status == "loaded":
        scope = "" if len(layers) <= 1 else f" · 含 {len(layers)} 个规则文件"
        return f"{rules['source']} · 已加载 {rules['bytes']} 字节{scope}"
    if status == "missing":
        return "根目录未找到 AGENTS.md"
    if status == "empty":
        return "AGENTS.md 是空文件"
    return "AGENTS.md 未加载 · " + rules["reason"]
