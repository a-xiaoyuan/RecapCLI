"""从实际任务记录提取状态栏数据，不估算费用或伪造上下文占比。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskStatus:
    task_id: str = ""
    status: str = ""
    requests: int = 0
    duration: float | None = None
    usage: dict | None = None
    modified_files: tuple = ()
    stop_reason: str = ""

    @classmethod
    def from_task(cls, task):
        paths = []
        for event in task.get("events", []):
            if event.get("kind") != "tool_result":
                continue
            data = event.get("data", {})
            result = data.get("result", {})
            if (data.get("name") in {"edit_file", "restore_file"} and result.get("ok")
                    and (result.get("changed") or result.get("restored"))):
                path = result.get("path")
                if isinstance(path, str) and path not in paths:
                    paths.append(path)
        return cls(task_id=task.get("id", ""), status=task.get("status", ""),
                   requests=task.get("model_requests", 0), duration=task.get("duration_seconds"),
                   usage=task.get("usage"), modified_files=tuple(paths), stop_reason=task.get("stop_reason", ""))


def token_count(value):
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "未知"


def usage_text(task):
    if not task.task_id:
        return "—\n还没有任务用量"
    if not isinstance(task.usage, dict):
        return "用量未知\n服务尚未返回完整统计"
    return "\n".join(("输入  " + token_count(task.usage.get("prompt_tokens")),
                      "输出  " + token_count(task.usage.get("completion_tokens")),
                      "合计  " + token_count(task.usage.get("total_tokens"))))


def task_text(task, *, busy=False, approval=False):
    if approval:
        return "等待审批"
    if busy:
        return "处理中…"
    labels = {"returned": "已回答", "incomplete": "未完成", "failed": "失败", "cancelled": "已取消",
              "blocked": "受阻", "unknown": "待核对", "running": "未获得终态"}
    text = labels.get(task.status, "就绪")
    if task.duration is not None:
        text += f" · {task.duration:.1f}s"
    return text
