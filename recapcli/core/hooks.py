"""用户在启动时显式指定的 Hooks：工具前后和结束时运行本地命令。

退出码 0 放行，2 阻止并把输出交回模型，其他情况只提示、不阻止。
配置文件要用 --hooks 指定，不会自动读取项目里的文件。
"""

import json
from pathlib import Path
import re

from ..tools.commands import run_command
from ..tools.filesystem import ToolError

EVENTS = ("pre_tool", "post_tool", "stop")
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 600
MAX_STOP_BLOCKS = 3
BLOCK_EXIT_CODE = 2
FEEDBACK_CHARS = 4000


def _parse_hook(event, item):
    if not isinstance(item, dict) or set(item) - {"command", "match", "timeout"}:
        raise ValueError(f"{event} 的每一项只能包含 command、match、timeout")
    command = item.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
        raise ValueError(f"{event} 的 command 必须是非空字符串数组")
    timeout = item.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"{event} 的 timeout 必须大于 0 且不超过 {MAX_TIMEOUT_SECONDS} 秒")
    match = item.get("match")
    if match is not None and event == "stop":
        raise ValueError("stop 不支持 match")
    try:
        pattern = re.compile(match) if match is not None else None
    except (re.error, TypeError) as error:
        raise ValueError(f"{event} 的 match 不是有效的正则：{match}") from error
    return {"command": command, "pattern": pattern, "timeout": timeout}


class HookSet:
    def __init__(self, hooks):
        self.hooks = hooks

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"--hooks 无法读取：{error}") from error
        if not isinstance(data, dict) or set(data) - set(EVENTS):
            raise ValueError("--hooks 配置只能包含 " + "、".join(EVENTS))
        hooks = {}
        for event in EVENTS:
            items = data.get(event, [])
            if not isinstance(items, list):
                raise ValueError(f"--hooks 的 {event} 必须是数组")
            try:
                hooks[event] = [_parse_hook(event, item) for item in items]
            except ValueError as error:
                raise ValueError(f"--hooks 配置错误：{error}") from error
        return cls(hooks)

    def has(self, event):
        return bool(self.hooks[event])

    def run(self, run, event, payload, tool=None):
        """依次运行匹配的 Hook；有一个阻止就停下并返回原因，否则返回 None。"""
        ctx = run.ctx
        text = json.dumps({"event": event, "workspace": str(ctx.workspace), "tool": tool, **payload}, ensure_ascii=False)
        for hook in self.hooks[event]:
            if hook["pattern"] is not None and not hook["pattern"].fullmatch(tool or ""):
                continue
            label = " ".join(hook["command"])
            timeout = max(1.0, min(hook["timeout"], ctx.budget.working_time()))
            try:
                result = run_command(ctx.workspace, list(hook["command"]), timeout_seconds=timeout, stdin_text=text,
                                     check=ctx.budget.remaining_time)
            except ToolError as error:
                result = {"status": "failed", "returncode": None, "output": str(error), "record_id": None}
            if result["record_id"]:
                run.end_checks.changed()
            blocked = result["status"] == "finished" and result["returncode"] == BLOCK_EXIT_CODE
            run.trace("hook", event=event, tool=tool, command=hook["command"], blocked=blocked, status=result["status"],
                      returncode=result["returncode"], record_id=result["record_id"])
            if blocked:
                reason = result["output"].strip()[-FEEDBACK_CHARS:] or "Hook 未说明原因"
                ctx.on_event(f"[Hook] {event} 已阻止：{label}")
                return reason
            if result["status"] != "finished" or result["returncode"] != 0:
                ctx.on_event(f"[Hook] {event} 未正常结束，已忽略：{label}（{result['status']}，退出码 {result['returncode']}）")
        return None
