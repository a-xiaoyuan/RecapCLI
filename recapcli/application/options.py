"""启动参数的共用校验，行式 CLI 和 TUI 都走这里。"""

import math
import os
import shlex

from ..core.agent import AgentLimits, DEFAULT_STEPS
from ..core.hooks import HookSet


def check_approval_timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 3600):
        raise ValueError("--approval-timeout 必须是大于 0 且不超过 3600 的秒数")


def capabilities_from(args):
    requested = {"read"}
    if args.allow_web:
        requested.add("web")
    if args.allow_write:
        requested.add("write")
    if args.allow_exec:
        requested.add("exec")
    return {"read"} if args.mode == "multi" and not requested & {"write", "exec"} else requested


def max_steps_from(args):
    max_steps = DEFAULT_STEPS[args.mode] if args.max_steps is None else args.max_steps
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        raise ValueError("--max-steps 必须大于 0")
    if args.mode == "plan" and max_steps < 4 and not args.compact:
        raise ValueError("计划模式至少需要 4 次模型请求预算")
    if args.mode == "multi" and max_steps < 3 and not args.compact:
        raise ValueError("Multi 至少需要 3 次模型请求预算")
    return max_steps


def verify_command_from(args):
    """把 --verify-cmd 拆成 argv；命令不经过 shell，所以管道和 && 不生效。"""
    if args.verify_cmd is None:
        return None
    try:
        argv = shlex.split(args.verify_cmd, posix=os.name != "nt")
    except ValueError as error:
        raise ValueError(f"--verify-cmd 无法解析：{error}") from error
    if os.name == "nt":
        if args.verify_cmd.count('"') % 2:
            raise ValueError("--verify-cmd 无法解析：引号没有闭合")
        argv = [item.strip('"') for item in argv]
    if not argv:
        raise ValueError("--verify-cmd 不能为空")
    return argv


def hooks_from(args):
    if args.hooks is None:
        return None
    if args.mode == "multi":
        raise ValueError("--hooks 暂不支持 multi 模式")
    return HookSet.load(args.hooks)


def limits_from(args):
    return AgentLimits(
        max_context_tokens=args.context_tokens or AgentLimits.max_context_tokens,
        max_tool_calls=args.max_tool_calls, max_seconds=args.max_seconds,
        request_timeout=args.request_timeout, max_retries=args.max_retries,
        max_plan_steps=args.plan_steps, max_replans=args.plan_replans,
    )
