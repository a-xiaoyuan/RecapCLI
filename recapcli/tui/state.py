"""终端壳启动时读取的模型配置与已保存会话。

这里不创建运行数据目录、不请求模型、不向界面暴露密钥；只判断本地配置是否齐全。
真正发送时由 driver 打开会话并调用现有 Agent。
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess

from ..config import ConfigError, list_model_profiles
from ..storage.files import data_root
from ..tools.filesystem import sensitive_path


@dataclass(frozen=True)
class ShellState:
    workspace: Path
    profiles: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    mode: str = "react"
    capabilities: tuple = ("read",)
    notes: tuple = ()
    branch: str = ""

    @property
    def active_profile(self):
        return next((item for item in self.profiles if item.get("default")), self.profiles[0] if self.profiles else None)

    @property
    def ready(self):
        """本地模型与密钥配置齐全；不代表接口已经探测可用。"""
        profile = self.active_profile
        return bool(profile and profile.get("configured"))


def workspace_files(workspace, limit=5000):
    """@ 补全的候选：优先 git 列出的文件（遵守 .gitignore），不列凭据路径。"""
    workspace = Path(workspace)
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(workspace),
                                 "ls-files", "-co", "--exclude-standard"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, timeout=2, check=False)
        paths = result.stdout.splitlines() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        paths = None
    if paths is None:
        paths = []
        for root, directories, files in os.walk(workspace):
            directories[:] = [name for name in directories if not name.startswith(".")]
            paths.extend((Path(root) / name).relative_to(workspace).as_posix() for name in files)
            if len(paths) >= limit:
                break
    return [path for path in paths[:limit] if not sensitive_path(path)]


def _sessions(workspace):
    root = data_root(workspace)  # 只计算路径，不创建目录
    if not (root / "sessions").is_dir():
        return []
    from ..storage.session import SessionStore  # 目录已存在时才构造，避免在新工作区落盘
    return SessionStore(workspace).list_sessions()


def load_shell_state(workspace):
    workspace = Path(workspace).resolve()
    notes = []
    try:
        profiles = list_model_profiles()
    except ConfigError as error:
        profiles = []
        notes.append(f"模型配置读取失败：{error}")
    try:
        sessions = _sessions(workspace)
    except (OSError, ValueError) as error:
        sessions = []
        notes.append(f"会话列表读取失败：{error}")
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                 "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, timeout=2, check=False)
        branch = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""
    return ShellState(workspace=workspace, profiles=profiles, sessions=sessions, notes=tuple(notes), branch=branch)
