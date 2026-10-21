import contextlib
import io
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from archer.cli import main
from archer.ui.terminal_art import cell_width
from archer.ui.terminal_ui import TerminalUI


ANSI = re.compile(r"\x1b\[[0-9;]*m")
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
        self.assertIn("Archer>", plain)
        self.assertIn('value = "hello"', plain)
        self.assertNotIn("###", plain)
        self.assertNotIn("```", plain)
        self.assertNotIn("**重点**", plain)
        self.assertGreaterEqual(len(set(re.findall(r"38;2;\d+;\d+;\d+", text))), 4)

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
        self.assertIn("你>", ANSI.sub("", output.getvalue()))
        self.assertIn("Archer>", ANSI.sub("", output.getvalue()))

    def test_tool_success_and_failure_have_distinct_styles_and_labels(self):
        styles = []
        for event in ["[模型] 第 1 次请求", "[工具] read_file", "[结果] 成功", "[结果] 失败：文件不存在"]:
            output = io.StringIO()
            TerminalUI(output, color="always").event(event)
            self.assertIn(event, ANSI.sub("", output.getvalue()))
            styles.append(re.search(r"38;2;\d+;\d+;\d+", output.getvalue()).group())
        self.assertEqual(len(set(styles)), 4)

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
                with patch("archer.ui.terminal_ui.shutil.get_terminal_size", return_value=os.terminal_size((columns, 24))):
                    TerminalUI(output, color="always").answer("### 中文标题\n\n```python\n" + code + "\n```")
                plain = ANSI.sub("", output.getvalue())
                for line in plain.splitlines():
                    self.assertLessEqual(cell_width(line), columns - 1)
                self.assertIn(code, "".join(plain.split()))

    def test_main_sends_events_and_final_answer_through_colored_ui(self):
        from archer.core.agent import TurnResult

        def fake_agent(client, model, prompt, workspace, *, on_event, **kwargs):
            on_event("[工具] read_file")
            on_event("[结果] 成功")
            return TurnResult(ANSWER, [{"role": "user", "content": prompt},
                                       {"role": "assistant", "content": ANSWER}])

        config = {"OPENAI_API_KEY": "offline-test-key", "OPENAI_MODEL": "test-model"}
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch("archer.config.dotenv_values", return_value=config):
            with patch("archer.cli.OpenAI"), patch("archer.cli.run_agent", side_effect=fake_agent):
                with contextlib.redirect_stdout(output), tempfile.TemporaryDirectory() as workspace:
                    self.assertEqual(main(["你好", "--workspace", workspace, "--banner", "off", "--color", "always"]), 0)
        text = output.getvalue()
        self.assertIn("你> 你好", ANSI.sub("", text))
        self.assertIn("[工具] read_file", ANSI.sub("", text))
        self.assertIn("Archer>", ANSI.sub("", text))
        self.assertGreaterEqual(len(set(re.findall(r"38;2;\d+;\d+;\d+", text))), 5)


if __name__ == "__main__":
    unittest.main()
