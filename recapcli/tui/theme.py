"""中性深色终端；薰衣草点缀，绿色只用于就绪点。"""

from rich.text import Text

PALETTE = {
    "base": "#0b0d0f", "stage": "#111315", "line": "#303238",
    "text": "#dddde3", "muted": "#b6b7c0", "faint": "#9698a3",
    "lavender": "#cdb9ef", "violet": "#cdb9ef", "online": "#45c575", "error": "#e6a1ab",
}


def spaced(text, gap=" "):
    return gap.join(text)


def dot(ready=None):
    color = PALETTE["online"] if ready is True else PALETTE["faint"]
    return Text.assemble(("●" if ready is True else "○", color))
