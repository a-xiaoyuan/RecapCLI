"""提问、工具状态和回答的显示层；不参与 Agent 消息历史和工具执行。"""

import io
import re
import shutil
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from rich.theme import Theme

from .terminal import terminal_output, write_terminal


# 跟随终端前景/背景：正文用默认色，次要信息用 ANSI 灰，只保留一个强调色。
ACCENT = "#d97757"
THEME = Theme({
    "user": "bold " + ACCENT,
    "assistant": "bold " + ACCENT,
    "model": "bright_black",
    "tool": "bright_black",
    "success": "green",
    "error": "bold red",
    "muted": "bright_black",
    "notice": ACCENT,
    "markdown.h1": "bold",
    "markdown.h2": "bold",
    "markdown.h3": "bold",
    "markdown.strong": "bold",
    "markdown.em": "italic",
    "markdown.code": ACCENT,
    "markdown.item.bullet": "bright_black",
    "markdown.item.number": "bright_black",
    "markdown.link": "underline " + ACCENT,
    "markdown.block_quote": "bright_black",
})


def clean_output(text):
    # 保留正文中的换行与缩进；模型和文件内容不能携带终端控制指令。
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(text))


class TerminalUI:
    def __init__(self, stream=None, error_stream=None, color="auto"):
        self.stream = sys.stdout if stream is None else stream
        self.error_stream = sys.stderr if error_stream is None else error_stream
        self.mode = {"auto": "auto", "always": "color", "never": "plain"}[color]
        self._stream_text = ""
        self._stream_visible = False

    def _output(self, draw, *, error=False):
        stream = self.error_stream if error else self.stream
        with terminal_output(stream, self.mode) as color:
            buffer = io.StringIO()
            size = shutil.get_terminal_size(fallback=(80, 24))
            console = Console(
                file=buffer, force_terminal=color, color_system="truecolor" if color else None,
                no_color=not color, legacy_windows=False, theme=THEME,
                markup=False, highlight=False, emoji=False,
                width=max(1, size.columns - 1), height=max(1, size.lines),
            )
            draw(console, color)
            write_terminal(stream, buffer.getvalue())

    def _print(self, text, *, style=None, error=False, end="\n"):
        self._output(lambda console, color: console.print(
            Text(clean_output(text), style=style), end=end,
        ), error=error)

    def user(self, prompt):
        text = Text("› ", style="user")
        text.append(clean_output(prompt), style="default")
        self._output(lambda console, color: console.print(text))

    def read_prompt(self):
        self._print("› ", style="user", end="")
        return input()

    def event(self, event):
        self._finish_stream()
        if event.startswith("[结果]"):
            style = "error" if event.startswith("[结果] 失败") else "success"
        elif event.startswith("[待办]") or event.startswith("[收尾]"):
            style = "notice"
        else:
            style = "tool"
        self._print(event, style=style)

    def answer(self, answer):
        answer = clean_output(answer)
        if self._stream_visible:
            streamed = self._stream_text
            self._finish_stream()
            if answer == streamed:
                return
            if streamed and answer.endswith(streamed):
                self.info(answer[:-len(streamed)].strip())
                return

        def draw(console, color):
            console.print()
            console.print(Text("●", style="assistant"))
            if color:
                console.print(Padding(Markdown(answer, code_theme="default", hyperlinks=False), (0, 0, 0, 2)))
            else:
                # 保存到文件时保留原始 Markdown，便于再次阅读或处理。
                console.print(Text(answer), soft_wrap=True)
            console.print()

        self._output(draw)

    def _finish_stream(self):
        if self._stream_visible:
            write_terminal(self.stream, "\n\n")
        self._stream_visible = False
        self._stream_text = ""

    def stream_update(self, kind, text):
        if kind == "start":
            self._finish_stream()
        elif kind == "delta":
            if not self._stream_visible:
                self._print("\n● ", style="assistant", end="")
                self._stream_visible = True
            text = clean_output(text)
            self._stream_text += text
            write_terminal(self.stream, text)
        elif kind == "end":
            # 正文保留到 answer()，最终回答不再重复打印。
            pass
        elif kind == "abort":
            if self._stream_visible:
                write_terminal(self.stream, "\n[回答未完成]\n")
            self._stream_visible = False
            self._stream_text = ""

    def error(self, message):
        self._finish_stream()
        self._print(message, style="error", error=True)

    def info(self, message):
        if self._stream_visible:
            self._finish_stream()
        self._print(message, style="muted")

    def show_approval(self, message):
        self._print(message, style="notice")
