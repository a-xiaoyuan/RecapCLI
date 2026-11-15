"""从结束的任务里提炼一条经验候选：先按记录整理事实，再请模型归纳，结果默认不生效。"""

import json
import re

from ..llm.client import request_completion

MAX_DIGEST_CHARS = 6000
MAX_NOTE_CHARS = 1500
FIELD_LIMITS = {"trigger": 300, "lesson": 500, "evidence": 600}
EDIT_TOOL_NAMES = {"edit_file", "edit_files"}
LEARNING_TIMEOUT = 60

SYSTEM_PROMPT = (
    "你在复盘一次编程 Agent 任务，要从记录的事实里提炼一条下次可以直接照做的经验。"
    "只依据给出的事实，不要猜测或补充记录里没有的内容；事实不足以说明原因时，把 lesson 写成空字符串。"
    "只返回一个 JSON 对象，不要其他文字：{\"trigger\": 什么情况下适用（写清任务类型、模块或现象）, "
    "\"lesson\": 下次应该怎么做（一两句可执行的做法）, \"evidence\": 支撑这条经验的事实（引用记录里的文件名、命令或原话）}。"
)


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def failure_digest(task, note=None):
    """把任务记录整理成给模型看的事实摘要；只取记录里已有的内容，并限制长度。"""
    lines = [f"任务：{_short(task.get('prompt'), 600)}",
             f"执行终态：{task.get('status')}；停止原因：{_short(task.get('stop_reason'), 300)}；"
             f"人工验收：{task.get('verification_status')}"]
    if note:
        lines.append(f"用户补充的依据：{_short(note, MAX_NOTE_CHARS)}")
    calls, edited, problems = {}, [], []
    for event in task.get("events") or ():
        kind, data = event.get("kind"), event.get("data") or {}
        if kind == "tool_start":
            calls[data.get("call_id")] = data.get("arguments") or ""
        elif kind == "tool_result":
            name, result = data.get("name"), data.get("result") or {}
            if name in EDIT_TOOL_NAMES and result.get("ok"):
                path = result.get("path") or _short(calls.get(data.get("call_id")), 120)
                if path not in edited:
                    edited.append(path)
            elif not result.get("ok", True):
                problems.append(f"{name} 失败：{_short(result.get('error'), 200)}")
            elif name == "run_command" and result.get("returncode") not in (0, None):
                problems.append(f"命令退出码 {result.get('returncode')}：{_short(calls.get(data.get('call_id')), 160)}"
                                f" → {_short((result.get('output') or '')[-400:], 300)}")
        elif kind == "auto_verify" and not data.get("success"):
            problems.append(f"自动验证未通过：{_short(data.get('command'), 160)}")
        elif kind == "acceptance_check" and data.get("status") == "failed":
            problems.append(f"需求验收项 {data.get('id')} 未通过：{_short(data.get('reason'), 200)}")
        elif kind == "budget_stop":
            problems.append(f"收尾原因：{_short(data.get('reason'), 200)}")
    lines.append("通过编辑工具改过的文件：" + ("、".join(edited) if edited else "无"))
    if problems:
        lines.append("记录到的问题（最近的在后）：")
        lines.extend(f"- {item}" for item in problems[-12:])
    lines.append(f"模型最后的回答：{_short(task.get('answer'), 800)}")
    digest = "\n".join(lines)
    return digest if len(digest) <= MAX_DIGEST_CHARS else digest[:MAX_DIGEST_CHARS - 1] + "…"


def learning_messages(digest):
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": digest}]


def parse_lesson(content):
    """解析模型返回的 JSON；格式不对或没有可执行做法时报错，不生成候选。"""
    text = (content or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match is None:
        raise ValueError("模型没有返回 JSON，未生成经验候选")
    try:
        payload = json.loads(match.group(0))
    except ValueError as error:
        raise ValueError("模型返回的 JSON 无法解析，未生成经验候选") from error
    if not isinstance(payload, dict) or not all(isinstance(payload.get(key), str) for key in FIELD_LIMITS):
        raise ValueError("模型返回缺少 trigger、lesson 或 evidence，未生成经验候选")
    lesson = {key: _short(payload[key], limit) for key, limit in FIELD_LIMITS.items()}
    if not lesson["lesson"]:
        raise ValueError("模型认为事实不足以提炼经验，未生成候选；可以补充依据后再试")
    if not lesson["trigger"]:
        raise ValueError("模型没有说明经验的适用条件，未生成候选")
    return lesson


def lesson_text(lesson):
    text = f"适用：{lesson['trigger']}。做法：{lesson['lesson']}"
    return text + (f"。依据：{lesson['evidence']}" if lesson["evidence"] else "")


def distill_experience(client, model, task, note=None, *, timeout=LEARNING_TIMEOUT):
    """请求一次模型，不提供工具；返回 trigger、lesson、evidence 三个字段。"""
    if task.get("status") == "running":
        raise ValueError("任务仍在运行，结束后再提炼经验")
    response = request_completion(client, model=model, messages=learning_messages(failure_digest(task, note)),
                                  tools=None, tools_allowed=False, timeout=timeout)
    message = response.choices[0].message if getattr(response, "choices", None) else None
    return parse_lesson(message.content if message is not None else None)
