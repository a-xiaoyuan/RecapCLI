"""上下文字节和 token 的粗略估算，模型层和 Agent 层共用。"""

import json


def json_size(value):
    """UTF-8 JSON 字节数；只用于摘要、报告等次级截断，不作为上下文预算。"""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def estimate_tokens(value):
    """粗估 token：ASCII 约 4 字符/token，非 ASCII 约 1 字符/token。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_count = sum(ord(char) < 128 for char in text)
    return max(1, (ascii_count + 3) // 4 + len(text) - ascii_count)


def json_tokens(value):
    return estimate_tokens(value)
