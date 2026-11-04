"""模型自己维护的待办清单（todo_write）。

多入口需求最容易漏掉某个入口；让模型先列出清单、逐项勾选，结束前还有未完成项时提醒一次。
清单只存在于本轮，完整变化记录在任务事件里。
"""

import json

STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 20
MAX_ITEM_CHARS = 200
MARKS = {"pending": "☐", "in_progress": "◐", "completed": "☑"}

TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": (
            "维护本轮任务的待办清单。涉及多个文件、多个入口（存储、CLI、交互命令、帮助、测试）或三步以上的任务，"
            "先列出全部事项再动手；开始某项时标 in_progress，做完立即标 completed。每次提交完整清单，会整体替换旧清单。"
            "简单问答不需要清单。"),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": f"完整清单，最多 {MAX_ITEMS} 项",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "一句话描述这一项"},
                            "status": {"type": "string", "enum": list(STATUSES)},
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    },
}


class TodoList:
    def __init__(self):
        self.items = []
        self.reminded = False

    def update(self, arguments):
        """校验并整体替换清单；返回给模型的工具结果。"""
        try:
            data = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            return {"ok": False, "error": "参数必须是 JSON 对象"}
        todos = data.get("todos") if isinstance(data, dict) else None
        if not isinstance(todos, list) or len(todos) > MAX_ITEMS:
            return {"ok": False, "error": f"todos 必须是最多 {MAX_ITEMS} 项的数组"}
        items = []
        for entry in todos:
            if (not isinstance(entry, dict) or entry.get("status") not in STATUSES
                    or not isinstance(entry.get("content"), str) or not entry["content"].strip()):
                return {"ok": False, "error": "每项需要非空 content 和 status（pending / in_progress / completed）"}
            items.append({"content": entry["content"].strip()[:MAX_ITEM_CHARS], "status": entry["status"]})
        self.items = items
        done = sum(item["status"] == "completed" for item in items)
        return {"ok": True, "summary": f"待办 {done}/{len(items)} 已完成"}

    def open_items(self):
        return [item for item in self.items if item["status"] != "completed"]

    def render(self):
        return "\n".join(f"{MARKS[item['status']]} {item['content']}" for item in self.items)

    def context_note(self):
        """每次请求都附在系统提示后，旧的 todo_write 结果被移出上下文也不会丢清单。"""
        return "当前待办清单（todo_write 维护）：\n" + self.render() if self.items else ""

    def reminder(self):
        """模型准备结束但仍有未完成项时提醒一次；第二次照常结束，避免死循环。"""
        pending = self.open_items()
        if not pending or self.reminded:
            return None
        self.reminded = True
        lines = "\n".join(f"- {item['content']}" for item in pending)
        return ("待办清单里还有未完成项：\n" + lines + "\n"
                "请继续完成并更新清单；确实无法完成的，在最终答复里逐项说明原因。")
