"""模型工具定义、参数解析与本地工具分发。"""

from copy import deepcopy
import json
from pathlib import Path

from .filesystem import ToolError, list_directory, read_file, search_text

TOOL_REGISTRY = {
    "read_file": {
        "handler": read_file,
        "description": "读取工作区内的 UTF-8 文本，单次返回最多 64 KiB；可按行读取大文件片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "start_line": {"type": "integer", "description": "起始行号，从 1 开始"},
                "end_line": {"type": "integer", "description": "结束行号，包含该行"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "search_text": {
        "handler": search_text,
        "description": "在工作区文本文件中搜索字符串，返回相对路径、行号和该行内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜索的文本"},
                "path": {"type": "string", "description": "搜索起点，默认工作区根目录"},
                "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认否"},
                "max_results": {"type": "integer", "description": "最多返回条数，默认 50"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "list_directory": {
        "handler": list_directory,
        "description": "查看工作区目录结构，返回目录与文件的树形列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的目录，默认根目录"},
                "max_depth": {"type": "integer", "description": "递归深度，默认 2，最大 5"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}


def _spec_for(name):
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"未知工具：{name}")
    if not callable(spec.get("handler")):
        raise ToolError(f"工具未注册执行函数：{name}")
    return spec


def tool_definitions():
    """schema 与执行函数来自同一注册表；错误注册在请求模型前报错。"""
    return [{"type": "function", "function": {
        "name": name, "description": _spec_for(name)["description"],
        "parameters": deepcopy(spec["parameters"]),
    }} for name, spec in TOOL_REGISTRY.items()]


def _parse_arguments(arguments, schema):
    if isinstance(arguments, str):
        try:
            params = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise ToolError(f"参数不是合法 JSON：{error.msg}") from error
    else:
        params = arguments
    if not isinstance(params, dict):
        raise ToolError("工具参数必须是 JSON 对象")
    unknown = set(params) - set(schema["properties"])
    if unknown:
        raise ToolError(f"不支持的参数：{', '.join(sorted(unknown))}")
    missing = set(schema.get("required", [])) - set(params)
    if missing:
        raise ToolError(f"缺少参数：{', '.join(sorted(missing))}")
    for name, value in params.items():
        kind = schema["properties"][name]["type"]
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if not valid.get(kind, False):
            raise ToolError(f"{name} 类型必须是 {kind}")
    return params


def execute_tool(name, arguments, workspace):
    """校验并执行工具；所有失败都转成结构化结果。"""
    try:
        spec = _spec_for(name)
        params = _parse_arguments(arguments, spec["parameters"])
        result = spec["handler"](workspace=Path(workspace).resolve(), **params)
        return {"ok": True, **({"content": result} if isinstance(result, str) else result)}
    except (OSError, ValueError, TypeError) as error:
        return {"ok": False, "error": str(error)}
