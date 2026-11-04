"""工作区路径检查与凭据路径策略，文件工具和图片输入共用。"""

from pathlib import Path

from ..config import ENV_FILE
from .access import authorize_path, local_path

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".recapcli", ".idea", ".vscode",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


class ToolError(ValueError):
    """工具参数或执行环境不满足要求。"""


def checked_target(workspace, target):
    """入口路径和遍历得到的子路径使用同一份访问检查。"""
    target = local_path(target).resolve()
    if target == ENV_FILE.resolve():
        raise ToolError("禁止访问 RecapCLI 的本地凭据配置")
    if sensitive_path(target):
        raise ToolError("禁止访问敏感凭据文件或目录")
    authorize_path(workspace, target)
    return target


def sensitive_path(path):
    """读取、检索、编辑和图片输入共用的凭据路径策略。"""
    # Windows 的 :$DATA 等备用数据流语法不能绕过基础文件名检查。
    parts = [part.split(":", 1)[0].rstrip(" .").casefold() for part in Path(path).parts]
    return any(part in {".ssh", ".aws", ".azure", ".kube", ".netrc", "_netrc", ".npmrc", ".pypirc"}
               or ((part == ".env" or part.startswith(".env."))
                   and part not in {".env.example", ".env.sample", ".env.template"})
               or part.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))
               for part in parts)
