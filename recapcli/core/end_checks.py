"""主代理收到候选答复后的结束检查。"""

from ..tools.commands import command_status
from .acceptance import MAX_ACCEPTANCE_ROUNDS, run_acceptance
from .auto_verify import MAX_VERIFY_ROUNDS, run_verification
from .hooks import MAX_STOP_BLOCKS
from .results import TurnResult


class EndChecks:
    def __init__(self, run):
        self.run = run
        self.verify_dirty = False
        self.verify_failures = 0
        self.verified = False
        self.stop_blocks = 0
        self.pending_rounds = 0
        self.pending_commands = set()
        self.acceptance_rounds = 0
        self.acceptance_current = False

    def changed(self):
        self.verify_dirty = True
        self.verified = False
        if self.acceptance_current:
            self.run.trace("acceptance_invalidated")
            self.acceptance_current = False

    def observe_tool(self, name, result, edited):
        if edited:
            self.changed()
        record_id = result.get("record_id")
        if not record_id or name not in {"run_command", "command_input", "stop_command", "command_status"}:
            return
        if name == "command_status" and record_id not in self.pending_commands:
            return
        self.changed()
        if name == "command_input" or result.get("status") in {"running", "unknown"}:
            self.pending_commands.add(record_id)
        else:
            self.pending_commands.discard(record_id)

    def _pending(self):
        ctx = self.run.ctx
        for record_id in sorted(self.pending_commands):
            if ctx.budget.time_low():
                break
            try:
                status = command_status(ctx.workspace, record_id, max_output_bytes=1)["status"]
            except (OSError, ValueError):
                continue
            if status not in {"running", "unknown"}:
                self.pending_commands.remove(record_id)
        return sorted(self.pending_commands)

    def _continue(self, feedback):
        self.run.reminder = {"role": "system", "content": feedback}

    def finish(self, answer, final_request):
        run, ctx = self.run, self.run.ctx
        note = run.todos.reminder() if run.todos is not None and not final_request else None
        if note is not None:
            run.trace("todo_reminder", open_items=run.todos.open_items())
            ctx.on_event("[待办] 清单还有未完成项，提醒模型继续")
            return self._continue(note)
        checking = ctx.role is None and bool(ctx.verify_command or ctx.acceptance)
        if checking and (pending := self._pending()):
            self.pending_rounds += 1
            reason = "命令仍在运行或结果未知，暂不能验证"
            run.trace("verification_pending", record_ids=pending, reason=reason)
            ctx.on_event(f"[验证] {reason}")
            if final_request or self.pending_rounds >= MAX_VERIFY_ROUNDS:
                return run.incomplete(answer, reason)
            return self._continue(f"{reason}。请用 command_status 核对这些命令：" + "、".join(pending))
        if ctx.hooks is not None and ctx.role is None:
            reason = ctx.hooks.run(run, "stop", {"answer": answer, "edited": run.edited})
            if reason is not None:
                self.stop_blocks += 1
                if final_request or self.stop_blocks >= MAX_STOP_BLOCKS:
                    return run.incomplete(answer, "结束 Hook 未放行")
                return self._continue(f"结束 Hook 要求继续处理：{reason}")
        if ctx.verify_command and ctx.role is None and self.verify_dirty:
            label = " ".join(ctx.verify_command)
            if ctx.budget.time_low():
                ctx.on_event("[验证] 时间不足，未运行")
                return run.incomplete(answer, f"时间不足，自动验证未运行（{label}）")
            self.verified, feedback = run_verification(run)
            self.verify_dirty = not self.verified
            if not self.verified:
                self.verify_failures += 1
                if final_request or self.verify_failures >= MAX_VERIFY_ROUNDS:
                    return run.incomplete(answer, f"自动验证未通过（{label}）")
                return self._continue(feedback)
        acceptance_note = ""
        if ctx.acceptance is not None:
            self.acceptance_rounds += 1
            self.acceptance_current = True
            passed, label, feedback, not_run = run_acceptance(run, self.acceptance_rounds)
            if not passed:
                if final_request or not_run or self.acceptance_rounds >= MAX_ACCEPTANCE_ROUNDS:
                    return run.incomplete(answer, label)
                return self._continue(f"{label}。请修复未通过项，再给出最终答复。\n{feedback}")
            acceptance_note = f"；{label}"
        if final_request and run.forced_reason and run.local_tool_calls > 0 and not run.read_summary_forced:
            return run.incomplete(answer, run.forced_reason)
        checked = "；自动验证通过" if self.verified else ""
        manual = "人工验收未登记" if acceptance_note else "任务验收未验证"
        return TurnResult(answer, ctx.turn, "returned", f"模型已返回回答{checked}{acceptance_note}；{manual}", run.new_summary)
