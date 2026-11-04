"""本次运行的文件访问范围；外部目录确认一次，写入仍沿用工具能力与审批。"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock


_active = ContextVar("recapcli_file_access", default=None)
_directory_authorizer = None


def register_directory_authorizer(authorizer):
    """由审批层注册外部目录的确认流程，基础层不反向依赖工具层。"""
    global _directory_authorizer
    _directory_authorizer = authorizer


class AccessDenied(ValueError):
    def __init__(self, target, denial=None):
        message = f"工作区之外的目录尚未授权：{target}。可确认目录访问，或使用 --add-dir / /add-dir 指定目录。"
        super().__init__(message)
        self.result = {**(denial or {}), "ok": False, "error": message}


def local_path(value):
    path = Path(value).expanduser()
    if path.drive.startswith("\\\\"):
        raise ValueError("文件工具暂不访问 UNC 或设备路径，请使用本机目录")
    return path


class PathAccess:
    def __init__(self, workspace, directories=()):
        self.workspace = Path(workspace).resolve()
        self.directories = []
        self.lock = RLock()
        for directory in directories:
            self.add(directory)

    def add(self, directory):
        path = local_path(directory).resolve()
        if path.exists() and not path.is_dir():
            raise ValueError(f"授权目标不是目录：{path}")
        with self.lock:
            if path != self.workspace and path not in self.directories:
                self.directories.append(path)
        return path

    def contains(self, target):
        return any(target.is_relative_to(root) for root in (self.workspace, *self.directories))


@contextmanager
def using_access(policy, approver=None, on_approval=None, time_remaining=None):
    token = _active.set((policy, approver, on_approval, time_remaining))
    try:
        yield
    finally:
        _active.reset(token)


def authorize_path(workspace, target):
    workspace = Path(workspace).resolve()
    if target.is_relative_to(workspace):
        return
    active = _active.get()
    denial = None
    if active is not None:
        policy, approver, on_approval, remaining = active
        with policy.lock:
            if policy.contains(target):
                return
            directory = target if target.is_dir() else target.parent
            denial = _directory_authorizer(workspace, directory, approver, on_approval, remaining)
            if denial is None:
                policy.add(directory)
                return
    raise AccessDenied(target, denial)


def access_root(workspace, target):
    roots = [Path(workspace).resolve()]
    active = _active.get()
    if active is not None:
        roots.extend(active[0].directories)
    matches = [root for root in roots if target.is_relative_to(root)]
    return max(matches, key=lambda root: len(root.parts)) if matches else roots[0]
