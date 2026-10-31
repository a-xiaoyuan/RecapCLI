"""读取当前工作区根目录的开发约定。"""

import hashlib

from ..tools.filesystem import ToolError, _resolve


MAX_RULE_BYTES = 8 * 1024


def load_project_rules(workspace, max_bytes=MAX_RULE_BYTES):
    source = {"source": "AGENTS.md"}
    try:
        target = _resolve(workspace, "AGENTS.md", must_exist=False)
        with target.open("rb") as file:
            content = file.read(max_bytes + 1)
        if len(content) > max_bytes:
            return {**source, "status": "skipped", "reason": f"超过 {max_bytes} 字节的规则预算，未加载"}
        text = content.decode("utf-8-sig").strip()
    except FileNotFoundError:
        return {**source, "status": "missing"}
    except UnicodeError:
        return {**source, "status": "skipped", "reason": "文件不是 UTF-8 文本"}
    except (OSError, ToolError) as error:
        return {**source, "status": "skipped", "reason": str(error)}
    return {**source, "status": "loaded" if text else "empty", "text": text,
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def project_rules_description(rules):
    if rules is None:
        return "本轮未记录项目规则；普通提问时读取根目录 AGENTS.md"
    status = rules["status"]
    if status == "loaded":
        return f"{rules['source']} · 已加载 {rules['bytes']} 字节"
    if status == "missing":
        return "根目录未找到 AGENTS.md"
    if status == "empty":
        return "AGENTS.md 是空文件"
    return "AGENTS.md 未加载 · " + rules["reason"]
