"""用终端色块绘制软萌小幽灵，并提供共用的终端兼容处理。"""

from contextlib import contextmanager
import os
import shutil
import sys
import tomllib
import unicodedata
from pathlib import Path

from ..config import PROJECT_ROOT


# 圆圆的薄荷白小幽灵、粉色腮红与挥动的小手；每个字符承载上下两个像素。
GHOST = (
    "",
    "",
    "                      g",
    "                     gwg",
    "          ooooooo     g",
    "        ooiwwwwwboo",
    " l     oiwwwwwwwwwbo",
    "lwl   oiwwwwwwwwwwwbo",
    " l   oiwwwwwwwwwwwwwbo",
    "     oiwwwwwwwwwwwwwbo",
    "    oiwwwwwwwwwwwwwwwbwio",
    "    oiwwwwwwwwwwwwwwwbwio",
    "    oiwwwwwwwwwwwwwwwbio",
    "    oiwwwwdwwwwwwdwwwbo",
    "  oiwiwwwddwwwwwddwwwbo",
    "  oiwiwwwwwwwwwwwwwwwbo",
    "   owiwccwwwwwwwwwccwbo",
    "    oiwwwwwwmwmwwwwwwbo",
    "    oiwwwwwwwmwwwwwwwbo",
    "    oiwwwwwwwwwwwwwwwbo",
    "    oiwwwwwwwwwwwwwwwbo",
    "     oiwwwwwwwwwwwwwwbo",
    "     oiwwwwwwwwwwwwwbo",
    "     oiwwwiiwwwiiwwwbo",
    "     oiwwo owwwio owbo",
    "      oooo  oooo   oo",
    "                       l",
    "                      lwl",
    "                       l",
)

PALETTE = {
    "o": (83, 88, 124),     # 柔和的蓝紫轮廓
    "b": (179, 206, 226),   # 身体边缘阴影
    "l": (193, 178, 239),   # 薰衣草色小星星
    "i": (214, 239, 241),   # 浅薄荷色边缘
    "w": (245, 252, 252),   # 软软的白色身体
    "g": (255, 219, 149),   # 奶油金色小星星
    "d": (74, 79, 115),     # 豆豆眼
    "c": (250, 177, 199),   # 粉色腮红
    "m": (183, 113, 151),   # 小小的笑脸
}


RESET = "\033[0m"
TEAL = "\033[36m"
# 启动面板里小幽灵的最大宽度；缩小占比，把空间留给标题、提示与工作区信息。
GHOST_MAX_WIDTH = 16


def cell_width(text):
    """按终端列宽计算中文，避免面板右边框错位。"""
    return sum(0 if unicodedata.category(char) in {"Mn", "Me", "Cf", "Cc"} else
               2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in text)


def fit_text(text, width, *, middle=False):
    """按显示列截断；路径可保留首尾，小于三列时也不会溢出。"""
    width = max(0, width)
    if cell_width(text) <= width:
        return text + " " * (width - cell_width(text))
    if width <= 3:
        return "." * width
    budget = width - 3
    prefix = ""
    for char in text:
        if cell_width(prefix + char) > (budget // 2 if middle else budget):
            break
        prefix += char
    suffix = ""
    if middle:
        for char in reversed(text[len(prefix):]):
            if cell_width(char + suffix) > budget - cell_width(prefix):
                break
            suffix = char + suffix
    result = prefix + "..." + suffix
    return result + " " * (width - cell_width(result))


def compact_sprite(max_width=None, max_height=None):
    """按窗口等比缩小像素画；空间足够时保留原图，不放大。"""
    width = max(map(len, GHOST))
    rows = [row.ljust(width) for row in GHOST]
    height = len(rows)
    scale = min(1, (max_width or width) / width, (max_height or height) / height)
    target_width = max(1, int(width * scale))
    target_height = max(1, int(height * scale))
    scaled = [[rows[min(height - 1, int((y + 0.5) * height / target_height))]
                   [min(width - 1, int((x + 0.5) * width / target_width))]
               for x in range(target_width)] for y in range(target_height)]
    if scale < 1 and target_width >= 12:
        # 缩小时补回可能被采样跳过的五官。
        for source_x, source_y, color in [(10, 13, "d"), (17, 13, "d"), (13, 18, "m")]:
            x = min(target_width - 1, int((source_x + 0.5) * target_width / width))
            y = min(target_height - 1, int((source_y + 0.5) * target_height / height))
            scaled[y][x] = color
    return ["".join(row) for row in scaled]


def pixel_lines(rows=None):
    """上下两个像素合为一个字符，保持像素比例并减少启动画面高度。"""
    rows = compact_sprite() if rows is None else list(rows)
    width = max(map(len, rows), default=0)
    if not width:
        return []
    rows = [row.ljust(width) for row in rows]
    rows += [" " * width] if len(rows) % 2 else []
    lines = []
    for y in range(0, len(rows), 2):
        line = ""
        for top, bottom in zip(rows[y], rows[y + 1]):
            line += RESET
            if top == bottom == " ":
                line += " "
                continue
            if top != " ":
                line += "\033[38;2;{};{};{}m".format(*PALETTE[top])
            if bottom != " " and top != " ":
                line += "\033[48;2;{};{};{}m".format(*PALETTE[bottom])
            elif bottom != " ":
                line += "\033[38;2;{};{};{}m".format(*PALETTE[bottom])
            line += "▄" if top == " " else "▀"
        lines.append(line + RESET)
    return lines


def _clean(text):
    return "".join(char if char.isprintable() else "?" for char in str(text))


def _title():
    try:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
            version = tomllib.load(file)["project"]["version"]
        return "RecapCLI  v" + _clean(version)
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return "RecapCLI"


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


def _render_banner(path, preview, columns, height, color):
    # 少用最后一列，并给后续输入/输出留三行，避免自动换行或挤掉提示符。
    width = max(1, min(columns - 1, 96))
    title = _title()
    mode = "模式  " + ("界面预览 · 不请求模型" if preview else "模型问答 · 工作区工具")

    def workspace_text(available):
        label = "工作区  "
        if available < cell_width(label) + 4:
            return fit_text(path, available, middle=True)
        return label + fit_text(path, available - cell_width(label), middle=True)

    def plain():
        lines = [fit_text(title, width), fit_text("你的终端代码阅读搭档", width),
                 fit_text(mode, width), workspace_text(width), fit_text("--help 查看帮助", width)]
        return "\n".join(line.rstrip() for line in lines[:max(1, height - 3)]) + "\n"

    if not color or columns < 40:
        return plain()
    inner = width - 4
    body_budget = height - 7  # 两条边框、提示、空行，加三行后续输入区域。
    if body_budget < 8:
        return plain()
    rows = compact_sprite(max_width=GHOST_MAX_WIDTH, max_height=body_budget * 2)
    side_by_side = inner - len(rows[0]) - 3 >= 24
    if not side_by_side:
        rows = compact_sprite(max_width=min(inner, GHOST_MAX_WIDTH), max_height=(body_budget - 5) * 2)
    if len(rows[0]) < 12:
        return plain()
    art = pixel_lines(rows)
    art_width = len(rows[0])
    output = []

    def border(left, right):
        output.append(TEAL + left + "─" * (width - 2) + right + RESET)

    def panel_row(content):
        output.append(TEAL + "│ " + RESET + content + TEAL + " │" + RESET)

    def styled(text, index, available):
        color = "\033[1m" if index == 0 else TEAL if index in {3, 4} else ""
        return color + fit_text(text, available) + RESET

    border("╭", "╮")
    if side_by_side:
        info = [title, "你的终端代码阅读搭档", "", "读项目  /  查代码  /  工具调用",
                mode, None, "", "小幽灵，陪你慢慢读懂代码。"]
        available = inner - art_width - 3
        info_offset = (len(art) - len(info)) // 2
        for index, line in enumerate(art):
            info_index = index - info_offset
            text = info[info_index] if 0 <= info_index < len(info) else ""
            if text is None:
                text = workspace_text(available)
            panel_row(line + " " * 3 + styled(text, info_index, available))
    else:
        for line in art:
            left = (inner - art_width) // 2
            panel_row(" " * left + line + " " * (inner - art_width - left))
        panel_row(" " * inner)
        for index, text in enumerate([title, "你的终端代码阅读搭档", mode, workspace_text(inner)]):
            panel_row(styled(text, index, inner))
    border("╰", "╯")
    hint = "  --preview 预览   --help 帮助   Ctrl+C 退出"
    output.append("\033[35m" + fit_text(hint, width).rstrip() + RESET)
    return "\n".join(output) + "\n\n"


@contextmanager
def terminal_output(stream, mode="auto"):
    """让面板与正文使用相同的颜色检测和 Windows 控制台恢复逻辑。"""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    color = _supports_color(stream, mode)
    try:
        "▀▄│─╭╮╰╯".encode(encoding)
    except LookupError:
        encoding = "utf-8"
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


def print_banner(stream=None, workspace=None, preview=False, mode="auto"):
    """按终端宽高布局；兼容 PyCharm，并为重定向和旧编码提供文本回退。"""
    if mode not in {"auto", "color", "plain", "off"}:
        raise ValueError("未知启动面板模式")
    if mode == "off":
        return
    stream = sys.stdout if stream is None else stream
    workspace = Path.cwd() if workspace is None else Path(workspace)
    path = _clean(workspace.resolve())
    size = shutil.get_terminal_size(fallback=(80, 24))
    with terminal_output(stream, mode) as color:
        write_terminal(stream, _render_banner(path, preview, max(1, size.columns), max(1, size.lines), color))
