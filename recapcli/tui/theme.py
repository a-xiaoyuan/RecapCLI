"""终端主题与对话排版。

不铺底色：背景和正文颜色跟随用户的终端（包括背景图）。配色有六个家族，各有深浅两版；
PALETTE / GRADIENT 是当前生效的一套，切换主题时原地更新，渲染时实时读取。
recap.tcss 通过 $recap-* 变量取同一套颜色（见 css_variables）。
"""

import json
import re

from rich.box import ROUNDED
from rich.markdown import BlockQuote, Heading, ListElement, Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..ui.terminal_ui import clean_output

def _scheme(muted, faint, accent, online, error, warning, info, surface, added, removed, code, gradient):
    return {"muted": muted, "faint": faint, "accent": accent, "online": online, "error": error,
            "warning": warning, "info": info, "surface": surface, "added": added, "removed": removed,
            "code": code, "gradient": gradient}


# 各家族官方的日夜两版取色；surface 是用户消息底色，added / removed 是差异行底色，code 是行内代码字色。
THEMES = {
    "catppuccin": ("Catppuccin", {
        "dark": _scheme("#a6adc8", "#6c7086", "#cba6f7", "#a6e3a1", "#f38ba8", "#fab387", "#89b4fa",
                        "#2a2740", "#1f3329", "#3a2230", "#ddb6f2", ("#f5c2e7", "#cba6f7", "#89b4fa", "#94e2d5")),
        "light": _scheme("#6c6f85", "#9ca0b0", "#8839ef", "#40a02b", "#d20f39", "#fe640b", "#1e66f5",
                         "#e6e9ef", "#d5ecd4", "#f5d3da", "#8839ef", ("#ea76cb", "#8839ef", "#1e66f5", "#179299")),
    }),
    "kanagawa": ("神奈川", {
        "dark": _scheme("#a6a69c", "#727169", "#7e9cd8", "#98bb6c", "#e46876", "#ffa066", "#7fb4ca",
                        "#2a2a37", "#2b3328", "#43242b", "#e6c384", ("#7e9cd8", "#957fb8", "#d27e99", "#e6c384")),
        "light": _scheme("#716e61", "#8a8980", "#4d699b", "#6f894e", "#c84053", "#cc6d00", "#4e8ca2",
                         "#e5ddb0", "#dde8cf", "#f3d6d6", "#624c83", ("#4d699b", "#624c83", "#b35b79", "#cc6d00")),
    }),
    "rose-pine": ("Rosé Pine", {
        "dark": _scheme("#908caa", "#6e6a86", "#c4a7e7", "#9ccfd8", "#eb6f92", "#f6c177", "#9ccfd8",
                        "#26233a", "#1f2d2f", "#3a2232", "#ebbcba", ("#ebbcba", "#c4a7e7", "#9ccfd8")),
        "light": _scheme("#797593", "#9893a5", "#907aa9", "#56949f", "#b4637a", "#ea9d34", "#286983",
                         "#f2e9e1", "#e3efee", "#f4dde1", "#d7827e", ("#d7827e", "#907aa9", "#56949f")),
    }),
    "tokyo-night": ("Tokyo Night", {
        "dark": _scheme("#a9b1d6", "#565f89", "#7aa2f7", "#9ece6a", "#f7768e", "#e0af68", "#7dcfff",
                        "#24283b", "#20303b", "#37222c", "#bb9af7", ("#7aa2f7", "#bb9af7", "#f7768e")),
        "light": _scheme("#6172b0", "#8990b3", "#2e7de9", "#587539", "#f52a65", "#8c6c3e", "#007197",
                         "#d0d5e3", "#d6e3d0", "#f0d4dc", "#9854f1", ("#2e7de9", "#9854f1", "#f52a65")),
    }),
    "gruvbox": ("Gruvbox", {
        "dark": _scheme("#bdae93", "#928374", "#83a598", "#b8bb26", "#fb4934", "#fe8019", "#8ec07c",
                        "#3c3836", "#32361a", "#3c1f1e", "#fabd2f", ("#fb4934", "#fe8019", "#fabd2f", "#b8bb26")),
        "light": _scheme("#665c54", "#928374", "#076678", "#79740e", "#9d0006", "#af3a03", "#427b58",
                         "#ebdbb2", "#e6e4b8", "#f2d5c8", "#b57614", ("#9d0006", "#af3a03", "#b57614", "#79740e")),
    }),
    "github": ("GitHub", {
        "dark": _scheme("#9ea7b3", "#7d8590", "#2f81f7", "#3fb950", "#f85149", "#d29922", "#58a6ff",
                        "#161b22", "#12261e", "#2d1517", "#a371f7", ("#2f81f7", "#a371f7", "#db61a2")),
        "light": _scheme("#57606a", "#8c959f", "#0969da", "#1a7f37", "#cf222e", "#9a6700", "#0550ae",
                         "#f6f8fa", "#dafbe1", "#ffebe9", "#8250df", ("#0969da", "#8250df", "#bf3989")),
    }),
}
MODES = {"light": "浅色", "dark": "深色"}
DEFAULT_THEME = ("catppuccin", "light")

# 当前生效的配色。Rich 样式里 "default" 即终端自身的前景色。
PALETTE = {}
GRADIENT = ()
CURRENT_THEME = DEFAULT_THEME


def parse_theme(value):
    """“名称”或“名称:light|dark”；只写名称时用浅色。返回 (家族, 模式)。"""
    name, _, mode = str(value or "").strip().lower().partition(":")
    if name not in THEMES:
        raise ValueError(f"未知主题：{value}。可选：{'、'.join(THEMES)}")
    mode = mode or DEFAULT_THEME[1]
    if mode not in MODES:
        raise ValueError("主题模式只能是 light 或 dark，例如 catppuccin:dark")
    return name, mode


def initial_theme(value=None):
    """启动时的主题：--theme 参数优先，其次是上次 /theme 保存的选择，都没有就用默认。"""
    if value:
        return parse_theme(value)
    from ..config import load_ui_settings
    try:
        saved = load_ui_settings().get("theme")
        return parse_theme(saved) if saved else DEFAULT_THEME
    except ValueError:
        return DEFAULT_THEME  # 保存的值坏了不阻止启动，用默认主题


def theme_label(family, mode):
    return f"{THEMES[family][0]} · {MODES[mode]}"


def apply_palette(family, mode):
    """原地替换 PALETTE 和 GRADIENT；已经画出来的内容需要调用方重新渲染。"""
    global GRADIENT, CURRENT_THEME
    scheme = dict(THEMES[family][1][mode])
    GRADIENT = tuple(scheme.pop("gradient"))
    PALETTE.clear()
    PALETTE.update({"text": "default", **scheme})
    CURRENT_THEME = (family, mode)


def css_variables():
    """给 recap.tcss 用的 $recap-* 变量。"""
    return {f"recap-{key}": value for key, value in PALETTE.items() if key not in {"text", "added", "removed", "code"}}


apply_palette(*DEFAULT_THEME)


def _rgb(value):
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def gradient_color(position):
    """position 取 0–1 循环：渐变首尾相接，流动时不会跳色。"""
    stops = (*GRADIENT, GRADIENT[0])
    scaled = (position % 1.0) * (len(stops) - 1)
    index = int(scaled)
    start, end = _rgb(stops[index]), _rgb(stops[index + 1])
    mix = scaled - index
    return "#" + "".join(f"{round(a + (b - a) * mix):02x}" for a, b in zip(start, end))


def gradient_text(content, phase=0.0, span=None):
    """逐列着色的多行文字；phase 改变时颜色沿文字流动。"""
    lines = content.split("\n")
    width = span or max(len(line) for line in lines) or 1
    text = Text(no_wrap=True)
    for row, line in enumerate(lines):
        if row:
            text.append("\n")
        for column, char in enumerate(line):
            text.append(char, style="bold " + gradient_color(phase + (column + row * 2) / (width * 1.5)))
    return text


def progress_bar(done, total, width=14):
    """渐变进度条；total 无效时返回空条。"""
    filled = 0 if total <= 0 else min(width, round(width * done / total))
    if done > 0 and total > 0:
        filled = max(1, filled)  # 用了一点也要看得出来，不能四舍五入成空条
    text = Text(no_wrap=True)
    for index in range(width):
        if index < filled:
            text.append("▰", style=gradient_color(index / (width * 1.5)))
        else:
            text.append("▱", style=PALETTE["faint"])
    return text


class QuotePanel(BlockQuote):
    def __rich_console__(self, console, options):
        yield Panel(self.elements, box=ROUNDED, border_style=PALETTE["warning"], padding=(0, 1), safe_box=False)


class OrderedList(ListElement):
    def __rich_console__(self, console, options):
        number = self.list_start or 1
        for index, item in enumerate(self.items):
            for segment in item.render_number(console, options, number + index, number + len(self.items)):
                if index == 0:
                    yield Segment(segment.text, (segment.style or Style()) + Style(color=PALETTE["online"]))
                else:
                    yield segment


class SectionHeading(Heading):
    def __rich_console__(self, console, options):
        yield Text.assemble(("◈ ", "bold " + PALETTE["accent"]), self.text, justify="left")


class AnswerMarkdown(Markdown):
    elements = {**Markdown.elements, "blockquote_open": QuotePanel, "ordered_list_open": OrderedList,
                "heading_open": SectionHeading}


class GuidedAnswer:
    """逐行绘制引导线，正文紧接标题。"""

    def __init__(self, body, heading):
        self.body, self.heading = body, heading

    def __rich_console__(self, console, options):
        yield self.heading
        lines = console.render_lines(self.body, options.update(width=max(1, options.max_width - 2)), pad=False)
        # 去掉 Markdown 段落带来的首尾空行，引导线贴着正文。
        while lines and not "".join(segment.text for segment in lines[0]).strip():
            lines.pop(0)
        while lines and not "".join(segment.text for segment in lines[-1]).strip():
            lines.pop()
        guide = Style(color=PALETTE["accent"])
        for line in lines:
            yield Segment("┃ ", guide)
            yield from line
            yield Segment.line()


def render_answer(content, task=None):
    body = AnswerMarkdown(content, code_theme="ansi_dark", hyperlinks=False)
    body.parsed = [token for token in body.parsed if token.type != "hr"]
    for token in body.parsed:
        for child in token.children or ():
            if child.type == "softbreak":
                child.type = "hardbreak"
    heading = Text("● ", style=PALETTE["accent"])
    heading.append_text(gradient_text("RecapCLI"))
    if task is not None:
        metrics = []
        if task.duration is not None:
            metrics.append(f"{task.duration:.1f}s")
        total = (task.usage or {}).get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool):
            metrics.append(f"{total:,} tokens")
        if metrics:
            heading.append("  ·  " + "  ·  ".join(metrics), style=PALETTE["muted"] + " not bold")
    return GuidedAnswer(body, heading)


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_text(lines):
    """每个文件一行标题和增删统计，正文按增删着色；有 @@ 头时左侧显示行号（删除行用旧行号）。"""
    text = Text()
    old = new = None
    for index, line in enumerate(lines):
        if line.startswith("--- "):
            continue
        if text:
            text.append("\n")
        if line.startswith("+++ "):
            old = new = None
            rest = lines[index + 1:]
            end = next((i for i, item in enumerate(rest) if item.startswith("--- ")), len(rest))
            added = sum(item.startswith("+") for item in rest[:end])
            removed = sum(item.startswith("-") for item in rest[:end])
            text.append("└ " + line[4:].removeprefix("b/"), style=PALETTE["muted"])
            text.append(f"  +{added}", style=PALETTE["online"])
            text.append(f" -{removed}", style=PALETTE["error"])
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old, new = int(hunk.group(1)), int(hunk.group(2))
            text.append(line, style=PALETTE["muted"])
            continue
        number = ""
        if old is not None and not line.startswith("\\"):
            if line.startswith("-"):
                number, old = old, old + 1
            elif line.startswith("+"):
                number, new = new, new + 1
            else:
                number, old, new = new, old + 1, new + 1
        if old is not None:
            text.append(f"{number:>4} ", style=PALETTE["faint"])
        if line.startswith("+"):
            text.append(line, style=f"{PALETTE['online']} on {PALETTE['added']}")
        elif line.startswith("-"):
            text.append(line, style=f"{PALETTE['error']} on {PALETTE['removed']}")
        else:
            text.append(line, style=PALETTE["muted"])
    return text


def render_approval(content):
    """只留做决定需要的内容：操作和差异。工作区、文件指纹保留在任务记录里。"""
    lines = clean_output(content).split("\n")
    fields = {line.split("：", 1)[0]: line.split("：", 1)[1] for line in lines if "：" in line}
    if "工具" not in fields:
        return render_line(content, "event")
    text = Text("● ", style=PALETTE["accent"])
    text.append("需要批准", style=PALETTE["text"])
    text.append("  " + fields["工具"], style=PALETTE["muted"])
    start = lines.index("差异提示：") + 1 if "差异提示：" in lines else len(lines)
    end = next((index for index, line in enumerate(lines) if line.startswith("请输入 approve")), len(lines))
    command = None
    try:
        params = json.loads(fields.get("参数", ""))
        command = params.get("argv") if isinstance(params, dict) else None
    except ValueError:
        params = None
    if isinstance(command, list):
        cwd = params.get("cwd", ".")
        text.append("\n$ " + " ".join(str(item) for item in command) + ("" if cwd == "." else f"  （{cwd}）"))
    elif start >= end:
        text.append("\n" + fields.get("参数", ""), style=PALETTE["muted"])
    if start < end and not isinstance(command, list):
        text.append("\n")
        text.append_text(_diff_text(lines[start:end]))
    return text


def render_line(content, kind):
    """纯文字行；用户输入和模型输出都先移除终端控制字符。"""
    if kind == "approval":
        return render_approval(content)
    text_value = clean_output(content)
    if kind == "user":
        text = Text("❯ ", style="bold " + PALETTE["accent"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "assistant":
        text = Text("● ", style=PALETTE["accent"])
        text.append(text_value)
    elif kind == "error":
        text = Text(text_value, style=PALETTE["error"])
    elif kind == "event":
        head = text_value.split("\n", 1)[0]
        if text_value.startswith("[差异]\n"):
            text = _diff_text(text_value.split("\n")[1:])
        elif text_value.startswith("[操作] "):
            text = Text("└ " + text_value.removeprefix("[操作] "), style=PALETTE["muted"])
        else:
            text = Text(text_value.removeprefix("[命令输出]\n"),
                        style=PALETTE["error"] if "失败" in head else PALETTE["muted"])
    else:
        text = Text(text_value, style=PALETTE["muted"])
    return text
