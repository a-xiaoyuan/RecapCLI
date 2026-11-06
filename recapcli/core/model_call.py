"""一次模型请求：预算检查、流式转发、有限重试和事实记录。"""

from openai import APIError

from ..llm.client import ModelClient, request_completion
from ..llm.protocols import ModelResponseError
from ..llm.streaming import IncompleteStreamError
from .budget import BudgetExhausted
from .context import json_tokens
from .requests import retry_delay

# 历史压缩时要留给后续执行的模型请求数。
COMPACT_RESERVE = {"plan": 4, "multi": 3}
PHASE_LABELS = {"plan": "规划", "execute": "执行", "review": "核对", "summary": "汇总", "compact": "历史压缩"}


def request_model(run, messages, phase, *, tools=None, tools_allowed=False, history_messages=0, final=False):
    """请求模型并返回响应；失败和重试也占用请求预算。"""
    ctx = run.ctx
    client, limits, budget = ctx.client, ctx.limits, ctx.budget
    requests_used = run.requests_used
    estimate = json_tokens(messages) + (json_tokens(tools) if tools else 0)
    for attempt in range(limits.max_retries + 1):
        if requests_used >= ctx.max_steps:
            raise BudgetExhausted("模型请求预算已耗尽")
        # 收尾请求可以用掉预留时间；普通请求不行。
        available = budget.remaining_time() if final else budget.working_time()
        if available <= 1:
            raise BudgetExhausted("本轮执行时间预算即将耗尽")
        request_index = budget.request(ctx.request_reserve)
        requests_used += 1
        run.requests_used = requests_used
        model_config = client.metadata() if isinstance(client, ModelClient) else {"protocol": "chat_completions", "model": ctx.model}
        run.trace("model_request", index=request_index, phase=phase, model_config=model_config,
                  history_messages=history_messages, context_tokens=int(estimate * run.meter.ratio), tools_allowed=tools_allowed,
                  messages=messages, tools=[item["function"]["name"] for item in (tools or [])])
        phase_label = PHASE_LABELS.get(phase, "")
        ctx.on_event(f"[模型] 第 {request_index} 次请求" + (f"（{phase_label}）" if phase_label else ""))
        visible_stream = (ctx.on_stream is not None and phase in {"react", "summary"}
                          and ctx.role not in {"analysis", "review"}
                          and (not isinstance(client, ModelClient) or client.metadata()["capabilities"]["streaming"]))
        emitted = False

        def delta(text):
            nonlocal emitted
            budget.remaining_time()
            emitted = emitted or bool(text)
            ctx.on_stream("delta", text)

        if visible_stream:
            ctx.on_stream("start", "")
        try:
            response = request_completion(client, model=ctx.model, messages=messages, tools=tools,
                                          tools_allowed=tools_allowed, timeout=min(limits.request_timeout, available),
                                          context_tokens=limits.max_context_tokens, workspace=ctx.workspace,
                                          on_delta=delta if visible_stream else None, check=budget.remaining_time)
        except (ModelResponseError, IncompleteStreamError) as error:
            run.trace("model_response", response=error.response, usage=error.usage)
            if visible_stream:
                ctx.on_stream("abort", "")
            raise
        except APIError as error:
            if visible_stream:
                ctx.on_stream("abort", "")
            delay = retry_delay(error, attempt)
            reserve = COMPACT_RESERVE.get(ctx.mode, 1) if phase == "compact" else int(tools_allowed)
            if (delay is None or emitted or attempt >= limits.max_retries
                    or requests_used >= ctx.max_steps - reserve
                    or budget.requests >= budget.max_requests - ctx.request_reserve
                    or delay >= budget.working_time()):
                raise
            run.trace("model_retry", index=request_index, error_type=type(error).__name__,
                      status_code=getattr(error, "status_code", None), delay_seconds=delay)
            ctx.on_event(f"[重试] 模型请求暂时失败（{type(error).__name__}），{delay:g} 秒后重试；不会重放工具。")
            budget.cancel_event.wait(delay)
            budget.remaining_time()
            continue
        except BaseException:
            if visible_stream:
                ctx.on_stream("abort", "")
            raise
        usage = response.usage.model_dump(exclude_none=True) if response.usage else None
        run.trace("model_response", response=response.model_dump(exclude_none=True), usage=usage)
        run.meter.observe(estimate, usage)
        # 响应已经到手就使用它；超时与否交给下一轮的预算判断，不丢弃已付费的回答。
        if visible_stream:
            ctx.on_stream("end", "")
        return response
