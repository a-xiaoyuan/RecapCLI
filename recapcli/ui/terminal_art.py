"""用等宽方块像素绘制 Recap 字标，并提供共用的终端兼容处理。"""

from contextlib import contextmanager
import os
import shutil
import sys
import tomllib
import unicodedata
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path

from ..config import PROJECT_ROOT


# 五枚等宽字母，橙色方块像素。显示时上下两格合成一个半块字符。
_LETTERS = {
    "R": ("oooo ", "o   o", "o   o", "oooo ", "o  o ", "o   o", "o   o"),
    "E": ("ooooo", "o    ", "o    ", "oooo ", "o    ", "o    ", "ooooo"),
    "C": (" oooo", "o    ", "o    ", "o    ", "o    ", "o    ", " oooo"),
    "A": (" ooo ", "o   o", "o   o", "ooooo", "o   o", "o   o", "o   o"),
    "P": ("oooo ", "o   o", "o   o", "oooo ", "o    ", "o    ", "o    "),
}
RECAP_MARK = tuple(" ".join(letter[row] for letter in (_LETTERS[name] for name in "RECAP"))
                   for row in range(len(_LETTERS["R"])))

# 与 tui 强调色 #d4652a 一致。只涂字标像素，不给整块终端铺底。
PIXEL_COLORS = {"o": (212, 101, 42)}

RESET = "\033[0m"
ACCENT = "\033[38;2;212;101;42m"
MUTED = "\033[38;2;111;103;95m"
MARK_MAX_WIDTH = max(map(len, RECAP_MARK))


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


def scale_rows(source, max_width=None, max_height=None):
    """按窗口等比缩小像素行；空间足够时保留原图，不放大。"""
    width = max(map(len, source))
    rows = [row.ljust(width) for row in source]
    height = len(rows)
    scale = min(1, (max_width or width) / width, (max_height or height) / height)
    target_width = max(1, int(width * scale))
    target_height = max(1, int(height * scale))
    scaled = [[rows[min(height - 1, int((y + 0.5) * height / target_height))]
                   [min(width - 1, int((x + 0.5) * width / target_width))]
               for x in range(target_width)] for y in range(target_height)]
    return ["".join(row) for row in scaled]


def _half_blocks(rows):
    """上下两个像素合为一个字符，返回每一显示行的像素对。"""
    width = max(map(len, rows), default=0)
    if not width:
        return []
    rows = [row.ljust(width) for row in rows]
    if len(rows) % 2:
        rows.append(" " * width)
    lines = []
    for y in range(0, len(rows), 2):
        lines.append(list(zip(rows[y], rows[y + 1])))
    return lines


def pixel_lines(rows=None):
    """字标的 ANSI 行，供行式启动面板打印。TUI 不要把这些字符串放进 Static。"""
    pairs = _half_blocks(scale_rows(RECAP_MARK) if rows is None else list(rows))
    lines = []
    for row in pairs:
        line = ""
        for top, bottom in row:
            line += RESET
            if top == bottom == " ":
                line += " "
                continue
            if top != " ":
                line += "\033[38;2;{};{};{}m".format(*PIXEL_COLORS[top])
            if bottom != " " and top != " ":
                line += "\033[48;2;{};{};{}m".format(*PIXEL_COLORS[bottom])
            elif bottom != " ":
                line += "\033[38;2;{};{};{}m".format(*PIXEL_COLORS[bottom])
            line += "▄" if top == " " else "▀"
        lines.append(line + RESET)
    return lines


def wordmark_text(max_width=None, max_height=None):
    """Rich 文本字标。窄到无法辨认时退回 RECAP，不含 ANSI 转义。"""
    from rich.color import Color
    from rich.style import Style
    from rich.text import Text

    rows = scale_rows(RECAP_MARK, max_width, max_height)
    if not rows or len(rows[0]) < 12:
        return Text("RECAP", style=Style(color=Color.from_rgb(*PIXEL_COLORS["o"]), bold=True))
    ink = Style(color=Color.from_rgb(*PIXEL_COLORS["o"]))
    solid = Style(color=ink.color, bgcolor=ink.color)
    text = Text()
    for index, row in enumerate(_half_blocks(rows)):
        if index:
            text.append("\n")
        for top, bottom in row:
            if top == bottom == " ":
                text.append(" ")
            elif top != " " and bottom != " ":
                text.append("▀", style=solid)
            elif top != " ":
                text.append("▀", style=ink)
            else:
                text.append("▄", style=ink)
    return text


def _clean(text):
    return "".join(char if char.isprintable() else "?" for char in str(text))


def project_version():
    try:
        return distribution_version("recapcli")
    except PackageNotFoundError:
        pass
    try:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
            return str(tomllib.load(file)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None


def _title():
    version = project_version()
    return "RecapCLI" + ("  v" + _clean(version) if version else "")


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

    tagline = "终端里的代码阅读搭档"

    def plain():
        lines = [fit_text(title, width), fit_text(tagline, width),
                 fit_text(mode, width), workspace_text(width), fit_text("--help 查看帮助", width)]
        return "\n".join(line.rstrip() for line in lines[:max(1, height - 3)]) + "\n"

    if not color or columns < 40:
        return plain()
    inner = width - 4
    body_budget = height - 7  # 两条边框、提示、空行，加三行后续输入区域。
    if body_budget < 8:
        return plain()
    rows = scale_rows(RECAP_MARK, max_width=min(inner, MARK_MAX_WIDTH), max_height=body_budget * 2)
    side_by_side = inner - len(rows[0]) - 3 >= 24
    if not side_by_side:
        rows = scale_rows(RECAP_MARK, max_width=min(inner, MARK_MAX_WIDTH), max_height=(body_budget - 5) * 2)
    if len(rows[0]) < 12:
        return plain()
    art = pixel_lines(rows)
    art_width = len(rows[0])
    output = []

    def border(left, right):
        output.append(ACCENT + left + "─" * (width - 2) + right + RESET)

    def panel_row(content):
        output.append(ACCENT + "│ " + RESET + content + ACCENT + " │" + RESET)

    def styled(text, index, available):
        color = "\033[1m" + ACCENT if index == 0 else MUTED if index in {3, 4} else ""
        return color + fit_text(text, available) + RESET

    border("╭", "╮")
    if side_by_side:
        info = [title, tagline, "", "读项目  /  查代码  /  工具调用",
                mode, None, "", "从手头的代码读起，改动留有记录。"]
        available = inner - art_width - 3
        row_count = max(len(art), len(info))
        art_offset = (row_count - len(art)) // 2
        info_offset = (row_count - len(info)) // 2
        blank = " " * art_width
        for index in range(row_count):
            art_index = index - art_offset
            line = art[art_index] if 0 <= art_index < len(art) else blank
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
        for index, text in enumerate([title, tagline, mode, workspace_text(inner)]):
            panel_row(styled(text, index, inner))
    border("╰", "╯")
    hint = "  --preview 预览字标   --help 帮助   Ctrl+C 退出"
    output.append(ACCENT + fit_text(hint, width).rstrip() + RESET)
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
