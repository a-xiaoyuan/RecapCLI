"""中性深色终端；薰衣草点缀，绿色只用于就绪点。"""

from rich.text import Text

from ..ui.terminal_ui import clean_output

PALETTE = {
    "base": "#0b0d0f", "stage": "#111315", "line": "#303238",
    "text": "#dddde3", "muted": "#b6b7c0", "faint": "#9698a3",
    "lavender": "#cdb9ef", "violet": "#cdb9ef", "online": "#45c575", "error": "#e6a1ab",
}
DISPLAY_LIMIT = 8000


def spaced(text, gap=" "):
    return gap.join(text)


def dot(ready=None):
    color = PALETTE["online"] if ready is True else PALETTE["faint"]
    return Text.assemble(("●" if ready is True else "○", color))


def render_line(content, kind):
    """对话行。成功结果不用绿色；绿色只留给就绪点。"""
    text_value = clean_output(content)
    if len(text_value) > DISPLAY_LIMIT:
        text_value = text_value[:DISPLAY_LIMIT] + "\n…（显示已截断，完整结果见 /task ID）"
    if kind == "user":
        text = Text("> ", style=PALETTE["muted"])
        text.append(text_value, style=PALETTE["text"])
    elif kind == "assistant":
        text = Text("> ", style=PALETTE["lavender"])
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
