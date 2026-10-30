"""黑底、薄荷绿点缀的终端主题。"""

from rich.text import Text

from ..ui.terminal_ui import clean_output

PALETTE = {
    "base": "#080b0a", "stage": "#101512", "line": "#24302a",
    "text": "#dce4de", "muted": "#b7c5bc", "faint": "#92a79a",
    "mint": "#49dba5", "online": "#49dba5", "error": "#f19f9c",
}
DISPLAY_LIMIT = 8000


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
        text = Text("> ", style=PALETTE["mint"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "assistant":
        text = Text("> ", style=PALETTE["mint"])
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
