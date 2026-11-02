"""终端主题与对话排版。"""

from rich.box import ROUNDED
from rich.markdown import BlockQuote, Heading, ListElement, Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ..ui.terminal_ui import clean_output

PALETTE = {
    "base": "#161622", "stage": "#1e1e2e", "line": "#45475a",
    "text": "#cdd6f4", "muted": "#a6adc8", "faint": "#6c7086",
    "accent": "#cba6f7", "online": "#a6e3a1", "error": "#f38ba8",
    "user": "#cba6f7", "assistant": "#cba6f7", "yellow": "#fab387",
    "index": "#b4befe", "warning": "#fab387", "user_background": "#262639",
}

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
    """逐行绘制引导线，正文紧接标题，不引入 Panel 的空白上下边框。"""

    def __init__(self, body, heading):
        self.body, self.heading = body, heading

    def __rich_console__(self, console, options):
        yield self.heading
        lines = console.render_lines(self.body, options.update(width=max(1, options.max_width - 2)))
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
    heading = Text("● RecapCLI", style="bold " + PALETTE["accent"])
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


def dot(ready=None):
    color = PALETTE["online"] if ready is True else PALETTE["faint"]
    return Text.assemble(("●" if ready is True else "○", color))


def render_line(content, kind):
    """纯文字行；用户输入和模型输出都先移除终端控制字符。"""
    text_value = clean_output(content)
    if kind == "user":
        text = Text("❯ ", style="bold " + PALETTE["user"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "assistant":
        text = Text("● ", style=PALETTE["assistant"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "error":
        text = Text(text_value, style=PALETTE["error"])
    elif kind == "event":
        head = text_value.split("\n", 1)[0]
        style = PALETTE["error"] if "失败" in head else PALETTE["text"]
        text = Text(text_value, style=style)
        if text_value.startswith("[差异]\n"):
            text.highlight_regex(r"(?m)^\+(?!\+).*$", PALETTE["online"])
            text.highlight_regex(r"(?m)^-(?!-).*$", PALETTE["error"])
            text.highlight_regex(r"(?m)^@@.*$", PALETTE["index"])
    else:
        text = Text(text_value, style="italic " + PALETTE["faint"])
    return text
