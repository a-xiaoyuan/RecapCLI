"""Plan and Execute 的阶段与计划状态；模型请求和工具执行仍由 agent.py 负责。"""

from dataclasses import dataclass, field
import json
import re


MAX_PLAN_STEPS = 12
MAX_REPLANS = 2

PLAN_INSTRUCTION = """当前阶段：制定计划（Plan）。
围绕用户当前请求，生成 1 到 5 个带依赖的具体步骤。简单问题可以只有一步。
尚未读取代码时，先安排定位和读取，不要猜测文件位置；修改任务应包含必要的验证。
计划必须遵守用户授权和当前开放能力。此阶段禁止调用工具，也不要声称已经执行。
只返回 JSON 对象，格式：{"steps": [{"id":"read","description":"定位并读取代码","kind":"read","depends_on":[]},
{"id":"edit","description":"修改问题","kind":"write","depends_on":["read"]},
{"id":"test","description":"运行相关验证","kind":"verify","depends_on":["edit"]}]}。
kind 为 read（只读）、write（修改）或 verify（运行验证）。依赖使用步骤 ID，不能自依赖或形成环。
只把真正独立的步骤设为无依赖；修改必须依赖定位，验证必须依赖修改。独立 read 步骤可以同批执行。
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

BATCH_REVIEW = """当前同批执行了多个独立只读步骤，必须逐项核对。
只返回 {"results":[{"id":"步骤ID","status":"completed 或 blocked","reason":"实际依据"}]}，
results 必须覆盖本批每个 ID，不能将某一步的证据用于宣称其他步骤完成。
需要重规划时，仍可返回 status=replan、reason 和 steps，替换全部未完成步骤。"""


@dataclass
class PlanStep:
    description: str
    status: str = "pending"
    evidence: str = ""
    id: str = ""
    kind: str = "write"
    depends_on: list[str] = field(default_factory=list)


class PlanFormatError(ValueError):
    """响应格式有误，可请求只修正格式，不重放工具。"""


def _read_object(content):
    if len(content.encode("utf-8")) > 32 * 1024:
        raise ValueError("计划响应超过 32 KiB")
    text = content.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        # 兼容模型在唯一 JSON 代码块前后附带解释；仍完整校验对象及步骤结构。
        blocks = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL | re.IGNORECASE)
        objects = []
        for block in blocks:
            try:
                candidate = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                objects.append(candidate)
        if len(objects) != 1:
            raise PlanFormatError("计划或核对响应必须包含唯一的合法 JSON 对象") from error
        data = objects[0]
    if not isinstance(data, dict):
        raise ValueError("计划或核对响应必须是 JSON 对象")
    return data


def plan_instruction(max_steps=MAX_PLAN_STEPS):
    return PLAN_INSTRUCTION.replace("1 到 5", f"1 到 {max_steps}")


def _read_steps(data, completed=(), max_steps=MAX_PLAN_STEPS):
    descriptions = data.get("steps")
    if not isinstance(descriptions, list) or not 1 <= len(descriptions) <= max_steps:
        raise ValueError(f"计划必须包含 1 到 {max_steps} 个步骤")
    steps = []
    completed_ids = {step.id for step in completed}
    for index, item in enumerate(descriptions):
        if isinstance(item, str):
            description = item
            step_id = f"step_{len(completed_ids) + index + 1}"
            kind = "write"
            dependencies = [steps[-1].id] if steps else []
        elif isinstance(item, dict):
            description = item.get("description")
            step_id, kind = item.get("id"), item.get("kind", "write")
            dependencies = item.get("depends_on", [])
        else:
            raise ValueError("计划步骤必须是文本或带 ID 的对象")
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise ValueError("计划步骤必须是 1 到 500 字的非空文本")
        if not isinstance(step_id, str) or not step_id.strip() or len(step_id) > 64:
            raise ValueError("步骤 ID 必须是 1 到 64 字的非空文本")
        if kind not in {"read", "write", "verify"}:
            raise ValueError("步骤 kind 必须是 read、write 或 verify")
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise ValueError("depends_on 必须是步骤 ID 列表")
        steps.append(PlanStep(description.strip(), id=step_id, kind=kind, depends_on=dependencies))
    ids = [step.id for step in steps]
    if len(set(ids)) != len(ids) or completed_ids.intersection(ids):
        raise ValueError("计划步骤 ID 不能重复或覆盖已完成步骤")
    resolved = set(completed_ids)
    pending = list(steps)
    while pending:
        ready = [step for step in pending if set(step.depends_on) <= resolved]
        if not ready:
            raise ValueError("计划依赖存在环或引用了未知步骤")
        resolved.update(step.id for step in ready)
        pending = [step for step in pending if step not in ready]
    return steps


def ready_batch(steps):
    """只读步骤可成批；修改、验证按依赖逐项执行。"""
    completed = {step.id for step in steps if step.status == "completed"}
    ready = [step for step in steps if step.status == "pending" and set(step.depends_on) <= completed]
    if not ready:
        return []
    return [step for step in ready if step.kind == "read"][:2] if ready[0].kind == "read" else ready[:1]


class PlanController:
    def __init__(self, on_event, max_steps=MAX_PLAN_STEPS, max_replans=MAX_REPLANS):
        self.max_steps, self.max_replans = max_steps, max_replans
        self.on_event = on_event
        self.phase = "plan"
        self.steps = []
        self.current = 0
        self.replans = 0
        self.stop_reason = ""
        self.batch = []
        self.verification_result = None

    @property
    def allows_tools(self):
        return self.phase == "execute"

    def describe(self):
        labels = {"pending": "待执行", "running": "执行中", "reviewing": "待核对",
                  "completed": "已完成", "blocked": "受阻"}
        lines = []
        for number, step in enumerate(self.steps, start=1):
            line = f"{number}. [{labels[step.status]}] {step.description}"
            if step.depends_on:
                line += "（依赖：" + ", ".join(step.depends_on) + "）"
            if step.evidence:
                line += f"；核对依据：{step.evidence}"
            lines.append(line)
        return "\n".join(lines) or "尚未形成计划"

    def instruction(self):
        if self.phase == "plan":
            return plan_instruction(self.max_steps)
        progress = "\n当前计划：\n" + self.describe()
        if self.phase == "execute":
            description = "；".join(step.description for step in self.batch)
            return ("当前阶段：执行步骤（Execute）。只执行当前步骤，结合已有实际结果，按需调用工具。"
                    "不要提前执行后续步骤。结束时说明完成内容、证据或阻碍，交由下一阶段核对。"
                    + ("本批均为独立只读步骤，可一次请求多个读取工具；禁止修改和命令。" if self.readonly_batch else "")
                    + f"\n当前步骤：{description}" + progress)
        if self.phase == "review":
            return REVIEW_INSTRUCTION.replace("1 到 5", f"1 到 {self.max_steps}") + ("\n" + BATCH_REVIEW if len(self.batch) > 1 else "") + progress
        return ("当前阶段：汇总（Summary）。禁止调用工具。基于实际执行记录回答用户，"
                "区分已完成、未完成和未验证的内容，并给出有依据的文件引用。"
                "计划未全部完成时，明确说明剩余事项，不得宣称任务已全部完成。"
                f"\n停止原因：{self.stop_reason or '全部计划步骤已经核对'}" + progress)

    def accept(self, content):
        """处理无工具调用的模型回答；只有汇总阶段结束整轮任务。"""
        if self.phase == "summary":
            return True
        if self.phase == "plan":
            self.steps = _read_steps(_read_object(content), max_steps=self.max_steps)
            self.on_event("[计划]\n" + self.describe())
            self._start_step()
        elif self.phase == "execute":
            for step in self.batch:
                step.status = "reviewing"
            self.phase = "review"
            self.on_event(f"[核对] 第 {self.current + 1} 步")
        else:
            self._accept_review(_read_object(content))
        return False

    def _start_step(self):
        self.verification_result = None
        self.batch = ready_batch(self.steps)
        if not self.batch:
            self.phase = "summary"
            return
        self.phase = "execute"
        self.current = self.steps.index(self.batch[0])
        for step in self.batch:
            step.status = "running"
        self.on_event(f"[步骤] {self.current + 1}/{len(self.steps)}：" + "；".join(step.description for step in self.batch))

    @property
    def readonly_batch(self):
        return bool(self.batch) and all(step.kind == "read" for step in self.batch)

    def observe_tool(self, name, result):
        if self.phase == "execute" and name in {"run_command", "command_status"}:
            self.verification_result = result

    def _accept_review(self, data):
        if data.get("status") == "replan":
            reason = data.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise ValueError("核对原因必须是 1 到 1000 字的非空文本")
            done = [step for step in self.steps if step.status == "completed"]
            remaining = _read_steps(data, done, max_steps=self.max_steps)
            if self.replans >= self.max_replans:
                for step in self.batch:
                    step.status = "blocked"
                self.stop(f"已达到 {self.max_replans} 次重规划上限；" + reason)
                return
            self.replans += 1
            self.steps = done + remaining
            self.on_event(f"[重规划] 第 {self.replans} 次：{reason}\n" + self.describe())
            self._start_step()
            return
        reviews = data.get("results") if len(self.batch) > 1 else [data]
        if not isinstance(reviews, list) or len(reviews) != len(self.batch):
            raise ValueError("必须逐项核对当前批次的所有步骤")
        if len(self.batch) > 1:
            if any(not isinstance(item, dict) for item in reviews):
                raise ValueError("核对结果必须是对象")
            if {item.get("id") for item in reviews} != {step.id for step in self.batch}:
                raise ValueError("核对结果 ID 必须与当前批次一致")
            reviews = [next(item for item in reviews if item["id"] == step.id) for step in self.batch]
        for review in reviews:
            self._validate_review(review)
        for step, review in zip(self.batch, reviews):
            if step.kind == "verify" and review["status"] == "completed" and not (self.verification_result or {}).get("success"):
                review = {"status": "blocked", "reason": "没有成功完成的验证命令；运行中或模型自述不能作为通过依据"}
            step.status = review["status"]
            step.evidence = review["reason"].strip()
            self.on_event(f"[{'完成' if step.status == 'completed' else '受阻'}] 第 {self.steps.index(step) + 1} 步：{step.evidence}")
        blocked = next((step for step in self.batch if step.status == "blocked"), None)
        if blocked:
            self.stop(blocked.evidence)
        else:
            self._start_step()

    @staticmethod
    def _validate_review(data):
        status = data.get("status")
        reason = data.get("reason")
        if status not in ("completed", "blocked"):
            raise ValueError("核对状态必须是 completed、replan 或 blocked")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("核对原因必须是 1 到 1000 字的非空文本")
        if data.get("steps", []) != []:
            raise ValueError("只有 replan 可以提供新的剩余步骤")

    def stop(self, reason):
        if self.phase != "summary":
            self.stop_reason = reason
            self.phase = "summary"
            self.on_event("[计划停止] " + reason)

    def final_answer(self, content):
        completed = sum(step.status == "completed" for step in self.steps)
        finished = bool(self.steps) and completed == len(self.steps) and not self.stop_reason
        state = "已完成" if finished else "未完成"
        header = f"计划状态：{state}（模型核对通过 {completed}/{len(self.steps)} 步；任务验收另行记录）。"
        if self.stop_reason:
            header += "\n停止原因：" + self.stop_reason
        return header + "\n\n" + content
