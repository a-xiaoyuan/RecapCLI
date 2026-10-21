"""对外提供模型工具定义与统一执行入口。"""

from .registry import execute_tool, tool_definitions

__all__ = ["execute_tool", "tool_definitions"]
