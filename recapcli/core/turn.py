"""一轮任务的主循环：组织上下文、请求模型、分发回复，并在预算用完时收尾。"""

from dataclasses import dataclass
import json
from typing import Any, Callable

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from ..llm.protocols import assistant_record
from ..llm.streaming import IncompleteStreamError
from ..tools import tool_definitions
from .acceptance import acceptance_label
from .budget import BudgetExhausted, SKIPPED_RESULT
from .compaction import finish_compaction, prepare_compaction, summary_text
from .context import MAX_HISTORY_MESSAGES, TokenMeter, json_tokens, select_history
from .end_checks import EndChecks
from .model_call import COMPACT_RESERVE, request_model
from .planning import PlanFormatError
from .results import (TraceError, TurnResult, clear_old_tool_results, close_turn, context_overflow,
                      fallback_answer, has_tool_results, is_tool_call_text)
from .todos import TodoList
from .tool_batch import ToolBatch
from ..tools.checkpoints import turn_checkpoint

COMPACT_AT = 0.75          # 当前轮超过窗口的这个比例时，移出较早的工具结果
READ_SUMMARY_AT = 4        # 连续只读轮数达到这里时提醒模型先总结或开始修改
READ_FORCE_SUMMARY_AT = 8  # 只读任务连续达到这里时强制收尾；可写任务改为进入写入阶段
EDIT_PHASE_RESERVE = 6     # 剩余请求不超过这个数且仍未写入时，也进入写入阶段
# 写入阶段只留修改、执行、读文件和待办，不再提供仓库搜索。
EDIT_PHASE_TOOLS = frozenset({
    "edit_file", "edit_files", "undo_edit", "rewind_checkpoint", "restore_file",
    "run_command", "command_status", "command_input", "stop_command",
    "read_file", "todo_write",
})
FINAL_INSTRUCTION = ("本次是最后一次模型请求，不能调用工具。请基于已有事实直接给出最终答复；"
                     "仍需更多信息或操作时，说明未完成项和原因。不要继续规划或输出工具调用标记。")
TOOL_TEXT_CORRECTION = ("上一条输出是无效的工具调用文本。现在不能调用任何工具。"
                        "请用自然语言直接回答；不要输出 DSML、XML、tool_calls、"
                        "函数调用标记或工具参数。")
FINAL_SUMMARY_INSTRUCTION = ("现在停止读取，不要调用任何工具。请直接基于已读内容和已有工具结果，"
                             "用自然语言回答用户最初的问题。")
PLANNER_FINAL_INSTRUCTION = "仅返回要求的计划 JSON，不调用工具，不执行计划。"


def model_failure_reason(error):
    """可以转入收尾的模型故障；鉴权、参数、模型拒绝等错误返回空，继续按失败处理。"""
    if isinstance(error, IncompleteStreamError):
        return "模型流式连接中断" if error.interrupted else ""
    if context_overflow(error):
        return "服务端报告上下文超限"
    if isinstance(error, APITimeoutError):
        return "模型请求超时"
    if isinstance(error, APIConnectionError):
        return "模型连接中断"
    if isinstance(error, APIStatusError) and (error.status_code == 429 or error.status_code >= 500):
        return f"模型服务返回 HTTP {error.status_code}"
    return ""


@dataclass
class TurnContext:
    """一轮任务开始后不再变化的输入；可变进度放在 TurnRunner 上。"""

    client: Any
    model: str
    prompt: str
    workspace: Any
    max_steps: int
    mode: str
    limits: Any
    budget: Any
    capabilities: frozenset
    approver: Callable
    access_policy: Any
    rules: dict
    structure: dict | None
    memory_text: str
    system_text: str
    planner: Any
    user_message: dict
    turn: list
    definitions: list
    extra_definitions: list
    extra_names: set
    extra_tools: Any
    extra_executor: Callable | None
    history: list
    summary: dict | None
    role: str | None
    request_reserve: int
    tools_enabled: bool
    auto_compact: bool
    compact_only: bool
    on_event: Callable
    on_trace: Callable | None
    on_stream: Callable | None
    verify_command: list | None = None
    hooks: Any = None
    skills: Any = None
    acceptance: Any = None
    read_only: bool = False
    allow_edit_phase: bool = True


class TurnRunner:
    def __init__(self, ctx):
        self.ctx = ctx
        self.requests_used = 0
        self.local_tool_calls = 0
        self.read_rounds = 0        # 连续只读工具轮数；写入、命令或核对会清零
        self.edited = []
        self.edit_phase = False     # 可写且尚未写入时，读满期限后收窄工具
        self.notified_read_summary = False
        self.notified_edit_deadline = False
        self.end_checks = EndChecks(self)
        self.format_correction = None
        self.corrected_phases = set()
        self.meter = TokenMeter()
        self.todos = TodoList() if any(item["function"]["name"] == "todo_write" for item in ctx.definitions) else None
        self.summary = ctx.summary
        self.new_summary = None
        self.forced_reason = ""     # 非空表示因预算转入收尾
        self.read_summary_forced = False  # 只读过多触发的收尾不是失败
        self.reminder = None
        self.system_text = ctx.system_text
        self.history_for_model = ctx.history
        self.final_message = {"role": "system", "content": (
            PLANNER_FINAL_INSTRUCTION if ctx.role == "planner" else FINAL_INSTRUCTION)}
        self.window = ctx.limits.max_context_tokens
        self.definition_tokens = json_tokens(ctx.definitions) if ctx.definitions else 0

    def trace(self, kind, **data):
        if self.ctx.on_trace is not None:
            try:
                self.ctx.on_trace(kind, data)
            except (OSError, ValueError, RuntimeError) as error:
                raise TraceError(str(error)) from error

    def plan_snapshot(self):
        planner = self.ctx.planner
        if planner is not None:
            self.trace("plan", phase=planner.phase, current=planner.current, replans=planner.replans,
                       stop_reason=planner.stop_reason,
                       steps=[{"id": item.id, "kind": item.kind, "depends_on": item.depends_on,
                               "description": item.description, "status": item.status,
                               "model_review": item.evidence} for item in planner.steps])

    def system_message(self):
        planner = self.ctx.planner
        instruction = "" if planner is None else "\n\n" + planner.instruction()
        note = self.todos.context_note() if self.todos is not None else ""
        return {"role": "system", "content": self.system_text + instruction + ("\n\n" + note if note else "")}

    def fits_context(self, messages, pending_results=0):
        """当前轮（含系统提示、收尾消息、工具定义和尚未回填的结果占位）是否放得进窗口。"""
        placeholder = {"role": "tool", "tool_call_id": "x", "content": json.dumps(SKIPPED_RESULT, ensure_ascii=False)}
        total = self.meter.count([self.system_message(), *messages, *([placeholder] * pending_results), self.final_message])
        return total + self.definition_tokens <= self.window

    def incomplete(self, answer, reason):
        if self.ctx.acceptance is not None and not self.end_checks.acceptance_current:
            reason += "；" + acceptance_label(0, 0, len(self.ctx.acceptance.checks))
        manual = "人工验收未登记" if self.ctx.acceptance is not None else "任务验收未验证"
        return TurnResult(answer, self.ctx.turn, "incomplete", f"{reason}；{manual}", self.new_summary)

    def finish_incomplete(self, reason, correction=None):
        """预算用完：先尝试不带工具的汇总；模型输出工具文本时再纠正一次，最后用事实兜底。"""
        ctx = self.ctx
        turn, planner = ctx.turn, ctx.planner
        close_turn(turn)
        self.trace("budget_stop", reason=reason, snapshot=ctx.budget.snapshot())
        ctx.on_event(f"[收尾] {reason}，转入最终汇总")
        if planner is not None and not planner.stop_reason:
            planner.stop(reason)
            self.plan_snapshot()
        answer = None
        try:
            for attempt in range(2):
                if self.requests_used >= ctx.max_steps:
                    break
                system = self.system_message()
                final_nudge = ({"role": "user", "content": FINAL_SUMMARY_INSTRUCTION}
                               if ctx.mode == "react" and ctx.role is None and ctx.planner is None
                               else None)
                closing = (([{"role": "system", "content": correction}] if correction else [])
                           + [self.final_message] + ([final_nudge] if final_nudge is not None else []))
                clear_old_tool_results(turn, lambda: self.meter.count([system, *turn, *closing]) <= self.window, keep=1)
                messages = [system, *turn, *closing]
                if self.meter.count(messages) > self.window:
                    break
                response = request_model(self, messages, planner.phase if planner else "react", final=True,
                                         tools=self.final_tools(), tools_allowed=False)
                message = response.choices[0].message if response.choices else None
                content = message.content if message is not None else None
                if content and content.strip() and not is_tool_call_text(content):
                    answer = content
                    break
                if is_tool_call_text(content):
                    self.trace("final_summary_tool_text", attempt=attempt + 1)
                    correction = TOOL_TEXT_CORRECTION
        except (BudgetExhausted, APIError, RuntimeError, ValueError) as error:
            self.trace("final_summary_failed", error_type=type(error).__name__)
        if answer is None:
            answer = fallback_answer(reason, turn, self.todos or TodoList(), self.edited)
        turn.append({"role": "assistant", "content": answer})
        self.trace("message", message=turn[-1])
        return self.incomplete(answer, reason)

    def compact_history(self, reserved_tokens):
        """按需压缩历史。compact_only 时返回最终结果，否则返回 None 继续执行。"""
        ctx = self.ctx
        covered = self.summary["covered_messages"] if self.summary else 0
        uncovered = ctx.history[covered:]
        needs_compaction = (len(uncovered) > MAX_HISTORY_MESSAGES
                            or self.meter.count([{"role": "system", "content": ctx.system_text + summary_text(self.summary)},
                                                 *uncovered, *ctx.turn]) > (ctx.limits.max_context_tokens - reserved_tokens) * COMPACT_AT)
        reserve = COMPACT_RESERVE.get(ctx.mode, 1)
        if ctx.compact_only or (ctx.auto_compact and needs_compaction and ctx.max_steps > reserve):
            try:
                compact_messages, end = prepare_compaction(ctx.history, self.summary, ctx.limits.context_bytes)
                if compact_messages is not None:
                    response = request_model(self, compact_messages, "compact")
                    candidate = finish_compaction(response, ctx.history, self.summary, end, ctx.model, ctx.limits.context_bytes)
                    self.trace("compaction", summary=candidate, automatic=not ctx.compact_only)
                    self.summary = self.new_summary = candidate
                    ctx.on_event(f"[压缩] 覆盖前 {end} 条消息；{candidate['before_bytes']} → {candidate['after_bytes']} 字节；原始历史保留")
                elif ctx.compact_only:
                    return TurnResult("没有可压缩的旧轮次：始终保留最近两个完整轮次。", [])
            except TraceError:
                raise
            except (APIError, ValueError, RuntimeError) as error:
                self.trace("compaction_failed", error_type=type(error).__name__,
                           reason=str(error) if not isinstance(error, APIError) else "摘要模型请求失败",
                           automatic=not ctx.compact_only)
                if ctx.compact_only:
                    raise
                ctx.on_event("[压缩] 未完成，保留原数据并回退到现有历史选择策略。")
        elif ctx.auto_compact and needs_compaction:
            self.trace("compaction_skipped", reason="模型请求预算需保留给正常执行与汇总")
        if ctx.compact_only:
            return TurnResult("历史压缩完成，原始消息完整保留；/summary 查看摘要与覆盖范围。", [],
                              summary=self.new_summary)
        return None

    def run_multi(self):
        ctx = self.ctx
        shared = dict(budget=ctx.budget, limits=ctx.limits, max_steps=ctx.max_steps, capabilities=ctx.capabilities,
                      history=ctx.history, memory_text=ctx.memory_text, summary=self.summary, new_summary=self.new_summary,
                      on_event=ctx.on_event, on_trace=ctx.on_trace, approver=ctx.approver, user_message=ctx.user_message,
                      on_stream=ctx.on_stream, access_policy=ctx.access_policy)
        if ctx.capabilities & {"write", "exec"}:
            from .team import run_team
            return run_team(ctx.client, ctx.model, ctx.prompt, ctx.workspace, extra_tools=ctx.extra_tools,
                            extra_executor=ctx.extra_executor, **shared)
        from .subagents import run_subagents
        return run_subagents(ctx.client, ctx.model, ctx.prompt, ctx.workspace, **shared)

    def exploring_edit(self):
        """有写入或执行能力，调用方没有把任务标成只读，且本轮还没改过文件。

        能力里只有 read 时这里为假，连续读取仍会在第 8 轮强制总结。
        调用方也可以用 read_only 声明本任务只读，即使能力里带着 write/exec。
        """
        if self.ctx.read_only:
            return False
        return bool(self.ctx.capabilities & {"write", "exec"}) and not self.edited

    def _push_edit_phase(self):
        """理解题可以关掉写入阶段，避免被收窄成只能改文件。"""
        return self.exploring_edit() and self.ctx.allow_edit_phase

    def _edit_phase_due(self):
        if not self._push_edit_phase():
            return False
        remaining = self.ctx.max_steps - self.requests_used
        return self.read_rounds >= READ_FORCE_SUMMARY_AT or remaining <= EDIT_PHASE_RESERVE

    def _read_spans(self):
        """已成功读到的文件和行段，供写入期限提醒列出来。"""
        calls = {}
        for message in self.ctx.turn:
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                if function.get("name") == "read_file":
                    calls[call.get("id")] = function.get("arguments") or ""
        spans = []
        seen = set()
        for message in self.ctx.turn:
            if message.get("role") != "tool" or message.get("tool_call_id") not in calls:
                continue
            reference = None
            try:
                payload = json.loads(message.get("content") or "")
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and not payload.get("cleared") and payload.get("ok", True):
                reference = payload.get("reference") or payload.get("path")
            if not reference:
                raw = calls[message.get("tool_call_id")]
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, TypeError):
                    arguments = {}
                if isinstance(arguments, dict) and arguments.get("path"):
                    start = arguments.get("start_line") or arguments.get("next_start_line")
                    end = arguments.get("end_line")
                    if not start:
                        reference = arguments["path"]
                    elif not end or start == end:
                        reference = f"{arguments['path']}:{start}"
                    else:
                        reference = f"{arguments['path']}:{start}-{end}"
            if reference and reference not in seen:
                seen.add(reference)
                spans.append(reference)
        return spans

    def _set_read_reminder(self, content, event, kind):
        self.reminder = {"role": "system", "content": content}
        self.trace("read_summary_reminder", rounds=self.read_rounds, notice=kind)
        self.ctx.on_event(event)

    def _maybe_read_reminder(self, exploring):
        """可写任务只在第 4 轮和第 8 轮各提醒一次；只读任务保持原来的重复提醒。"""
        if self.read_rounds < READ_SUMMARY_AT:
            return
        if self._push_edit_phase():
            if self.read_rounds == READ_SUMMARY_AT and not self.notified_read_summary:
                self.notified_read_summary = True
                self._set_read_reminder(
                    (f"已连续读取 {self.read_rounds} 轮，尚未写入文件。"
                     "请根据已读内容调用修改或验证工具完成用户要求的改动；"
                     "不要重复读取同一文件。信息不够时再读最必要的片段。"),
                    f"[提醒] 已连续读取 {self.read_rounds} 轮，请开始修改",
                    "edit")
                return
            budget_due = self.edit_phase and self.ctx.max_steps - self.requests_used <= EDIT_PHASE_RESERVE
            if (not self.notified_edit_deadline and self.read_rounds != READ_SUMMARY_AT
                    and (self.read_rounds == READ_FORCE_SUMMARY_AT or budget_due)):
                self.notified_edit_deadline = True
                self.notified_read_summary = True
                spans = self._read_spans()
                listed = "\n".join(f"- {item}" for item in spans[:20])
                if len(spans) > 20:
                    listed += f"\n- 等 {len(spans)} 段"
                if not listed:
                    listed = "- （没有记录到已读行段）"
                self._set_read_reminder(
                    (f"已连续读取 {self.read_rounds} 轮，仍未写入文件。已读内容：\n{listed}\n"
                     "下一步请调用 edit_file 或 edit_files 完成用户要求的修改。"
                     "如果确实无法修改，直接说明原因并结束，不要再搜索或继续翻页。"),
                    f"[提醒] 已连续读取 {self.read_rounds} 轮，请改为写入",
                    "edit_deadline")
            return
        if exploring:
            content = (f"已连续读取 {self.read_rounds} 轮，尚未写入文件。"
                       "请根据已读内容调用修改或验证工具完成用户要求的改动；"
                       "不要重复读取同一文件。信息不够时再读最必要的片段。")
            event = f"[提醒] 已连续读取 {self.read_rounds} 轮，请开始修改"
        else:
            content = (f"已连续读取 {self.read_rounds} 轮。先基于现有事实总结已确认内容、缺口和下一步；"
                       "需要继续时只读最必要的文件，不要重复读取同一文件。")
            event = f"[提醒] 已连续读取 {self.read_rounds} 轮，请先总结再继续"
        self._set_read_reminder(content, event, "read" if not exploring else "edit")

    def start_final_request(self, step):
        """判断这一步是否必须是收尾请求，并记录原因。"""
        ctx = self.ctx
        budget, planner = ctx.budget, ctx.planner
        exploring = self.exploring_edit()
        if self._edit_phase_due():
            self.edit_phase = True
        elif not self._push_edit_phase():
            self.edit_phase = False
        read_stop = ctx.mode == "react" and self.read_rounds >= READ_FORCE_SUMMARY_AT and not exploring
        final_request = (bool(self.forced_reason) or read_stop or step == ctx.max_steps
                         or budget.should_summarize(ctx.request_reserve))
        if final_request and not self.forced_reason:
            self.forced_reason = (
                f"连续读取 {self.read_rounds} 轮，转入总结" if read_stop
                else "模型请求次数用完" if step == ctx.max_steps or budget.requests >= budget.max_requests - ctx.request_reserve - 1
                else "工具调用次数用完" if budget.tool_calls >= ctx.limits.max_tool_calls
                else "执行时间即将用完")
            self.read_summary_forced = read_stop
        if not final_request and ctx.mode == "react" and self.reminder is None:
            self._maybe_read_reminder(exploring)
        if planner is not None and final_request:
            planner.stop("模型请求或工具预算即将耗尽，保留本次请求汇总已有结果")
            self.plan_snapshot()
        return final_request

    def build_messages(self, final_request):
        """按窗口组织本次请求；放不下时返回收尾结果。"""
        ctx = self.ctx
        turn = ctx.turn
        final_nudge = ({"role": "user", "content": FINAL_SUMMARY_INSTRUCTION}
                       if final_request and ctx.mode == "react" and ctx.role is None and ctx.planner is None
                       else None)
        closing = (([self.format_correction] if self.format_correction is not None else [])
                   + ([self.reminder] if self.reminder is not None else [])
                   + ([self.final_message] if final_request else [])
                   + ([final_nudge] if final_nudge is not None else []))
        self.reminder = None
        system = self.system_message()
        tool_tokens = (self.definition_tokens
                       if (not final_request or (self.ctx.mode == "react" and self.ctx.role is None
                                                 and self.ctx.planner is None))
                       else 0)

        def turn_cost():
            return self.meter.count([system, *turn, *closing]) + tool_tokens

        # 当前轮接近窗口时先移出较早的工具结果，再考虑舍弃历史摘要。
        if turn_cost() > self.window * COMPACT_AT:
            cleared = clear_old_tool_results(turn, lambda: turn_cost() <= self.window * COMPACT_AT)
            if cleared:
                self.trace("context_cleared", cleared=cleared, reason="当前轮接近上下文窗口")
                ctx.on_event(f"[上下文] 已移出 {cleared} 条较早的工具结果")
        available = self.window - turn_cost() - 1
        if available < 0 and self.system_text != ctx.system_text:
            # 摘要也是可舍弃的历史，不能挤掉当前问题或未闭合工具结果。
            self.system_text = ctx.system_text
            system = self.system_message()
            self.history_for_model = ctx.history
            available = self.window - turn_cost() - 1
            self.trace("summary_omitted", reason="为当前轮次保留上下文空间")
        if available < 0:
            if len(turn) == 1:
                raise ValueError("当前问题本身超过模型上下文窗口，请缩短问题或附件")
            return self.finish_incomplete("上下文窗口已满"), None
        selected = select_history(self.history_for_model, MAX_HISTORY_MESSAGES, max_tokens=self.meter.raw_limit(available))
        return [system, *selected, *turn, *closing], selected

    def tools_for_request(self, final_request):
        ctx = self.ctx
        if final_request:
            # 保留工具定义并显式 tool_choice=none，DeepSeek 等模型更不容易把工具调用写成 XML 文本。
            if ctx.mode == "react" and ctx.role is None and ctx.planner is None:
                return ctx.definitions
            return None
        if ctx.planner is not None and ctx.planner.readonly_batch:
            return tool_definitions(ctx.capabilities & {"read", "web"}) + ctx.extra_definitions
        if self.edit_phase:
            # 不使用 tool_choice=required：模型确实无法修改时，仍可以用文字说明并结束。
            return [item for item in ctx.definitions if item["function"]["name"] in EDIT_PHASE_TOOLS]
        return ctx.definitions

    def final_tools(self):
        return self.tools_for_request(True)

    def handle_plan_reply(self, message, final_request):
        """计划模式的文本回复。返回结果表示本轮结束，None 表示继续下一步。"""
        ctx = self.ctx
        planner, turn = ctx.planner, ctx.turn
        try:
            finished = planner.accept(message.content)
        except PlanFormatError as error:
            phase_key = (planner.phase, tuple(item.id for item in planner.batch))
            if planner.phase != "review" or phase_key in self.corrected_phases or final_request:
                raise
            self.corrected_phases.add(phase_key)
            self.format_correction = {"role": "system", "content": (
                f"上一条核对响应格式错误：{error}。这里只修正输出格式，不执行或重放任何工具。"
                "请根据已记录事实，仅返回要求的 JSON 对象，禁止解释文字。\n" + planner.instruction())}
            self.trace("plan_format_retry", phase=planner.phase, step_ids=list(phase_key[1]))
            ctx.on_event("[格式修正] 核对响应未按 JSON 返回，仅修正格式，不重放操作")
            return None
        self.format_correction = None
        self.plan_snapshot()
        if not finished:
            return None
        answer = planner.final_answer(message.content)
        turn[-1]["content"] = answer
        if answer != message.content:
            turn[-1].pop("provider_state", None)
        complete = bool(planner.steps) and all(item.status == "completed" for item in planner.steps)
        blocked = any(item.status == "blocked" for item in planner.steps)
        if blocked:
            status = "blocked"
        elif complete and not planner.stop_reason:
            status = "returned"
        else:
            status = "incomplete"
        return TurnResult(answer, turn, status, planner.stop_reason or "计划已由模型核对；任务验收未验证", self.new_summary)

    def handle_text_reply(self, message, final_request):
        ctx = self.ctx
        if not message.content or not message.content.strip():
            raise RuntimeError("模型既没有返回正文，也没有请求工具")
        if is_tool_call_text(message.content):
            self.trace("tool_call_text", final=final_request)
            if final_request:
                return self.finish_incomplete("模型返回了工具调用文本，未给出最终答复",
                                              correction=TOOL_TEXT_CORRECTION)
            self.reminder = {
                "role": "system",
                "content": "上一条输出是工具调用标记，不是有效答复。不要输出 XML/DSML 工具调用；"
                           "需要工具时使用原生 tool_calls，否则直接给出结论。",
            }
            ctx.on_event("[提醒] 模型输出了工具调用文本，已要求重新答复")
            return None
        ctx.turn.append(assistant_record(message))
        self.trace("message", message=ctx.turn[-1])
        if ctx.planner is not None:
            return self.handle_plan_reply(message, final_request)
        return self.end_checks.finish(message.content, final_request)

    def handle_response(self, response, step, final_request, tools_allowed):
        ctx = self.ctx
        turn = ctx.turn
        if not response.choices:
            raise RuntimeError("模型没有返回候选消息")
        choice = response.choices[0]
        if choice.finish_reason == "length" and has_tool_results(turn):
            # 被截断的正文和工具参数都不可靠，不写入本轮，也不执行。
            return self.finish_incomplete("模型输出达到长度上限")
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise RuntimeError(f"模型响应未完整结束：{choice.finish_reason}")
        message = choice.message
        if message.tool_calls and final_request:
            # 服务忽略了 tool_choice=none：不执行工具，有正文就用正文收尾。
            if (message.content and message.content.strip() and ctx.planner is None
                    and not is_tool_call_text(message.content)):
                turn.append({"role": "assistant", "content": message.content})
                self.trace("message", message=turn[-1])
                return self.incomplete(message.content, self.forced_reason)
            return self.finish_incomplete(self.forced_reason or f"已达到 {ctx.max_steps} 次模型请求上限")
        if not message.tool_calls:
            return self.handle_text_reply(message, final_request)
        if not tools_allowed:
            raise RuntimeError("规划、核对和汇总阶段禁止调用工具，未执行这批调用")
        ids = [call.id for call in message.tool_calls]
        if len(set(ids)) != len(ids) or any(not call_id for call_id in ids):
            raise RuntimeError("模型返回了缺失或重复的工具调用 ID")
        turn.append(assistant_record(message))
        self.trace("message", message=turn[-1])
        ToolBatch(self, message, step).execute()
        return None

    def step(self):
        """执行一步。返回结果表示本轮结束，None 表示继续。"""
        ctx = self.ctx
        planner = ctx.planner
        step = self.requests_used + 1
        final_request = self.start_final_request(step)
        messages, selected = self.build_messages(final_request)
        if isinstance(messages, TurnResult):
            return messages
        tools_allowed = ctx.tools_enabled and not final_request and (planner is None or planner.allows_tools)
        try:
            response = request_model(self, messages, planner.phase if planner else "react",
                                     tools=self.tools_for_request(final_request), tools_allowed=tools_allowed,
                                     history_messages=len(selected), final=final_request)
        except BudgetExhausted as error:
            if final_request:
                return self.finish_incomplete(str(error))
            self.forced_reason = str(error)
            return None
        except (APIError, IncompleteStreamError) as error:
            # 工具事实已产生时，服务故障也要交代进度；收尾只汇总，不重放工具。
            reason = model_failure_reason(error)
            if reason and (has_tool_results(ctx.turn) or self.end_checks.acceptance_rounds):
                return self.finish_incomplete(reason)
            raise
        return self.handle_response(response, step, final_request, tools_allowed)

    def run(self):
        with turn_checkpoint(self.ctx.workspace) as checkpoint:
            result = self._run_turn()
        if checkpoint.get("saved"):
            self.trace("checkpoint", id=checkpoint["id"], files=sorted(checkpoint["files"]))
        return result

    def _run_turn(self):
        ctx = self.ctx
        if ctx.acceptance is not None:
            self.trace("acceptance_config", sha256=ctx.acceptance.sha256, checks=ctx.acceptance.catalog())
        self.trace("project_rules", **{key: value for key, value in ctx.rules.items() if key != "text"})
        if ctx.structure is not None:
            self.trace("repository_context", **{key: value for key, value in ctx.structure.items() if key != "map"})
        finished = self.compact_history(self.definition_tokens)
        if finished is not None:
            return finished
        if ctx.mode == "multi":
            return self.run_multi()
        self.system_text += ("\n\n" + summary_text(self.summary)) if self.summary else ""
        self.history_for_model = ctx.history[self.summary["covered_messages"]:] if self.summary else ctx.history
        try:
            while self.requests_used < ctx.max_steps:
                result = self.step()
                if result is not None:
                    return result
        except BudgetExhausted as error:
            # 时间在工具执行中途耗尽等情况：已完成的结果都在 turn 里，照常收尾。
            return self.finish_incomplete(str(error))
        return self.finish_incomplete(self.forced_reason or f"已达到 {ctx.max_steps} 次模型请求上限")
