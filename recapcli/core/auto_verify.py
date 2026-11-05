"""运行用户配置的验证命令。命令由用户在启动时指定，所以不再走逐次审批。"""

from ..tools.commands import run_command
from ..tools.filesystem import ToolError

MAX_VERIFY_ROUNDS = 3
VERIFY_TIMEOUT_SECONDS = 300
OUTPUT_TAIL_CHARS = 8000


def run_verification(run):
    """运行一次验证，记录事实，返回 (是否通过, 交给模型的说明)。"""
    ctx = run.ctx
    label = " ".join(ctx.verify_command)
    ctx.on_event(f"[验证] 运行 {label}")
    timeout = max(1.0, min(VERIFY_TIMEOUT_SECONDS, ctx.budget.working_time()))
    try:
        result = run_command(ctx.workspace, list(ctx.verify_command), timeout_seconds=timeout,
                             max_output_bytes=65536, check=ctx.budget.remaining_time)
    except ToolError as error:
        result = {"success": False, "status": "failed", "returncode": None, "output": str(error), "record_id": None}
    success = bool(result["success"])
    run.trace("auto_verify", command=list(ctx.verify_command), success=success, status=result["status"],
              returncode=result["returncode"], record_id=result["record_id"])
    if success:
        ctx.on_event("[验证] 通过")
        return True, ""
    ctx.on_event(f"[验证] 失败：{result['status']}，退出码 {result['returncode']}")
    output = result["output"][-OUTPUT_TAIL_CHARS:]
    return False, (f"自动验证失败：`{label}` 状态 {result['status']}，退出码 {result['returncode']}。"
                   f"请根据输出继续修复，修复后再结束；确实无法修复时说明原因。\n输出末尾：\n{output}")
