"""终端主题与对话排版。

不铺底色：背景和正文颜色跟随用户的终端（包括背景图），紫色作强调色。
"""

import json

from rich.box import ROUNDED
from rich.markdown import BlockQuote, Heading, ListElement, Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..ui.terminal_ui import clean_output

# Rich 样式里的颜色；"default" 即终端自身的前景色。与 recap.tcss 手写同步。
PALETTE = {
    "text": "default", "muted": "#a6adc8", "faint": "#6c7086",
    "accent": "#cba6f7", "online": "#a6e3a1", "error": "#f38ba8", "warning": "#fab387",
    "info": "#89b4fa", "surface": "#2a2740", "added": "#1f3329", "removed": "#3a2230",
}
GRADIENT = ("#f5c2e7", "#cba6f7", "#89b4fa", "#94e2d5")


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


def _diff_text(lines):
    """每个文件一行标题和增删统计，正文按增删着色。"""
    text = Text()
    for index, line in enumerate(lines):
        if line.startswith("--- "):
            continue
        if text:
            text.append("\n")
        if line.startswith("+++ "):
            rest = lines[index + 1:]
            end = next((i for i, item in enumerate(rest) if item.startswith("--- ")), len(rest))
            added = sum(item.startswith("+") for item in rest[:end])
            removed = sum(item.startswith("-") for item in rest[:end])
            text.append("└ " + line[4:].removeprefix("b/"), style=PALETTE["muted"])
            text.append(f"  +{added}", style=PALETTE["online"])
            text.append(f" -{removed}", style=PALETTE["error"])
        elif line.startswith("@@"):
            text.append(line, style=PALETTE["muted"])
        elif line.startswith("+"):
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
