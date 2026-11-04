"""执行模型一次返回的工具调用，并把结果放回当前轮。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json

from ..tools import execute_tool
from .budget import SKIPPED_RESULT
from .results import clear_old_tool_results, edited_paths, fact_result, oversized_result
from .todos import TODO_TOOL

PARALLEL_READ_TOOLS = frozenset({"read_file", "find_files", "list_directory", "search_text", "read_url", "web_search"})
MAX_PARALLEL_READS = 4


def _can_parallel_reads(tool_calls):
    return (len(tool_calls) > 1
            and all(call.function.name in PARALLEL_READ_TOOLS for call in tool_calls))


def _tool_location(call):
    try:
        arguments = json.loads(call.function.arguments)
        return arguments.get("path", arguments.get("cwd", "")) if isinstance(arguments, dict) else ""
    except (ValueError, TypeError):
        return ""


class ToolBatch:
    def __init__(self, run, message, step):
        self.run = run
        self.ctx = run.ctx
        self.calls = message.tool_calls
        self.step = step
        self.allowed = self.ctx.budget.reserve_tools(len(self.calls))

    def tool_time(self):
        return max(1.0, self.ctx.budget.working_time())

    def bound_approver(self, request):
        request.check = self.ctx.budget.remaining_time
        request.time_remaining = self.tool_time()
        return self.ctx.approver(request)

    def action_id(self, index, call):
        role = self.ctx.role
        return f"{role + ':' if role else ''}{self.step}:{index}:{call.id}"

    def announce(self, call):
        location = _tool_location(call)
        self.ctx.on_event(f"[工具] {call.function.name}" + (f" · {location}" if location else ""))

    def run_parallel_reads(self, runnable):
        ctx, trace = self.ctx, self.run.trace
        results = {}

        def read_one(call, index):
            return execute_tool(
                call.function.name, call.function.arguments, ctx.workspace,
                capabilities=ctx.capabilities & {"read", "web"}, time_remaining=self.tool_time(), for_model=True,
                access_policy=ctx.access_policy, approver=self.bound_approver,
                on_approval=lambda payload: trace("approval", action_id=self.action_id(index, call), call_id=call.id, **payload),
            )

        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_READS, len(runnable))) as pool:
            futures = {}
            for index, call in enumerate(runnable):
                trace("tool_start", action_id=self.action_id(index, call), call_id=call.id, name=call.function.name,
                      arguments=call.function.arguments, skipped=False)
                self.announce(call)
                futures[pool.submit(read_one, call, index)] = index
            for future in as_completed(futures):
                index = futures[future]
                call = runnable[index]
                try:
                    results[index] = future.result()
                except BaseException as error:
                    trace("tool_interrupted", action_id=self.action_id(index, call), call_id=call.id,
                          name=call.function.name, error_type=type(error).__name__, outcome="unknown")
                    results[index] = error
                else:
                    trace("tool_result", action_id=self.action_id(index, call), call_id=call.id, name=call.function.name,
                          result=fact_result(results[index]))
        return results

    def run_one(self, call, action_id, skip_tool):
        ctx, run = self.ctx, self.run
        trace = run.trace

        def record_artifact(kind, record_id):
            trace("artifact", action_id=action_id, call_id=call.id, record_type=kind, record_id=record_id)

        def report_approval(payload):
            trace("approval", action_id=action_id, call_id=call.id, **payload)

        planner = ctx.planner
        granted = ctx.capabilities & {"read", "web"} if planner is not None and planner.readonly_batch else ctx.capabilities
        try:
            if skip_tool:
                return SKIPPED_RESULT
            if call.function.name == TODO_TOOL["function"]["name"] and run.todos is not None:
                result = run.todos.update(call.function.arguments)
                if result["ok"]:
                    trace("todo", items=run.todos.items)
                    ctx.on_event("[待办]\n" + run.todos.render())
                return result
            if ctx.extra_executor is not None and call.function.name in ctx.extra_names:
                return ctx.extra_executor(
                    call.function.name, call.function.arguments, capabilities=granted,
                    approver=self.bound_approver, on_approval=report_approval,
                    time_remaining=self.tool_time(), check=ctx.budget.remaining_time,
                )
            return execute_tool(
                call.function.name, call.function.arguments, ctx.workspace, capabilities=granted,
                time_remaining=self.tool_time(), on_record=record_artifact, access_policy=ctx.access_policy,
                approver=self.bound_approver, on_approval=report_approval, for_model=True, check=ctx.budget.remaining_time,
            )
        except BaseException as error:
            trace("tool_interrupted", action_id=action_id, call_id=call.id, name=call.function.name,
                  error_type=type(error).__name__, outcome="unknown")
            raise

    def report(self, call, result):
        ctx = self.ctx
        if result.get("summary") and call.function.name != TODO_TOOL["function"]["name"]:
            ctx.on_event(f"[操作] {result['summary']}")
        if ctx.planner is not None:
            ctx.planner.observe_tool(call.function.name, result)
        if result.get("diff"):
            ctx.on_event("[差异]\n" + result["diff"])
        if result.get("output"):
            ctx.on_event("[命令输出]\n" + result["output"])

    def fit_into_turn(self, call, result, pending):
        """放入结果；放不下时先清理旧结果，再退回只保留收据。"""
        ctx, run = self.ctx, self.run
        content = ctx.budget.encode_result(result)
        tool_message = {"role": "tool", "tool_call_id": call.id, "content": content}

        def fits():
            return run.fits_context([*ctx.turn, tool_message], pending)

        if not fits():
            cleared = clear_old_tool_results(ctx.turn, fits)
            if cleared:
                run.trace("context_cleared", cleared=cleared, reason="容纳新的工具结果")
        if not fits():
            result = oversized_result(result, run.meter.count(content))
            tool_message["content"] = ctx.budget.encode_result(result)
        return result, tool_message

    def report_outcome(self, result, skip_tool):
        on_event = self.ctx.on_event
        if result.get("approval"):
            on_event(f"[审批] {result['approval']}")
        if skip_tool:
            on_event("[结果] 未执行：预算即将用完")
        elif result.get("ok") and "returncode" in result:
            on_event(f"[结果] 命令已结束，退出码 {result['returncode']}")
        else:
            on_event(f"[结果] {'成功' if result.get('ok') else '失败：' + str(result.get('error') or result.get('note', ''))}")

    def execute(self):
        ctx, run = self.ctx, self.run
        budget, trace = ctx.budget, run.trace
        runnable = self.calls[:self.allowed] if not budget.time_low() else []
        parallel_results = self.run_parallel_reads(runnable) if _can_parallel_reads(runnable) else {}
        for index, call in enumerate(self.calls):
            run.local_tool_calls += 1
            action_id = self.action_id(index, call)
            prepared = index in parallel_results
            # 超出工具次数或剩余时间不足的调用不执行，仍回填结果保持配对。
            skip_tool = not prepared and (index >= self.allowed or budget.time_low())
            if prepared:
                result = parallel_results[index]
                if isinstance(result, BaseException):
                    raise result
            else:
                trace("tool_start", action_id=action_id, call_id=call.id, name=call.function.name,
                      arguments=call.function.arguments, skipped=skip_tool)
                self.announce(call)
                result = self.run_one(call, action_id, skip_tool)
                # 在上下文处理前保存真实结果，操作事实不随模型上下文丢失。
                trace("tool_result", action_id=action_id, call_id=call.id, name=call.function.name,
                      result=fact_result(result))
            run.edited.extend(path for path in edited_paths(call.function.name, result) if path not in run.edited)
            self.report(call, result)
            result, tool_message = self.fit_into_turn(call, result, len(self.calls) - index - 1)
            self.report_outcome(result, skip_tool)
            ctx.turn.append(tool_message)
