"""终端壳要展示的只读信息：模型配置与已保存会话。

这里不创建运行数据目录、不请求模型、不读取密钥内容；只判断本地配置是否齐全。
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigError, list_model_profiles
from ..storage.files import data_root


@dataclass(frozen=True)
class ShellState:
    workspace: Path
    profiles: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    mode: str = "react"
    capabilities: tuple = ("read",)
    notes: tuple = ()

    @property
    def active_profile(self):
        return next((item for item in self.profiles if item.get("default")), self.profiles[0] if self.profiles else None)

    @property
    def ready(self):
        """本地模型与密钥配置齐全；不代表接口已经探测可用。"""
        profile = self.active_profile
        return bool(profile and profile.get("configured"))


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
    return ShellState(workspace=workspace, profiles=profiles, sessions=sessions, notes=tuple(notes))
