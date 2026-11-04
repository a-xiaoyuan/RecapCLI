"""上下文计量与历史选择：按 token 估算，并用服务端返回的真实用量校准。"""

from copy import deepcopy
import json

from ..config import DEFAULT_CONTEXT_TOKENS  # noqa: F401  (re-exported)

MAX_HISTORY_MESSAGES = 80


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


class TokenMeter:
    """估算值 × 校准系数。系数来自上一次请求的 prompt_tokens，与服务端 tokenizer 对齐。"""

    MIN_RATIO, MAX_RATIO = 0.8, 2.0

    def __init__(self):
        self.ratio = 1.0

    def count(self, value):
        return int(json_tokens(value) * self.ratio)

    def raw_limit(self, tokens):
        """把校准后的 token 数换回未校准估算，用于 select_history。"""
        return max(0, int(tokens / self.ratio))

    def observe(self, estimated, usage):
        actual = (usage or {}).get("prompt_tokens") if isinstance(usage, dict) else None
        # 很短的请求被模板开销主导，不用来校准。
        if isinstance(actual, int) and not isinstance(actual, bool) and estimated >= 2000:
            self.ratio = min(self.MAX_RATIO, max(self.MIN_RATIO, actual / estimated))


def select_history(messages, max_messages=MAX_HISTORY_MESSAGES, max_tokens=None):
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
    count, tokens = 0, 0
    for turn in reversed(turns):
        last = turn[-1]
        if last.get("role") != "assistant" or last.get("tool_calls"):
            break
        cost = json_tokens(turn) + 1
        if count + len(turn) > max_messages or (max_tokens is not None and tokens + cost > max_tokens):
            break
        selected.append(turn)
        count += len(turn)
        tokens += cost
    return deepcopy([message for turn in reversed(selected) for message in turn])
