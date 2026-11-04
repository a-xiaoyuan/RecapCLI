import contextlib
import io
import os
import re
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

from recapcli.cli import build_parser, main
from recapcli.ui.terminal import print_banner
from recapcli.ui.terminal_ui import TerminalUI


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cell_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)
ANSWER = '### 示例\n\n**重点**是 `name`。\n\n```python\nvalue = "hello"\nprint(value)\n```'


class PyCharmOutput(io.StringIO):
    def seekable(self):
        return False


class TerminalUITests(unittest.TestCase):
    def test_answer_has_markdown_styles_and_syntax_colors(self):
        output = io.StringIO()
        TerminalUI(output, color="always").answer(ANSWER)
        text = output.getvalue()
        plain = ANSI.sub("", text)
        self.assertIn("●", plain)
        self.assertIn('value = "hello"', plain)
        self.assertNotIn("###", plain)
        self.assertNotIn("```", plain)
        self.assertNotIn("**重点**", plain)
        self.assertIn("\x1b[1m", text)  # 标题与重点加粗
        # 跟随终端底色：不铺任何背景色块（代码高亮主题除外）。
        self.assertNotIn("48;2;244;230;216", text)

    def test_plain_mode_preserves_markdown_without_escape_codes(self):
        for color, env in [("never", {}), ("auto", {"NO_COLOR": "1"}), ("auto", {})]:
            with self.subTest(color=color, env=env), patch.dict(os.environ, env, clear=True):
                output = io.StringIO()
                TerminalUI(output, color=color).answer(ANSWER)
                self.assertNotIn("\x1b", output.getvalue())
                self.assertIn(ANSWER, output.getvalue())

    def test_pycharm_auto_mode_colors_both_prompt_and_answer(self):
        output = PyCharmOutput()
        with patch.dict(os.environ, {"PYCHARM_HOSTED": "1"}, clear=True):
            ui = TerminalUI(output)
            ui.user("你好")
            ui.answer("你好")
        self.assertIn("\x1b", output.getvalue())
        self.assertIn("› 你好", ANSI.sub("", output.getvalue()))
        self.assertIn("●", ANSI.sub("", output.getvalue()))

    def test_tool_success_and_failure_have_distinct_styles_and_labels(self):
        styles = {}
        for event in ["[工具] read_file", "[结果] 成功", "[结果] 失败：文件不存在", "[待办]\n☐ 接 CLI"]:
            output = io.StringIO()
            TerminalUI(output, color="always").event(event)
            self.assertIn(event, ANSI.sub("", output.getvalue()))
            styles[event] = re.search(r"\x1b\[[0-9;]*m", output.getvalue()).group()
        self.assertEqual(len(set(styles.values())), 4)

    def test_errors_only_write_to_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        TerminalUI(stdout, stderr, color="always").error("任务停止：文件不存在")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("任务停止：文件不存在", ANSI.sub("", stderr.getvalue()))
        self.assertIn("\x1b", stderr.getvalue())

    def test_user_markup_is_literal_and_control_sequences_are_removed(self):
        output = io.StringIO()
        ui = TerminalUI(output, color="always")
        ui.user("[red]原样保留[/red]")
        ui.answer("正文\x1b[31m继续\x1b]0;不能改窗口标题\x07结束")
        plain = ANSI.sub("", output.getvalue())
        self.assertIn("[red]原样保留[/red]", plain)
        self.assertNotIn("\x1b", plain)
        self.assertNotIn("\x07", plain)

    def test_narrow_answer_wraps_chinese_and_long_code_without_losing_code(self):
        code = 'print("abcdefghijklmnopqrstuvwxyz0123456789")'
        for columns in [20, 40, 80]:
            with self.subTest(columns=columns):
                output = io.StringIO()
                with patch("recapcli.ui.terminal_ui.shutil.get_terminal_size", return_value=os.terminal_size((columns, 24))):
                    TerminalUI(output, color="always").answer("### 中文标题\n\n```python\n" + code + "\n```")
                plain = ANSI.sub("", output.getvalue())
                for line in plain.splitlines():
                    self.assertLessEqual(cell_width(line), columns - 1)
                self.assertIn(code, "".join(plain.split()))

    def test_main_sends_events_and_final_answer_through_colored_ui(self):
        from recapcli.core.agent import TurnResult

        def fake_agent(client, model, prompt, workspace, *, on_event, **kwargs):
            on_event("[工具] read_file")
            on_event("[结果] 成功")
            return TurnResult(ANSWER, [{"role": "user", "content": prompt},
                                       {"role": "assistant", "content": ANSWER}])

        config = {"OPENAI_API_KEY": "offline-test-key", "OPENAI_MODEL": "test-model", "OPENAI_STREAMING": "false"}
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch("recapcli.config.dotenv_values", return_value=config):
            with patch("recapcli.cli.OpenAI"), patch("recapcli.application.runtime.run_agent", side_effect=fake_agent):
                with contextlib.redirect_stdout(output), tempfile.TemporaryDirectory() as workspace:
                    self.assertEqual(main(["你好", "--workspace", workspace, "--banner", "off", "--color", "always"]), 0)
        text = output.getvalue()
        self.assertIn("› 你好", ANSI.sub("", text))
        self.assertIn("[工具] read_file", ANSI.sub("", text))
        self.assertIn("●", ANSI.sub("", text))

    def test_banner_is_two_quiet_lines_and_can_be_turned_off(self):
        output = io.StringIO()
        print_banner(output, workspace="/tmp/recap-banner", mode="color")
        plain = ANSI.sub("", output.getvalue())
        self.assertTrue(plain.startswith("◆ RecapCLI"))
        self.assertIn("recap-banner", plain)
        self.assertLessEqual(len(plain.strip().splitlines()), 2)
        output = io.StringIO()
        print_banner(output, workspace="/tmp", mode="off")
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("--preview", build_parser().format_help())

if __name__ == "__main__":
    unittest.main()
