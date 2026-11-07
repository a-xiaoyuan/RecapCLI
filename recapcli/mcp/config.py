"""MCP 服务配置文件解析。"""

from __future__ import annotations

import json
import math
from pathlib import Path
import shlex

from .http_client import parse_mcp_url


def load_mcp_config(path, *, workspace="."):
    """读取显式指定的配置，返回 (name, config) 列表。"""
    workspace = Path(workspace).resolve()
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = workspace / path
    if not path.is_file():
        raise ValueError(f"MCP 配置文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"MCP 配置读取失败：{error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
        raise ValueError("MCP 配置必须包含 servers 对象")
    result = []
    for name, raw in data["servers"].items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("MCP 服务名必须是非空字符串")
        if not isinstance(raw, dict):
            raise ValueError(f"MCP 服务 {name} 的配置必须是对象")
        if ("command" in raw) == ("url" in raw):
            raise ValueError(f"MCP 服务 {name} 必须在 command 和 url 中选填一项")
        transport = _http_config(name, raw) if "url" in raw else _stdio_config(name, raw)
        timeout = raw.get("timeout", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < math.inf:
            raise ValueError(f"MCP 服务 {name} 的 timeout 必须是正数")
        max_result_bytes = raw.get("max_result_bytes", 64 * 1024)
        if (isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int)
                or max_result_bytes < 1):
            raise ValueError(f"MCP 服务 {name} 的 max_result_bytes 必须是正整数")
        permissions = raw.get("tool_permissions", {})
        if not isinstance(permissions, dict) or any(
                not isinstance(tool, str) or not isinstance(permission, str)
                or permission not in {"read", "write", "exec"}
                for tool, permission in permissions.items()):
            raise ValueError(f"MCP 服务 {name} 的 tool_permissions 必须把工具名映射到 read、write 或 exec")
        result.append((name.strip(), {
            **transport,
            "timeout": float(timeout),
            "max_result_bytes": max_result_bytes,
            "tool_permissions": permissions,
            "prefix": str(raw.get("prefix") or name),
            "config_dir": path.parent,
        }))
    return result


def _stdio_config(name, raw):
    command = raw["command"]
    if isinstance(command, str):
        command = shlex.split(command, posix=False)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError(f"MCP 服务 {name} 的 command 必须是非空参数数组")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError(f"MCP 服务 {name} 的 cwd 必须是字符串")
    env = raw.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError(f"MCP 服务 {name} 的 env 必须是对象")
    if "bearer_token_env" in raw:
        raise ValueError(f"MCP 服务 {name} 的 bearer_token_env 只适用于 url")
    return {"command": command, "cwd": cwd, "env": {str(key): str(value) for key, value in (env or {}).items()}}


def _http_config(name, raw):
    try:
        parse_mcp_url(raw["url"])
    except ValueError as error:
        raise ValueError(f"MCP 服务 {name} 的 {error}") from error
    if "cwd" in raw or "env" in raw:
        raise ValueError(f"MCP 服务 {name} 的 cwd 和 env 只适用于 command")
    token_env = raw.get("bearer_token_env")
    if "bearer_token_env" in raw and (not isinstance(token_env, str) or not token_env.isidentifier()):
        raise ValueError(f"MCP 服务 {name} 的 bearer_token_env 必须是环境变量名")
    return {"url": raw["url"], "bearer_token_env": token_env}
