"""一轮任务的结果类型，以及工具结果在上下文和收尾中的处理。"""

from dataclasses import dataclass
import json
import re

from .budget import SKIPPED_RESULT

KEEP_RECENT_RESULTS = 4    # 最近几条工具结果始终保留原文
CLEARED_NOTE = "较早的工具结果已移出上下文以节省空间，完整内容保存在任务记录中；如仍需要请重新调用。"
CLEARED_PREFIX = '{"cleared":true'
CONTEXT_OVERFLOW_MARKERS = ("context_length", "context length", "context window", "maximum context",
                            "prompt is too long", "input is too long", "too many tokens", "上下文")
EDIT_TOOLS = frozenset({"edit_file", "edit_files", "restore_file", "undo_edit"})


@dataclass(frozen=True)
class TurnResult:
    answer: str
    messages: list[dict]
    status: str = "returned"
    stop_reason: str = "模型已返回回答；任务验收未验证"
    summary: dict | None = None


class TraceError(RuntimeError):
    """事实记录失败必须停止，不能作为可回退的摘要错误吞掉。"""


def fact_result(result):
    """任务事实保留原文；模型专用的行号前缀不写入事实。"""
    if result.get("content_format") != "line_numbered":
        return result
    cleaned = dict(result)
    cleaned["content"] = re.sub(r"(?m)^\d+ \| ", "", cleaned.get("content", ""))
    cleaned.pop("content_format", None)
    return cleaned


def context_overflow(error):
    if getattr(error, "status_code", None) not in {400, 413}:
        return False
    text = f"{getattr(error, 'code', '')} {error.message}".casefold()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def has_tool_results(turn):
    return any(message.get("role") == "tool" for message in turn)


def call_index(turn):
    """tool_call_id → (工具名, 参数摘要)，用于给移出的结果留下可读的占位。"""
    calls = {}
    for message in turn:
        for call in message.get("tool_calls") or ():
            function = call.get("function", {})
            calls[call.get("id")] = (function.get("name", ""), str(function.get("arguments", ""))[:160])
    return calls


def clear_old_tool_results(turn, fits, *, keep=KEEP_RECENT_RESULTS, protect=()):
    """从最早的工具结果开始换成占位，直到 fits() 为真；返回移出的条数。

    只改模型看到的正文，tool_call_id 配对不变；完整结果早已写入任务记录。
    """
    if fits():
        return 0
    calls = call_index(turn)
    results = [message for message in turn if message.get("role") == "tool"
               and not str(message.get("content", "")).startswith(CLEARED_PREFIX)
               and message.get("tool_call_id") not in protect]
    candidates = results[:max(0, len(results) - keep)]
    cleared = 0
    for message in candidates:
        name, arguments = calls.get(message["tool_call_id"], ("", ""))
        message["content"] = json.dumps({"cleared": True, "tool": name, "arguments": arguments, "note": CLEARED_NOTE},
                                        ensure_ascii=False, separators=(",", ":"))
        cleared += 1
        if fits():
            break
    return cleared


def oversized_result(result, tokens):
    """单条结果放不进上下文时只保留收据字段，让模型缩小范围重试。"""
    keep = ("ok", "error", "summary", "path", "backup_id", "group_id", "record_id", "returncode", "success", "status")
    trimmed = {key: result[key] for key in keep if key in result}
    trimmed["truncated"] = True
    trimmed["note"] = f"结果约 {tokens} tokens，超出剩余上下文，未放入；请缩小范围（指定行号、路径或更具体的查询）后重试。"
    return trimmed


def close_turn(turn):
    """中途停止时给没有结果的工具调用补上 not_executed，保持调用与结果配对。"""
    answered = {message.get("tool_call_id") for message in turn if message.get("role") == "tool"}
    for message in list(turn):
        for call in message.get("tool_calls") or ():
            if call.get("id") not in answered:
                turn.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(SKIPPED_RESULT, ensure_ascii=False)})


def edited_paths(name, result):
    """以工具实际结果为准：被拒绝的编辑不算，批量失败时已写入的文件要算。"""
    if name not in EDIT_TOOLS:
        return []
    return [item["path"] for item in result.get("files") or [result]
            if item.get("path") and (item.get("changed") or item.get("restored"))]


def fallback_answer(reason, turn, todos, edited):
    """连最后一次汇总请求都发不出去时，用本轮事实拼一个诚实的结束说明。"""
    lines = [f"本轮在给出完整答复前停止：{reason}。", f"已调用 {len(call_index(turn))} 次工具。"]
    if edited:
        more = f"等 {len(edited)} 个文件" if len(edited) > 10 else ""
        lines.append("编辑过的文件：" + "、".join(edited[:10]) + more + "。")
    if todos.items:
        lines.append("待办清单：\n" + todos.render())
    lines.append("用 /diff 查看改动、/task 查看完整记录；发送“继续”可以接着完成。")
    return "\n".join(lines)
