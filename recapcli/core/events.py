"""给界面的结构化事件。

UiEvent 本身就是 str，内容与原来的中文事件文本逐字相同，所以只认字符串的消费者
（print、行式 CLI、现有测试）不受影响；需要结构的消费者（TUI 卡片、桌面端）
读 kind / data / agent，或用 to_json() 得到线上格式。
"""

import json


class UiEvent(str):
    kind: str
    data: dict
    agent: str

    def __new__(cls, kind, text, data=None, agent=""):
        event = super().__new__(cls, text)
        event.kind = kind
        event.data = dict(data or {})
        event.agent = agent
        return event

    def with_agent(self, role):
        """子代理转发时加 [角色] 前缀，同时记录角色。"""
        return UiEvent(self.kind, f"[{role}] {self}", self.data, role)

    def to_json(self):
        return json.dumps({"kind": self.kind, "agent": self.agent, "text": str(self), "data": self.data},
                          ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw):
        item = json.loads(raw)
        return cls(item["kind"], item["text"], item.get("data"), item.get("agent", ""))


def tag_role(text, role):
    """team / subagents 用：保留结构化信息的同时加角色前缀。"""
    if isinstance(text, UiEvent):
        return text.with_agent(role)
    return f"[{role}] {text}"


def tool_start(name, location=""):
    return UiEvent("tool_start", f"[工具] {name}" + (f" · {location}" if location else ""),
                   {"name": name, "location": location})


def operation(summary):
    return UiEvent("operation", f"[操作] {summary}", {"summary": summary})


def diff(text):
    return UiEvent("diff", "[差异]\n" + text, {"diff": text})


def command_output(text):
    return UiEvent("command_output", "[命令输出]\n" + text, {"output": text})


def todo(rendered, items):
    return UiEvent("todo", "[待办]\n" + rendered, {"items": items})


def approval_notice(message):
    return UiEvent("approval_notice", f"[审批] {message}", {"message": str(message)})


def tool_result(text, *, ok, returncode=None, skipped=False, error=""):
    return UiEvent("tool_result", text, {"ok": ok, "returncode": returncode, "skipped": skipped, "error": error})


def notice(text):
    return UiEvent("notice", text)
