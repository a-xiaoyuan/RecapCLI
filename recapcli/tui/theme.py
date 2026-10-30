"""中性终端底色与低饱和青绿点缀；正文不铺设代码背景。"""

from rich.text import Text

from ..ui.terminal_ui import clean_output

PALETTE = {
    "base": "#0c0c0c", "stage": "#191919", "line": "#555555",
    "text": "#dededc", "muted": "#b3b1ad", "faint": "#a09d97",
    "accent": "#80b6ad", "online": "#87b99a", "error": "#f28b91",
}
DISPLAY_LIMIT = 8000

_LETTERS = {
    "R": ("████ ", "█   █", "████ ", "█  █ ", "█   █"),
    "E": ("█████", "█    ", "████ ", "█    ", "█████"),
    "C": (" ████", "█    ", "█    ", "█    ", " ████"),
    "A": (" ███ ", "█   █", "█████", "█   █", "█   █"),
    "P": ("████ ", "█   █", "████ ", "█    ", "█    "),
    "L": ("█    ", "█    ", "█    ", "█    ", "█████"),
    "I": ("█████", "  █  ", "  █  ", "  █  ", "█████"),
}
WORDMARK = tuple("   ".join(" ".join(_LETTERS[char][row] for char in word)
                           for word in ("RECAP", "CLI")) for row in range(5))


def render_wordmark(width, *, compact=False):
    """用真实终端字符画标题；空间不足时只保留名称，不挤占输入区。"""
    art_width = max(map(len, WORDMARK))
    rows = ("RECAP CLI",) if compact or width < art_width else WORDMARK
    padding = " " * max(0, (width - max(map(len, rows))) // 2)
    return Text("\n".join(padding + row for row in rows), style="#e4e3de", no_wrap=True, overflow="crop")


def spaced(text, gap=" "):
    return gap.join(text)


def dot(ready=None):
    color = PALETTE["online"] if ready is True else PALETTE["faint"]
    return Text.assemble(("●" if ready is True else "○", color))


def render_line(content, kind):
    """纯文字行；用户输入和模型输出都先移除终端控制字符。"""
    text_value = clean_output(content)
    if len(text_value) > DISPLAY_LIMIT:
        text_value = text_value[:DISPLAY_LIMIT] + "\n…（显示已截断，完整结果见 /task ID）"
    if kind == "user":
        text = Text("> ", style=PALETTE["faint"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "assistant":
        text = Text("> ", style=PALETTE["faint"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "error":
        text = Text(text_value, style=PALETTE["error"])
    elif kind == "event":
        head = text_value.split("\n", 1)[0]
        style = PALETTE["error"] if "失败" in head else PALETTE["text"]
        text = Text(text_value, style=style)
    else:
        text = Text(text_value, style=PALETTE["faint"])
    return text
