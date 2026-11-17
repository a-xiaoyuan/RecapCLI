"""工具写入、验证结果和终态的同一份记录。

是否结束、要不要把模型拉回来继续修，仍由结束检查决定。
一旦决定结束，停止原因、未落盘声明和这份记录只在这里生成，避免各处各拼一次。
"""

import re
from dataclasses import dataclass

from .acceptance import acceptance_label

CHANGE_CLAIM = re.compile(r"已(?:经)?(?:完成|修改|实现|修复|更新|添加|增加|改动|新增)|改动如下|修改如下")
UNBACKED_CLAIM_NOTE = "> [核对] 本轮没有通过编辑工具修改任何文件，上面描述的改动并未写入工作区。"
UNBACKED_REASON = "回答声称已修改，但本轮未通过编辑工具写入文件"
MANUAL_PENDING = "人工验收未登记"
MANUAL_ABSENT = "任务验收未验证"


@dataclass(frozen=True)
class TerminalOutcome:
    """一轮结束时能同时看到的事实：写了哪些文件、验证到哪、终态是什么。"""

    status: str
    stop_reason: str
    answer: str
    files_written: tuple[str, ...]
    verification: str
    acceptance: str
    claim: str


def finish_outcome(answer, status, reason, *, files_written=(), verification="absent",
                   acceptance="absent", acceptance_total=0, acceptance_note=""):
    """按既有文句生成终态。不判断该不该结束，也不改验证是否算通过。"""
    files = tuple(files_written)
    if status == "returned":
        checked = "；自动验证通过" if verification == "passed" else ""
        note = acceptance_note if acceptance == "passed" else ""
        manual = MANUAL_PENDING if note else MANUAL_ABSENT
        stop = f"模型已返回回答{checked}{note}；{manual}"
    elif status == "incomplete":
        head = reason
        if acceptance == "not_run":
            head += "；" + acceptance_label(0, 0, acceptance_total)
        manual = MANUAL_PENDING if acceptance != "absent" else MANUAL_ABSENT
        stop = f"{head}；{manual}"
    else:
        stop = reason
    claim, answer, stop = judge_claim(answer, stop, claim_applies=False, files_written=files)
    return TerminalOutcome(status, stop, answer, files, verification, acceptance, claim)


def judge_claim(answer, stop_reason, *, claim_applies, files_written):
    """声称改过文件时，以编辑工具的实际写入为准。有写入不改终态；没写入只加核对，终态保持原值。"""
    text = answer or ""
    if UNBACKED_CLAIM_NOTE in text:
        return "unbacked", text, stop_reason
    claimed = bool(text) and CHANGE_CLAIM.search(text) is not None
    if not claimed:
        return "none", answer, stop_reason
    if files_written:
        return "backed", answer, stop_reason
    if not claim_applies:
        return "not_applicable", answer, stop_reason
    annotated = text.rstrip() + "\n\n" + UNBACKED_CLAIM_NOTE
    reason = stop_reason if UNBACKED_REASON in stop_reason else f"{stop_reason}；{UNBACKED_REASON}"
    return "unbacked", annotated, reason
