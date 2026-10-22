"""模型工具定义、参数解析与本地工具分发。"""

from copy import deepcopy
import json
from pathlib import Path

from .commands import run_command
from .editing import edit_file, restore_file
from .filesystem import ToolError, find_files, list_directory, read_file, search_text

TOOL_REGISTRY = {
    "read_file": {
        "handler": read_file,
        "description": "读取 UTF-8 文本，最多 64 KiB；返回正文、实际起止行和 reference，引用代码时使用该位置。",
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
    "find_files": {
        "handler": find_files,
        "description": "按文件名子串或 glob 查找文件，如 agent、*.py；含 / 时匹配工作区相对路径。默认忽略大小写。",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "文件名/路径子串或 glob"},
            "path": {"type": "string", "description": "搜索起点，默认根目录"},
            "max_results": {"type": "integer", "description": "默认 50，最大 200"},
        }, "required": ["pattern"], "additionalProperties": False},
    },
    "edit_file": {
        "handler": edit_file,
        "capability": "write",
        "description": "先读取目标，再精确替换 old_text 为 new_text。默认要求唯一匹配；创建文件时 old_text 为空。返回 diff 和恢复用 backup_id。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "old_text": {"type": "string", "description": "文件中的精确原文；新文件用空字符串"},
            "new_text": {"type": "string", "description": "替换后的内容"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配，默认否"},
        }, "required": ["path", "old_text", "new_text"], "additionalProperties": False},
    },
    "restore_file": {
        "handler": restore_file,
        "capability": "write",
        "description": "按 edit_file 的 backup_id 撤销一次编辑；若文件已有后续修改则拒绝覆盖。撤销新建文件会删除该文件。",
        "parameters": {"type": "object", "properties": {
            "backup_id": {"type": "string", "description": "编辑结果中的备份 ID"},
        }, "required": ["backup_id"], "additionalProperties": False},
    },
    "run_command": {
        "handler": run_command,
        "capability": "exec",
        "timeout_parameter": "timeout_seconds",
        "description": "运行前台命令，argv 是可执行文件和参数数组。默认不经 shell，cwd 在工作区内；返回退出码、合并输出和执行记录 ID。",
        "parameters": {"type": "object", "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "description": "如 [python路径, -m, unittest]"},
            "cwd": {"type": "string", "description": "工作区内的相对运行目录，默认 ."},
            "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 30，最多 60"},
            "max_output_bytes": {"type": "integer", "description": "输出 UTF-8/原始编码字节上限，默认 16384，最多 65536"},
            "encoding": {"type": "string", "description": "输出解码，默认 utf-8，旧 Windows 程序可用 gb18030"},
        }, "required": ["argv"], "additionalProperties": False},
    },
}


def _spec_for(name):
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"未知工具：{name}")
    if not callable(spec.get("handler")):
        raise ToolError(f"工具未注册执行函数：{name}")
    return spec


def tool_definitions(capabilities=("read",)):
    """schema 与执行函数来自同一注册表；错误注册在请求模型前报错。"""
    return [{"type": "function", "function": {
        "name": name, "description": _spec_for(name)["description"],
        "parameters": deepcopy(spec["parameters"]),
    }} for name, spec in TOOL_REGISTRY.items() if spec.get("capability", "read") in capabilities]


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
            "array": isinstance(value, list) and all(isinstance(item, str) for item in value),
        }
        if not valid.get(kind, False):
            raise ToolError(f"{name} 类型必须是 {kind}")
    return params


def execute_tool(name, arguments, workspace, *, capabilities=("read",), time_remaining=None):
    """校验并执行工具；所有失败都转成结构化结果。"""
    try:
        spec = _spec_for(name)
        capability = spec.get("capability", "read")
        if capability not in capabilities:
            option = "--allow-write" if capability == "write" else "--allow-exec"
            raise ToolError(f"当前未开启 {name}，启动时使用 {option}")
        params = _parse_arguments(arguments, spec["parameters"])
        timeout = spec.get("timeout_parameter")
        if timeout and time_remaining is not None:
            params[timeout] = min(params.get(timeout, 30), time_remaining)
        result = spec["handler"](workspace=Path(workspace).resolve(), **params)
        return {"ok": True, **({"content": result} if isinstance(result, str) else result)}
    except (OSError, ValueError, TypeError) as error:
        return {"ok": False, "error": str(error)}
