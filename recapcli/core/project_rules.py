"""读取工作区根目录与子目录的开发约定。"""

import hashlib
import os
from pathlib import Path
import subprocess

from ..base.paths import SKIP_DIRS
from ..tools.filesystem import ToolError, _resolve


MAX_RULE_BYTES = 8 * 1024
MAX_RULE_FILES = 20
GIT_TIMEOUT_SECONDS = 5


def _git_rule_files(workspace):
    """Git 项目用 ls-files 查找，不进入被忽略的大目录；用不了时返回 None。

    只认工作区根目录的仓库：工作区在别的仓库的忽略目录里时，ls-files 会什么都不返回。
    """
    if not (workspace / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "--cached", "--others", "--exclude-standard", "-z",
             "--", ":(glob)**/AGENTS.md"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return [Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name]


def _walked_rule_files(workspace):
    """先剪掉跳过的目录再往下走，不能遍历完再过滤。"""
    found = []
    for directory, folders, files in os.walk(workspace):
        folders[:] = [name for name in folders if name not in SKIP_DIRS]
        if "AGENTS.md" in files:
            found.append((Path(directory) / "AGENTS.md").relative_to(workspace))
    return found


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


def _layer_view(layer):
    return {key: value for key, value in layer.items() if key != "text"}


def load_rule_layers(workspace, max_bytes=MAX_RULE_BYTES, max_files=MAX_RULE_FILES):
    """读取根目录和子目录的 AGENTS.md，按根目录优先返回。"""
    workspace = Path(workspace).resolve()
    layers = []
    root = _read_rule(workspace, Path("AGENTS.md"), max_bytes)
    if root["status"] != "missing":
        layers.append(root)
    total = root.get("bytes", 0)
    files = 0
    found = _git_rule_files(workspace)
    if found is None:
        found = _walked_rule_files(workspace)
    candidates = [relative for relative in found if relative.as_posix() != "AGENTS.md"
                  and not any(part in SKIP_DIRS for part in relative.parts)]
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


def summarize_rule_layers(layers):
    """把分层结果收成一条规则摘要。

    status 描述实际进入上下文的结果，而不是单独的根目录文件：
    loaded 表示至少一份正文已加载；skipped 表示没有正文且至少一份被跳过；
    empty 表示找到的文件都为空；missing 表示没有 AGENTS.md。
    loaded 时 bytes 是已加载文件的字节合计。根目录文件本身被加载时，sha256 仍是该根文件指纹。
    """
    public = [_layer_view(layer) for layer in layers]
    loaded = [layer for layer in public if layer.get("status") == "loaded"]
    skipped = [layer for layer in public if layer.get("status") == "skipped"]
    empty = [layer for layer in public if layer.get("status") == "empty"]
    root = next((layer for layer in public if layer.get("source") == "AGENTS.md"), None)

    if loaded:
        primary = root if root and root.get("status") == "loaded" else loaded[0]
        record = {
            "source": primary.get("source", "AGENTS.md"),
            "scope": primary.get("scope", "."),
            "status": "loaded",
            "bytes": sum(layer.get("bytes", 0) for layer in loaded),
            "layers": public,
        }
        fingerprint = root.get("sha256") if root and root.get("status") == "loaded" else None
        if not fingerprint and len(loaded) == 1:
            fingerprint = loaded[0].get("sha256")
        if fingerprint:
            record["sha256"] = fingerprint
        return record

    if skipped:
        chosen = root if root and root.get("status") == "skipped" else skipped[0]
        return {
            "source": chosen.get("source", "AGENTS.md"),
            "scope": chosen.get("scope", "."),
            "status": "skipped",
            "reason": chosen.get("reason", "未加载"),
            "layers": public,
        }

    if empty:
        chosen = root if root and root.get("status") == "empty" else empty[0]
        record = {
            "source": chosen.get("source", "AGENTS.md"),
            "scope": chosen.get("scope", "."),
            "status": "empty",
            "layers": public,
        }
        if "bytes" in chosen:
            record["bytes"] = chosen["bytes"]
        if chosen.get("sha256"):
            record["sha256"] = chosen["sha256"]
        return record

    return {"source": "AGENTS.md", "status": "missing", "layers": public}


def _skipped_text(layer, rules):
    source = layer.get("source", rules.get("source", "AGENTS.md"))
    reason = layer.get("reason") or rules.get("reason") or ""
    if reason:
        return f"{source} 未加载 · {reason}"
    return f"{source} 未加载"


def _single_rule_description(rules):
    status = rules.get("status")
    source = rules.get("source", "AGENTS.md")
    if status == "loaded":
        return f"{source} · 已加载 {rules.get('bytes', 0)} 字节"
    if status == "missing":
        return "根目录未找到 AGENTS.md"
    if status == "empty":
        return "AGENTS.md 是空文件" if source == "AGENTS.md" else f"{source} 是空文件"
    reason = rules.get("reason") or ""
    return f"{source} 未加载 · {reason}" if reason else f"{source} 未加载"


def _loaded_rules_description(loaded, layers):
    total = sum(int(layer.get("bytes") or 0) for layer in loaded)
    primary = next((layer.get("source") for layer in loaded if layer.get("source") == "AGENTS.md"), None)
    primary = primary or loaded[0].get("source", "AGENTS.md")
    shown = loaded[0].get("bytes", total) if len(loaded) == 1 else total
    text = f"{primary} · 已加载 {shown} 字节"
    if len(loaded) > 1:
        text += f" · 含 {len(loaded)} 个规则文件"
    notes = []
    empty = [layer for layer in layers if layer.get("status") == "empty"]
    skipped = [layer for layer in layers if layer.get("status") == "skipped"]
    if len(empty) == 1 and empty[0].get("source") == "AGENTS.md":
        notes.append("根目录 AGENTS.md 为空")
    elif empty:
        notes.append(f"{len(empty)} 个空文件")
    if skipped:
        notes.append(f"{len(skipped)} 个未加载")
    if notes:
        text += " · " + " · ".join(notes)
    return text


def _unloaded_rules_description(rules, layers):
    skipped = [layer for layer in layers if layer.get("status") == "skipped"]
    empty = [layer for layer in layers if layer.get("status") == "empty"]
    if skipped and len(skipped) == 1 and not empty:
        return _skipped_text(skipped[0], rules)
    if skipped:
        reason = skipped[0].get("reason") or rules.get("reason") or ""
        text = f"{len(skipped)} 个规则文件未加载"
        if empty:
            text += f" · {len(empty)} 个空文件"
        if reason:
            text += f" · {reason}"
        return text
    if len(empty) == 1 and empty[0].get("source") == "AGENTS.md":
        return "AGENTS.md 是空文件"
    if empty:
        names = "、".join(layer.get("source", "AGENTS.md") for layer in empty)
        return f"规则文件为空：{names}"
    if rules.get("status") == "missing":
        return "根目录未找到 AGENTS.md"
    return _single_rule_description(rules)


def project_rules_description(rules):
    if rules is None:
        return "本轮未记录项目规则；普通提问时读取根目录 AGENTS.md"
    layers = [layer for layer in (rules.get("layers") or []) if isinstance(layer, dict)]
    if not layers:
        return _single_rule_description(rules)
    loaded = [layer for layer in layers if layer.get("status") == "loaded"]
    if loaded:
        return _loaded_rules_description(loaded, layers)
    return _unloaded_rules_description(rules, layers)
