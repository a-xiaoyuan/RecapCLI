"""终端主题与对话排版。

不铺底色：背景、正文颜色都跟随用户的终端（深色、浅色主题都协调），
次要信息用 ANSI 灰，只保留一个强调色。
"""

from rich.markdown import BlockQuote, Heading, Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..ui.terminal_ui import clean_output

# Rich 样式里的颜色名；"default" 即终端自身的前景色。与 recap.tcss 手写同步。
PALETTE = {
    "text": "default", "muted": "bright_black",
    "accent": "#e07b39", "online": "green", "error": "red", "warning": "yellow",
}


class QuietQuote(BlockQuote):
    """引用只画一条灰色竖线，不加边框。"""

    def __rich_console__(self, console, options):
        lines = console.render_lines(self.elements, options.update(width=max(1, options.max_width - 2)))
        bar = Style(color=PALETTE["muted"])
        for line in lines:
            yield Segment("│ ", bar)
            yield from line
            yield Segment.line()


class PlainHeading(Heading):
    def __rich_console__(self, console, options):
        self.text.stylize("bold")
        yield self.text


class AnswerMarkdown(Markdown):
    elements = {**Markdown.elements, "blockquote_open": QuietQuote, "heading_open": PlainHeading}


class BulletAnswer:
    """第一行前加强调色圆点，其余行缩进两格对齐，末尾可附一行灰色用量。"""

    def __init__(self, body, footer=None):
        self.body, self.footer = body, footer

    def __rich_console__(self, console, options):
        lines = console.render_lines(self.body, options.update(width=max(1, options.max_width - 2)), pad=False)
        # 去掉 Markdown 段落带来的首尾空行，圆点贴着正文。
        while lines and not "".join(segment.text for segment in lines[0]).strip():
            lines.pop(0)
        while lines and not "".join(segment.text for segment in lines[-1]).strip():
            lines.pop()
        for index, line in enumerate(lines):
            yield Segment("● " if index == 0 else "  ", Style(color=PALETTE["accent"]) if index == 0 else None)
            yield from line
            yield Segment.line()
        if self.footer is not None:
            yield Segment("  ")
            yield self.footer


def _metrics(task):
    if task is None:
        return None
    metrics = []
    if task.duration is not None:
        metrics.append(f"{task.duration:.1f}s")
    total = (task.usage or {}).get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        metrics.append(f"{total:,} tokens")
    return Text("  ·  ".join(metrics), style=PALETTE["muted"]) if metrics else None


def render_answer(content, task=None):
    body = AnswerMarkdown(content, code_theme="ansi_dark", hyperlinks=False)
    body.parsed = [token for token in body.parsed if token.type != "hr"]
    for token in body.parsed:
        for child in token.children or ():
            if child.type == "softbreak":
                child.type = "hardbreak"
    return BulletAnswer(body, _metrics(task))


def render_line(content, kind):
    """纯文字行；用户输入和模型输出都先移除终端控制字符。"""
    text_value = clean_output(content)
    if kind == "user":
        text = Text("› ", style="bold " + PALETTE["accent"])
        text.append(text_value, style="bold")
    elif kind == "assistant":
        text = Text("● ", style=PALETTE["accent"])
        text.append(text_value)
    elif kind == "error":
        text = Text(text_value, style=PALETTE["error"])
    elif kind == "event":
        head = text_value.split("\n", 1)[0]
        text = Text(text_value, style=PALETTE["error"] if "失败" in head else PALETTE["muted"])
        if text_value.startswith("[差异]\n"):
            text.highlight_regex(r"(?m)^\+(?!\+).*$", PALETTE["online"])
            text.highlight_regex(r"(?m)^-(?!-).*$", PALETTE["error"])
            text.highlight_regex(r"(?m)^@@.*$", PALETTE["accent"])
    else:
        text = Text(text_value, style=PALETTE["muted"])
    return text
