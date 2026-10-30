"""从本轮已记录的事实构造可继续的历史；不重新请求模型或执行工具。"""

from copy import deepcopy
import json

from .context import json_size


class TurnRecovery:
    def __init__(self, max_tool_bytes):
        self.messages = []
        self.pending = {}
        self.has_operations = False
        self.remaining_bytes = max_tool_bytes

    def observe(self, kind, data):
        # 子代理有独立上下文；不能混入父任务或其他角色的工具调用。
        if data.get("agent"):
            return
        if kind == "model_request" and data.get("phase") != "compact" and not self.messages:
            user = next((message for message in reversed(data["messages"]) if message["role"] == "user"), None)
            if user is not None:
                self.messages.append(deepcopy(user))
        elif kind == "message" and self.messages:
            message = deepcopy(data["message"])
            self.messages.append(message)
            self.pending = {}
            for call in message.get("tool_calls", []):
                result = {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({
                    "ok": False, "execution_status": "not_executed", "error": "本轮中断，该调用尚未执行。",
                }, ensure_ascii=False)}
                self.pending[call["id"]] = result
                self.messages.append(result)
        elif kind in {"tool_start", "tool_result"} and data["call_id"] in self.pending:
            self.has_operations = True
            if kind == "tool_start":
                result = {"ok": False, "execution_status": "unknown",
                          "error": "调用已开始但尚无完成记录；核对文件、备份或命令记录，禁止自动重放。"}
            else:
                result = data["result"]
                size = json_size(result)
                if size > self.remaining_bytes:
                    # 保留操作收据和退出码，省略超预算正文；完整结果仍在任务追溯中。
                    fields = ("ok", "error", "summary", "path", "reference", "backup_id", "command_id", "record_id",
                              "returncode", "status", "sha256", "before_hash", "after_hash")
                    result = {key: result[key] for key in fields if key in result}
                    result.update(truncated=True, recovery_note="正文超过恢复上下文预算，完整事实见本任务记录。")
                self.remaining_bytes = max(0, self.remaining_bytes - json_size(result))
            self.pending[data["call_id"]]["content"] = json.dumps(result, ensure_ascii=False)

    def finish(self, task_id):
        if not self.has_operations:
            return []
        note = (f"本轮已中断，任务 ID：{task_id}，任务未完成。已保留工具结果；"
                "未收到完成记录的调用标为 unknown，未调度的调用标为 not_executed。"
                "这些记录不代表任务成功。继续前先核对已有文件和操作收据，不得因请求失败自动重复编辑或命令。")
        return [*deepcopy(self.messages), {"role": "assistant", "content": note}]
