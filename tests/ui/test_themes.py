"""配色主题：取色完整、名称解析、启动顺序、/theme 切换与保存。"""

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from recapcli.application.commands import handle_command
from recapcli.config import load_ui_settings, save_ui_settings
from recapcli.tui import theme
from recapcli.tui.theme import (DEFAULT_THEME, PALETTE, THEMES, apply_palette, css_variables, initial_theme,
                                parse_theme)

HEX = re.compile(r"^#[0-9a-f]{6}$")


class ThemeDataTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(apply_palette, *DEFAULT_THEME)

    def test_every_family_has_complete_light_and_dark(self):
        keys = set(THEMES["catppuccin"][1]["dark"])
        for family, (_, modes) in THEMES.items():
            self.assertEqual(set(modes), {"light", "dark"}, family)
            for mode, scheme in modes.items():
                with self.subTest(family=family, mode=mode):
                    self.assertEqual(set(scheme), keys)
                    for key, value in scheme.items():
                        values = value if key == "gradient" else (value,)
                        self.assertTrue(all(HEX.match(item) for item in values), (key, value))

    def test_default_is_catppuccin_light(self):
        self.assertEqual(DEFAULT_THEME, ("catppuccin", "light"))

    def test_parse(self):
        self.assertEqual(parse_theme("catppuccin"), ("catppuccin", "light"))
        self.assertEqual(parse_theme(" Gruvbox:DARK "), ("gruvbox", "dark"))
        with self.assertRaisesRegex(ValueError, "未知主题"):
            parse_theme("solarized")
        with self.assertRaisesRegex(ValueError, "light 或 dark"):
            parse_theme("github:dim")

    def test_apply_updates_palette_in_place(self):
        same = PALETTE
        apply_palette("gruvbox", "dark")
        self.assertIs(theme.PALETTE, same)
        self.assertEqual(PALETTE["accent"], "#83a598")
        self.assertEqual(PALETTE["text"], "default")
        self.assertEqual(theme.GRADIENT[0], "#fb4934")
        self.assertEqual(css_variables()["recap-accent"], "#83a598")
        self.assertNotIn("recap-text", css_variables())


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = Path(directory.name) / "ui.json"
        patcher = patch("recapcli.config.UI_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_argument_beats_saved_beats_default(self):
        self.assertEqual(initial_theme(), DEFAULT_THEME)
        save_ui_settings(theme="kanagawa:dark")
        self.assertEqual(initial_theme(), ("kanagawa", "dark"))
        self.assertEqual(initial_theme("github"), ("github", "light"))

    def test_broken_file_falls_back_to_default(self):
        self.file.write_text("{不是 JSON", encoding="utf-8")
        self.assertEqual(load_ui_settings(), {})
        self.assertEqual(initial_theme(), DEFAULT_THEME)
        self.file.write_text(json.dumps({"theme": "solarized"}), encoding="utf-8")
        self.assertEqual(initial_theme(), DEFAULT_THEME)

    def test_save_merges(self):
        save_ui_settings(other=1)
        save_ui_settings(theme="github:dark")
        self.assertEqual(load_ui_settings(), {"other": 1, "theme": "github:dark"})


class LineCliTests(unittest.TestCase):
    def test_theme_command_points_to_tui(self):
        class UI:
            def __init__(self):
                self.lines = []

            def info(self, text):
                self.lines.append(text)

            error = info

        ui = UI()
        handle_command("/theme gruvbox", ui, None, {"messages": []}, "m")
        self.assertIn("终端界面", ui.lines[0])


class AppThemeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file = self.root / "ui.json"
        patcher = patch("recapcli.config.UI_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(apply_palette, *DEFAULT_THEME)

    def _app(self, **options):
        from recapcli.tui.app import RecapShell
        from recapcli.tui.driver import ShellOptions
        from recapcli.tui.state import ShellState
        return RecapShell(ShellState(workspace=self.root, profiles=[], sessions=[]),
                          options=ShellOptions(workspace=self.root, **options))

    async def test_theme_option_and_live_switch(self):
        app = self._app(theme="rose-pine:dark")
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            self.assertEqual(theme.CURRENT_THEME, ("rose-pine", "dark"))
            self.assertEqual(app.theme, "recap-rose-pine-dark")
            self.assertEqual(app.get_css_variables()["recap-accent"], "#c4a7e7")
            app._submit_value("/theme gruvbox:light")
            await pilot.pause()
            self.assertEqual(theme.CURRENT_THEME, ("gruvbox", "light"))
            self.assertEqual(app.theme, "recap-gruvbox-light")
            self.assertEqual(app.get_css_variables()["recap-accent"], "#076678")
            self.assertEqual(load_ui_settings()["theme"], "gruvbox:light")
            app._submit_value("/theme 不存在")
            await pilot.pause()
            self.assertEqual(theme.CURRENT_THEME, ("gruvbox", "light"))
            app._submit_value("/theme")
            await pilot.pause()
            shown = "\n".join(line.content_text for line in app.query(".info-turn"))
            self.assertIn("当前主题：Gruvbox · 浅色", shown)
            self.assertIn("tokyo-night", shown)


if __name__ == "__main__":
    unittest.main()
