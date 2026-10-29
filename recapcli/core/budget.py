"""同一任务的线程安全预算与协作取消；不把字节预算当作 token/费用预算。"""

import json
from threading import Event, RLock
import time

from .context import json_size


OUTPUT_LIMIT_RESULT = {"ok": False, "error": "本轮工具输出预算不足，请缩小范围后重试。"}


class AgentCancelled(KeyboardInterrupt):
    """沿用入口的取消终态，不能当作普通子代理失败吞掉。"""


class AgentBudget:
    def __init__(self, limits, max_requests, *, cancel_event=None, clock=time.monotonic):
        self.limits = limits
        self.max_requests = max_requests
        self.cancel_event = cancel_event if cancel_event is not None else Event()
        self.clock = clock
        self.deadline = clock() + limits.max_seconds
        self.lock = RLock()
        self.requests = 0
        self.tool_calls = 0
        self.tool_results = 0
        self.output_bytes = 0
        self.output_exhausted = False

    def remaining_time(self):
        if self.cancel_event.is_set():
            raise AgentCancelled("任务已取消，停止后续模型请求和工具调度")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RuntimeError("本轮执行时间预算已耗尽")
        return remaining

    def request(self, reserve=0):
        with self.lock:
            self.remaining_time()
            if self.requests >= self.max_requests - reserve:
                raise RuntimeError("共享模型请求预算已耗尽")
            self.requests += 1
            return self.requests

    def should_summarize(self, reserve=0):
        with self.lock:
            return (self.requests >= self.max_requests - reserve - 1
                    or self.tool_calls >= self.limits.max_tool_calls or self.output_exhausted)

    def reserve_tools(self, count):
        with self.lock:
            self.remaining_time()
            if count > self.limits.max_tool_calls - self.tool_calls:
                raise RuntimeError("模型请求的工具数量超过本轮剩余预算，未执行这批调用")
            self.tool_calls += count

    def encode_result(self, result, fits_context):
        """全局预留未返回调用的错误正文；大结果与上下文溢出共用停止规则。"""
        with self.lock:
            reserve = (self.limits.max_tool_calls - self.tool_results - 1) * json_size(OUTPUT_LIMIT_RESULT)
            if json_size(result) > self.limits.max_tool_bytes - self.output_bytes - reserve:
                result = OUTPUT_LIMIT_RESULT
                self.output_exhausted = True
            content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if not fits_context(content):
                result = OUTPUT_LIMIT_RESULT
                content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                self.output_exhausted = True
            self.tool_results += 1
            self.output_bytes += len(content.encode("utf-8"))
            return result, content

    def snapshot(self):
        with self.lock:
            return {"model_requests": self.requests, "tool_calls": self.tool_calls,
                    "tool_bytes": self.output_bytes, "output_exhausted": self.output_exhausted}
