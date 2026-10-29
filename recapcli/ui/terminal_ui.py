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

from .terminal_art import terminal_output, write_terminal


THEME = Theme({
    "user": "bold #96dfcf",
    "assistant": "bold #d4b6f7",
    "model": "#ada6c5",
    "tool": "#94d8e9",
    "success": "#acdba4",
    "error": "bold #f3a1b2",
    "muted": "#a9b1c6",
    "preview": "#efd396",
    "markdown.h1": "bold #d4b6f7",
    "markdown.h2": "bold #d4b6f7",
    "markdown.h3": "bold #b8c9f5",
    "markdown.strong": "bold #d4b6f7",
    "markdown.em": "italic #b8c9f5",
    "markdown.code": "#f0d59c on #242735",
    "markdown.item.bullet": "#96dfcf",
    "markdown.link": "underline #94d8e9",
    "markdown.block_quote": "#b0bfd1",
})


def clean_output(text):
    # 保留正文中的换行与缩进；模型和文件内容不能携带终端控制指令。
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(text))


class TerminalUI:
    def __init__(self, stream=None, error_stream=None, color="auto"):
        self.stream = sys.stdout if stream is None else stream
        self.error_stream = sys.stderr if error_stream is None else error_stream
        self.mode = {"auto": "auto", "always": "color", "never": "plain"}[color]

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
        text = Text("你> ", style="user")
        text.append(clean_output(prompt), style="default")
        self._output(lambda console, color: console.print(text))

    def read_prompt(self):
        self._print("你> ", style="user", end="")
        return input()

    def event(self, event):
        if event.startswith("[结果]"):
            style = "success" if event.startswith("[结果] 成功") else "error"
        elif event.startswith("[工具]") or event.startswith("[审批]"):
            style = "tool"
        else:
            style = "model"
        self._print(event, style=style)

    def answer(self, answer):
        answer = clean_output(answer)

        def draw(console, color):
            console.print()
            console.print(Text("RecapCLI>", style="assistant"))
            if color:
                console.print(Padding(Markdown(answer, code_theme="monokai", hyperlinks=False), (0, 0, 0, 2)))
            else:
                # 保存到文件时保留原始 Markdown，便于再次阅读或处理。
                console.print(Text(answer), soft_wrap=True)
            console.print()

        self._output(draw)

    def error(self, message):
        self._print(message, style="error", error=True)

    def info(self, message):
        self._print(message, style="muted")

    def show_approval(self, message):
        self._print(message, style="preview")

    def preview(self):
        self._print("界面预览 · 以下对话与日志均为演示，不请求模型", style="preview")
        self.user("读取 README.md，说明项目如何运行")
        self.event("[模型] 第 1 次请求")
        self.event("[工具] read_file")
        self.event("[结果] 成功")
        self.event("[模型] 第 2 次请求")
        self.answer(
            "### 运行 RecapCLI\n\n"
            "先安装依赖，再发送一次提问：\n\n"
            "```powershell\nuv sync\nuv run python main.py \"读取 README.md\"\n```\n\n"
            "- **只读工具**：使用 `read_file` 读取工作区文件。\n"
            "- **执行过程**：模型请求 → 工具执行 → 结果回传 → 回答。\n\n"
            "> 这是本地配色示例，未请求模型或读取文件。"
        )
