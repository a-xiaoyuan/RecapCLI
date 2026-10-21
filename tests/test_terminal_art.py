import contextlib
import io
import os
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from archer.cli import main
from archer.ui.terminal_art import cell_width, fit_text, print_banner


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Console(io.StringIO):
    """可声明 TTY/编码的输出流，写入时实际验证编码。"""

    def __init__(self, tty=True, encoding="utf-8"):
        super().__init__()
        self.tty = tty
        self.codec = encoding

    @property
    def encoding(self):
        return self.codec

    def isatty(self):
        return self.tty

    def seekable(self):
        return False

    def write(self, text):
        text.encode(self.encoding)
        return super().write(text)


class BannerTests(unittest.TestCase):
    def render(self, columns=80, lines=24, stream=None, env=None, mode="auto", workspace=None):
        stream = Console() if stream is None else stream
        with patch.dict(os.environ, env or {}, clear=True):
            with patch("archer.ui.terminal_art.shutil.get_terminal_size", return_value=os.terminal_size((columns, lines))):
                print_banner(stream, workspace=workspace or Path.cwd(), preview=True, mode=mode)
        return stream.getvalue()

    def test_banner_fits_windows_of_different_widths_and_heights(self):
        workspace = Path.cwd() / ("中文目录" * 12) / "项目末尾"
        for columns, lines in [(40, 20), (40, 24), (55, 24), (58, 24), (75, 24),
                               (76, 24), (80, 24), (120, 40), (80, 16), (40, 12),
                               (20, 12), (4, 8), (1, 4)]:
            with self.subTest(columns=columns, lines=lines):
                output = ANSI.sub("", self.render(columns, lines, workspace=workspace))
                visible = output.splitlines()
                self.assertLessEqual(len(visible), max(1, lines - 3))
                for line in visible:
                    self.assertLessEqual(cell_width(line), max(1, columns - 1))
                frame = [line for line in visible if line.startswith(("╭", "│", "╰"))]
                self.assertLessEqual(len({cell_width(line) for line in frame}), 1)

    def test_narrow_window_preserves_character_and_project_name(self):
        output = self.render(40, 24, workspace=Path.cwd() / ("很长的路径" * 10) / "项目末尾")
        self.assertIn("\x1b[38;2;", output)
        self.assertIn("▀", output)
        self.assertIn("项目末尾", output)

    def test_banner_keeps_ghost_compact(self):
        visible = ANSI.sub("", self.render(80, 24)).splitlines()
        self.assertIn("▀", "\n".join(visible))
        # 幽灵最大 16 列：80x24 下占 8 行像素画，整个面板不超过 13 行。
        self.assertLessEqual(len(visible), 13)

    def test_short_window_keeps_character_when_it_can_fit(self):
        self.assertIn("▀", self.render(80, 16))
        output = self.render(80, 10)
        self.assertNotIn("\x1b", output)
        self.assertIn("Archer", output)

    def test_pycharm_console_can_display_art_without_reporting_tty(self):
        output = self.render(stream=Console(tty=False), env={"PYCHARM_HOSTED": "1"})
        self.assertIn("▀", output)
        self.assertIn("\x1b[38;2;", output)

    def test_redirected_output_stays_plain_even_with_pycharm_environment(self):
        output = self.render(stream=io.StringIO(), env={"PYCHARM_HOSTED": "1"})
        self.assertNotIn("\x1b", output)
        self.assertIn("Archer", output)

    def test_disabled_color_and_dumb_terminal_stay_plain(self):
        for env in [{"NO_COLOR": ""}, {"TERM": "dumb"}]:
            with self.subTest(env=env):
                self.assertNotIn("\x1b", self.render(env=env))

    def test_explicit_modes_override_detection_or_hide_banner(self):
        self.assertIn("▀", self.render(stream=io.StringIO(), env={"NO_COLOR": "1"}, mode="color"))
        self.assertNotIn("\x1b", self.render(mode="plain"))
        self.assertEqual(self.render(mode="off"), "")

    def test_legacy_ascii_stream_does_not_crash_on_art_or_chinese(self):
        output = self.render(stream=Console(encoding="ascii"), workspace=Path.cwd() / "中文项目")
        self.assertIn("Archer", output)
        self.assertTrue(output.isascii())
        self.assertNotIn("\x1b", output)

    def test_tiny_text_width_and_combining_characters_fit(self):
        for text in ["中文路径/README.md", "e\u0301项目", "👩\u200d💻项目"]:
            for width in range(12):
                for middle in (True, False):
                    with self.subTest(text=text, width=width, middle=middle):
                        self.assertEqual(cell_width(fit_text(text, width, middle=middle)), width)

    def test_workspace_control_characters_are_not_sent_to_terminal(self):
        output = self.render(workspace=Path.cwd() / "资料\x1b[31m\n伪造输出")
        visible = ANSI.sub("", output)
        self.assertNotIn("\x1b", visible)
        self.assertNotIn("\n伪造输出", visible)

    def test_missing_version_metadata_does_not_block_startup(self):
        with patch("archer.ui.terminal_art.tomllib.load", side_effect=KeyError("project")):
            self.assertIn("Archer", self.render())

    def test_windows_console_failure_uses_plain_output(self):
        with patch("archer.ui.terminal_art._windows_ansi", return_value=False):
            self.assertNotIn("\x1b", self.render())

    def test_windows_console_mode_is_restored_even_when_writing_fails(self):
        restore = Mock()
        output = Console()
        with patch("archer.ui.terminal_art._windows_ansi", return_value=restore), patch.object(output, "write", side_effect=OSError("closed")):
            with self.assertRaises(OSError):
                self.render(stream=output)
        restore.assert_called_once_with()

    def test_cli_preview_never_reads_model_config_or_creates_client(self):
        with patch("archer.config.dotenv_values") as dotenv, patch("archer.cli.OpenAI") as client:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--preview", "--banner", "color"]), 0)
        dotenv.assert_not_called()
        client.assert_not_called()
        self.assertIn("Archer", output.getvalue())


if __name__ == "__main__":
    unittest.main()
