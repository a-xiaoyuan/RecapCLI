"""同一任务的线程安全预算与协作取消。

预算耗尽不是故障：调用方据此转入最后一次不带工具的汇总请求，而不是抛错丢掉已完成的工作。
"""

import json
from threading import Event, RLock
import time


SKIPPED_RESULT = {"ok": False, "execution_status": "not_executed",
                  "error": "本轮预算即将用完，该调用未执行；请基于已有结果收尾。"}


class AgentCancelled(KeyboardInterrupt):
    """沿用入口的取消终态，不能当作普通子代理失败吞掉。"""


class BudgetExhausted(RuntimeError):
    """请求次数、工具次数、时间或上下文用完；Agent 会转入收尾而不是失败。"""


class AgentBudget:
    def __init__(self, limits, max_requests, *, cancel_event=None, clock=time.monotonic):
        self.limits = limits
        self.max_requests = max_requests
        self.cancel_event = cancel_event if cancel_event is not None else Event()
        self.clock = clock
        self.deadline = clock() + limits.max_seconds
        # 给最后一次汇总请求留出时间；命令和普通请求都不能吃掉这段时间。
        self.finish_reserve = min(60.0, limits.max_seconds * 0.15)
        self.lock = RLock()
        self.requests = 0
        self.tool_calls = 0
        self.output_bytes = 0

    def remaining_time(self):
        if self.cancel_event.is_set():
            raise AgentCancelled("任务已取消，停止后续模型请求和工具调度")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise BudgetExhausted("本轮执行时间预算已耗尽")
        return remaining

    def working_time(self):
        """普通请求与工具可用的时间：总剩余减去收尾预留。"""
        return self.remaining_time() - self.finish_reserve

    def time_low(self):
        return self.working_time() <= 1

    def request(self, reserve=0):
        with self.lock:
            self.remaining_time()
            if self.requests >= self.max_requests - reserve:
                raise BudgetExhausted("共享模型请求预算已耗尽")
            self.requests += 1
            return self.requests

    def should_summarize(self, reserve=0):
        with self.lock:
            return (self.requests >= self.max_requests - reserve - 1
                    or self.tool_calls >= self.limits.max_tool_calls or self.time_low())

    def reserve_tools(self, count):
        """返回本批可执行的调用数；超出部分由调用方标记为未执行。"""
        with self.lock:
            self.remaining_time()
            allowed = max(0, min(count, self.limits.max_tool_calls - self.tool_calls))
            self.tool_calls += allowed
            return allowed

    def encode_result(self, result):
        with self.lock:
            content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            self.output_bytes += len(content.encode("utf-8"))
            return content

    def snapshot(self):
        with self.lock:
            return {"model_requests": self.requests, "tool_calls": self.tool_calls,
                    "tool_bytes": self.output_bytes}
