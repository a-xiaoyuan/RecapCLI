"""从完整存档中选择模型上下文，不修改存档。"""

from copy import deepcopy
import json

MAX_HISTORY_MESSAGES = 80
MAX_CONTEXT_BYTES = 256 * 1024


def json_size(value):
    """UTF-8 JSON 大小预算；不是模型 token 数。"""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def estimate_tokens(value):
    """保守估算 token：ASCII 约 4 字符/token，非 ASCII 约 1 字符/token。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_count = sum(ord(char) < 128 for char in text)
    return max(1, (ascii_count + 3) // 4 + len(text) - ascii_count)


def json_tokens(value):
    return estimate_tokens(value)


def select_history(messages, max_messages=MAX_HISTORY_MESSAGES, max_bytes=MAX_CONTEXT_BYTES, max_tokens=None):
    """选择最近的完整轮次；一个轮次放不下时停止，不留下孤立工具结果。"""
    turns = []
    current = []
    for message in messages:
        if message.get("role") == "user":
            if current:
                turns.append(current)
            current = [message]
        elif current:
            current.append(message)
    if current:
        turns.append(current)

    selected = []
    count, size, tokens = 0, 2, 0
    for turn in reversed(turns):
        last = turn[-1]
        if last.get("role") != "assistant" or last.get("tool_calls"):
            break
        cost = json_size(turn) + 1
        token_cost = json_tokens(turn) + 1
        if (count + len(turn) > max_messages or size + cost > max_bytes
                or (max_tokens is not None and tokens + token_cost > max_tokens)):
            break
        selected.append(turn)
        count += len(turn)
        size += cost
        tokens += token_cost
    return deepcopy([message for turn in reversed(selected) for message in turn])
