"""只读仓库 MCP 服务：stdio JSON-RPC，只暴露读取类工具。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..tools.registry import TOOL_REGISTRY, execute_tool
from ..base.version import project_version

DEFAULT_PROTOCOL = "2025-06-18"
SERVER_NAME = "recapcli-repo"


def _read_tools():
    return {
        name: spec for name, spec in TOOL_REGISTRY.items()
        if spec.get("capability", "read") == "read"
    }


def _result_text(result) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _tool_list(read_tools) -> list[dict]:
    return [
        {
            "name": name,
            "description": spec.get("description", ""),
            "inputSchema": spec["parameters"],
        }
        for name, spec in read_tools.items()
    ]


def _tool_result(payload) -> dict:
    return {"content": [{"type": "text", "text": _result_text(payload)}],
            "structuredContent": payload,
            "isError": not bool(payload.get("ok", False))}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def handle(message, workspace: Path, read_tools):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion")
        if not isinstance(version, str) or not version:
            version = DEFAULT_PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME,
                               "version": project_version() or "0.0.0"},
            },
        }
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"tools": _tool_list(read_tools)}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in read_tools:
            return _error(request_id, -32602, "未知的只读工具")
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments 必须是对象")
        try:
            result = execute_tool(name, arguments, workspace, capabilities=("read",), for_model=True)
        except Exception as error:  # 工具层错误转成 MCP 结果，不让服务崩溃
            result = {"ok": False, "error": str(error)}
        return {"jsonrpc": "2.0", "id": request_id,
                "result": _tool_result(result)}
    return _error(request_id, -32601, f"不支持的方法：{method}")


def serve(workspace: Path, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for stream in (stdin, stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    read_tools = _read_tools()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("请求必须是对象")
        except (ValueError, TypeError) as error:
            stdout.write(_result_text(_error(None, -32700, f"JSON 解析失败：{error}")) + "\n")
            stdout.flush()
            continue
        response = handle(message, workspace, read_tools)
        if response is not None:
            stdout.write(_result_text(response) + "\n")
            stdout.flush()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RecapCLI 只读仓库 MCP 服务")
    parser.add_argument("--workspace", default=".", help="允许读取的工作区目录")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"工作区不存在：{workspace}", file=sys.stderr)
        return 2
    return serve(workspace)


if __name__ == "__main__":
    raise SystemExit(main())
