"""MCP 工具适配：本地权限、审批与返回格式。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from .client import StdioMCPClient
from ..tools.approval import authorize_action, fail_closed

PREFIX = "mcp_repo_"
DEFAULT_MAX_RESULT_BYTES = 64 * 1024


def safe_prefix(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    return cleaned or "server"


def _truncate_text(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n[内容已按 MCP 返回大小限制截断]"
    room = max(0, max_bytes - len(suffix.encode("utf-8")))
    return encoded[:room].decode("utf-8", "ignore") + suffix


class MCPToolProvider:
    """内置服务只读；外部工具权限由本地配置确定，不采用服务端注解授权。"""

    def __init__(self, workspace=None, *, command=None, cwd=None, prefix="repo",
                 timeout=10.0, max_result_bytes=DEFAULT_MAX_RESULT_BYTES, env=None,
                 tool_permissions=None):
        builtin = command is None
        if command is None:
            if workspace is None:
                raise ValueError("MCP 工具需要一个工作区或启动命令")
            command = [sys.executable, "-m", "recapcli.mcp.repo_server",
                       "--workspace", str(Path(workspace).resolve())]
            workspace = Path(workspace).resolve()
        if isinstance(command, str):
            raise ValueError("MCP 启动命令必须是参数数组")
        if not command:
            raise ValueError("MCP 启动命令不能为空")
        if timeout <= 0:
            raise ValueError("MCP 超时必须是正数")
        if isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int) or max_result_bytes < 1:
            raise ValueError("MCP 返回大小必须是正整数")
        self.workspace = Path(workspace or cwd or Path.cwd()).resolve()
        self._default_permission = "read" if builtin else "exec"
        self._tool_permissions = dict(tool_permissions or {})
        self.tool_prefix = "mcp_" + safe_prefix(prefix) + "_"
        self.max_result_bytes = max_result_bytes
        self._client = StdioMCPClient(list(command), cwd=str(cwd or Path(__file__).resolve().parents[2]),
                                      timeout=float(timeout), env=env)
        self._tools = {}
        try:
            self._client.initialize()
            tools = self._client.list_tools()
        except BaseException:
            self._client.close()
            raise
        for tool in tools:
            name = str(tool.get("name", ""))
            if not name or name in self._tools:
                continue
            self._tools[name] = tool
        self.definitions = tuple(self._definition(name, tool) for name, tool in self._tools.items())

    def _definition(self, name, tool):
        return {
            "type": "function",
            "function": {
                "name": self.tool_prefix + name,
                "description": str(tool.get("description", "")) +
                               f"（本地权限：{self._tool_permissions.get(name, self._default_permission)}）",
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            },
        }

    def owns(self, name):
        return name.startswith(self.tool_prefix)

    def _limit(self, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) <= self.max_result_bytes:
            return payload
        content = payload.get("content")
        if isinstance(content, str):
            bounded = dict(payload)
            bounded["content"] = _truncate_text(content, max(1, self.max_result_bytes - 512))
            bounded["truncated"] = True
            bounded["limit_reason"] = "MCP 返回超过大小限制"
            bounded_raw = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(bounded_raw) <= self.max_result_bytes:
                return bounded
            return {"ok": False, "error": "MCP 返回超过大小限制，且截断后仍超限"}
        return {"ok": False, "error": "MCP 返回超过大小限制，且无法安全截断"}

    def call(self, name, arguments, *, capabilities=("read",), approver=None,
             on_approval=None, time_remaining=None, check=None):
        if not self.owns(name):
            raise ValueError("不是当前 MCP 服务的工具")
        remote_name = name[len(self.tool_prefix):]
        if remote_name not in self._tools:
            raise ValueError("未知的 MCP 工具")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError as error:
                raise ValueError("MCP 工具参数不是合法 JSON") from error
        if not isinstance(arguments, dict):
            raise ValueError("MCP 工具参数必须是对象")
        permission = self._tool_permissions.get(remote_name, self._default_permission)
        if permission not in capabilities:
            option = {"write": "--allow-write", "exec": "--allow-exec"}.get(permission, "读取权限")
            return {"ok": False, "error": f"当前未开启 {name}，需要 {option}"}
        if permission in {"write", "exec"}:
            denial = authorize_action(name, self.workspace, arguments, approver=approver or fail_closed,
                                      on_approval=on_approval, time_remaining=time_remaining)
            if denial is not None:
                return denial
        if check is not None:
            time_remaining = check()
        result = self._client.call_tool(remote_name, arguments, timeout=time_remaining)
        payload = result.get("structuredContent")
        payload = dict(payload) if isinstance(payload, dict) else {"content": result.get("content", [])}
        payload["ok"] = not bool(result.get("isError")) and bool(payload.get("ok", True))
        if not payload["ok"]:
            text = "\n".join(item.get("text", "") for item in result.get("content", [])
                             if item.get("type") == "text")
            payload["error"] = str(payload.get("error") or text or "MCP 工具执行失败")
        return self._limit(payload)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
