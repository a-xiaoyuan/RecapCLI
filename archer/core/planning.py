"""Plan and Execute 的阶段与计划状态；模型请求和工具执行仍由 agent.py 负责。"""

from dataclasses import dataclass
import json


MAX_PLAN_STEPS = 5
MAX_REPLANS = 2

PLAN_INSTRUCTION = """当前阶段：制定计划（Plan）。
围绕用户当前请求，生成 1 到 5 个按顺序执行的具体步骤。简单问题可以只有一步。
尚未读取代码时，先安排定位和读取，不要猜测文件位置；修改任务应包含必要的验证。
计划必须遵守用户授权和当前开放能力。此阶段禁止调用工具，也不要声称已经执行。
只返回 JSON 对象，格式：{"steps": ["步骤一", "步骤二"]}。
每个步骤最多 500 字。"""

REVIEW_INSTRUCTION = """当前阶段：核对当前步骤（Review）。
根据本轮实际工具调用、工具结果和执行说明，判断当前步骤是否完成。
不能只凭执行说明宣称成功；没有运行测试时不能说测试通过。
可以直接回答的问题不必调用工具。工具结果显示受阻时，说明原因，不要编造证据。
此阶段禁止调用工具，只返回 JSON 对象：
{"status": "completed", "reason": "本步骤的完成依据", "steps": []}
或者 {"status": "replan", "reason": "需要调整的原因", "steps": ["新的剩余步骤"]}
或者 {"status": "blocked", "reason": "无法继续的具体原因", "steps": []}。
replan 的 steps 替换从当前步骤起的全部未完成步骤，不能重复已完成部分；仍需覆盖用户目标。
新剩余步骤为 1 到 5 个，每个最多 500 字；reason 最多 1000 字。"""


@dataclass
class PlanStep:
    description: str
    status: str = "pending"
    evidence: str = ""


def _read_object(content):
    if len(content.encode("utf-8")) > 32 * 1024:
        raise ValueError("计划响应超过 32 KiB")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("计划或核对响应必须是合法 JSON") from error
    if not isinstance(data, dict):
        raise ValueError("计划或核对响应必须是 JSON 对象")
    return data


def _read_steps(data):
    descriptions = data.get("steps")
    if not isinstance(descriptions, list) or not 1 <= len(descriptions) <= MAX_PLAN_STEPS:
        raise ValueError("计划必须包含 1 到 5 个步骤")
    steps = []
    for description in descriptions:
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise ValueError("计划步骤必须是 1 到 500 字的非空文本")
        steps.append(PlanStep(description.strip()))
    return steps


class PlanController:
    def __init__(self, on_event):
        self.on_event = on_event
        self.phase = "plan"
        self.steps = []
        self.current = 0
        self.replans = 0
        self.stop_reason = ""

    @property
    def allows_tools(self):
        return self.phase == "execute"

    def describe(self):
        labels = {"pending": "待执行", "running": "执行中", "reviewing": "待核对",
                  "completed": "已完成", "blocked": "受阻"}
        lines = []
        for number, step in enumerate(self.steps, start=1):
            line = f"{number}. [{labels[step.status]}] {step.description}"
            if step.evidence:
                line += f"；核对依据：{step.evidence}"
            lines.append(line)
        return "\n".join(lines) or "尚未形成计划"

    def instruction(self):
        if self.phase == "plan":
            return PLAN_INSTRUCTION
        progress = "\n当前计划：\n" + self.describe()
        if self.phase == "execute":
            description = self.steps[self.current].description
            return ("当前阶段：执行步骤（Execute）。只执行当前步骤，结合已有实际结果，按需调用工具。"
                    "不要提前执行后续步骤。结束时说明完成内容、证据或阻碍，交由下一阶段核对。"
                    f"\n当前步骤：{description}" + progress)
        if self.phase == "review":
            return REVIEW_INSTRUCTION + progress
        return ("当前阶段：汇总（Summary）。禁止调用工具。基于实际执行记录回答用户，"
                "区分已完成、未完成和未验证的内容，并给出有依据的文件引用。"
                "计划未全部完成时，明确说明剩余事项，不得宣称任务已全部完成。"
                f"\n停止原因：{self.stop_reason or '全部计划步骤已经核对'}" + progress)

    def accept(self, content):
        """处理无工具调用的模型回答；只有汇总阶段结束整轮任务。"""
        if self.phase == "summary":
            return True
        if self.phase == "plan":
            self.steps = _read_steps(_read_object(content))
            self.on_event("[计划]\n" + self.describe())
            self._start_step()
        elif self.phase == "execute":
            self.steps[self.current].status = "reviewing"
            self.phase = "review"
            self.on_event(f"[核对] 第 {self.current + 1} 步")
        else:
            self._accept_review(_read_object(content))
        return False

    def _start_step(self):
        self.phase = "execute"
        step = self.steps[self.current]
        step.status = "running"
        self.on_event(f"[步骤] {self.current + 1}/{len(self.steps)}：{step.description}")

    def _accept_review(self, data):
        status = data.get("status")
        reason = data.get("reason")
        if status not in ("completed", "replan", "blocked"):
            raise ValueError("核对状态必须是 completed、replan 或 blocked")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("核对原因必须是 1 到 1000 字的非空文本")
        if status != "replan" and data.get("steps", []) != []:
            raise ValueError("只有 replan 可以提供新的剩余步骤")
        step = self.steps[self.current]
        step.evidence = reason.strip()
        if status == "completed":
            step.status = "completed"
            self.on_event(f"[完成] 第 {self.current + 1} 步：{step.evidence}")
            self.current += 1
            if self.current == len(self.steps):
                self.phase = "summary"
            else:
                self._start_step()
        elif status == "blocked":
            step.status = "blocked"
            self.stop(step.evidence)
        else:
            remaining = _read_steps(data)
            if self.replans >= MAX_REPLANS:
                step.status = "blocked"
                self.stop("已达到 2 次重规划上限；" + step.evidence)
                return
            self.replans += 1
            self.steps = self.steps[:self.current] + remaining
            self.on_event(f"[重规划] 第 {self.replans} 次：{reason}\n" + self.describe())
            self._start_step()

    def stop(self, reason):
        if self.phase != "summary":
            self.stop_reason = reason
            self.phase = "summary"
            self.on_event("[计划停止] " + reason)

    def final_answer(self, content):
        completed = sum(step.status == "completed" for step in self.steps)
        finished = bool(self.steps) and completed == len(self.steps) and not self.stop_reason
        state = "已完成" if finished else "未完成"
        header = f"计划状态：{state}（核对通过 {completed}/{len(self.steps)} 步）。"
        if self.stop_reason:
            header += "\n停止原因：" + self.stop_reason
        return header + "\n\n" + content
