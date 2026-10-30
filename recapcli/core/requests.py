"""模型请求的有限重试策略；工具执行不进入重试范围。"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math

from openai import APIConnectionError, APIStatusError


def retry_delay(error, attempt):
    if isinstance(error, APIStatusError):
        if error.status_code not in {408, 429} and error.status_code < 500:
            return None
        if getattr(error, "code", None) in {"insufficient_quota", "organization_usage_limit_exceeded",
                                           "billing_hard_limit_reached"}:
            return None
        headers = error.response.headers
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1)):
            value = headers.get(name)
            if value is None:
                continue
            try:
                delay = float(value) * scale
            except ValueError:
                if name != "retry-after":
                    continue
                try:
                    target = parsedate_to_datetime(value)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=timezone.utc)
                    delay = (target - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    continue
            if math.isfinite(delay) and delay >= 0:
                return delay
    elif not isinstance(error, APIConnectionError):
        return None
    return min(0.5 * 2 ** attempt, 4.0)
