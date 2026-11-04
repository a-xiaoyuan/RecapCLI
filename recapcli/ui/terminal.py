"""行式 CLI 的终端兼容处理：颜色检测、Windows VT、旧编码回退和一行启动标题。"""

from contextlib import contextmanager
import os
import sys
from pathlib import Path

from ..base.version import project_version

ACCENT = "\033[38;2;217;119;87m"
MUTED = "\033[90m"
RESET = "\033[0m"


def _clean(text):
    return "".join(char if char.isprintable() else "?" for char in str(text))


def title():
    release = project_version()
    return "RecapCLI" + ("  v" + _clean(release) if release else "")


def _supports_color(stream, mode):
    if mode == "color":
        return True
    if mode != "auto" or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if stream.isatty():
        return True
    # PyCharm 的普通 Run 控制台可能不是 TTY；文件和 StringIO 不应被误判。
    try:
        return os.environ.get("PYCHARM_HOSTED") == "1" and not stream.seekable()
    except (AttributeError, OSError):
        return False


def _windows_ansi(stream):
    """为真实 Windows 控制台临时启用 VT；返回恢复函数或失败标记。"""
    if os.name != "nt" or not stream.isatty():
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    except (AttributeError, OSError, ValueError):
        return None  # PyCharm 等模拟输出流不一定暴露控制台句柄。
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetConsoleMode.restype = wintypes.BOOL
    original = wintypes.DWORD()
    if not kernel.GetConsoleMode(handle, ctypes.byref(original)):
        return None if os.environ.get("PYCHARM_HOSTED") == "1" else False
    if not kernel.SetConsoleMode(handle, original.value | 0x0001 | 0x0004):
        return False
    return lambda: kernel.SetConsoleMode(handle, original.value)


@contextmanager
def terminal_output(stream, mode="auto"):
    """标题与正文共用颜色检测和 Windows 控制台恢复逻辑。"""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    color = _supports_color(stream, mode)
    try:
        "◆›".encode(encoding)
    except LookupError:
        color = False
    except UnicodeEncodeError:
        color = False
    restore = _windows_ansi(stream) if color else None
    if restore is False:
        color = False
    try:
        yield color
    finally:
        if callable(restore):
            restore()


def write_terminal(stream, text):
    """旧编码无法表示的装饰字符用替代字符输出，不阻断任务。"""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text = text.encode(encoding, errors="replace").decode(encoding)
    except LookupError:
        pass
    stream.write(text)
    stream.flush()


def print_banner(stream=None, workspace=None, mode="auto"):
    """两行启动标题：名称与版本，工作区路径。mode=off 时不输出。"""
    if mode not in {"auto", "color", "plain", "off"}:
        raise ValueError("未知启动标题模式")
    if mode == "off":
        return
    stream = sys.stdout if stream is None else stream
    path = _clean(Path(Path.cwd() if workspace is None else workspace).resolve())
    with terminal_output(stream, mode) as color:
        if color:
            text = f"{ACCENT}◆{RESET} {title()}\n  {MUTED}{path}{RESET}\n\n"
        else:
            text = f"{title()}\n  {path}\n\n"
        write_terminal(stream, text)
