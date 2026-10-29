"""Violet Studio 极简终端壳：配色约束、只读状态加载与布局交互冒烟。不请求模型。"""

import importlib.util
import io
from contextlib import redirect_stderr
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from recapcli.tui import state as shell_state
from recapcli.tui.state import ShellState, load_shell_state
from recapcli.tui.theme import PALETTE, dot

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None
TCSS = Path(shell_state.__file__).with_name("violet.tcss")

READY = [{"name": "default", "model": "demo-model", "protocol": "chat_completions", "default": True, "configured": True}]
MISSING = [{"name": "default", "model": "", "protocol": "chat_completions", "default": True, "configured": False}]


def demo_state(profiles=READY, sessions=None):
    return ShellState(workspace=Path("."), profiles=profiles, sessions=sessions or [
        {"id": "20261029-152210-a1f3", "messages": 4, "updated_at": "2026-10-29T15:40:02+08:00", "model": "demo-model"},
        {"id": "20261027-110525-7c02", "messages": 6, "updated_at": "2026-10-27T11:45:27+08:00", "model": "demo-model"},
    ])


def colors(text):
    return [str(span.style) for span in text.spans]


class PaletteTests(unittest.TestCase):
    def test_green_is_reserved_for_status_dot(self):
        css = TCSS.read_text(encoding="utf-8").lower()
        self.assertNotIn(PALETTE["online"].lower(), css)
        for green in ("green", "$success", "#00ff00"):
            self.assertNotIn(green, css)
        self.assertEqual(colors(dot(True)), [PALETTE["online"]])
        self.assertNotIn(PALETTE["online"], colors(dot(False)) + colors(dot()))

    def test_no_heavy_borders(self):
        css = TCSS.read_text(encoding="utf-8")
        for heavy in ("border: heavy", "border: thick", "border: double", "border: tall", "border: wide"):
            self.assertNotIn(heavy, css)


class StateTests(unittest.TestCase):
    def test_new_workspace_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(shell_state, "list_model_profiles", return_value=READY):
            state = load_shell_state(directory)
            self.assertEqual(state.sessions, [])
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertTrue(state.ready)

    def test_existing_sessions_are_listed(self):
        from recapcli.storage.session import SessionStore
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(shell_state, "list_model_profiles", return_value=MISSING):
            store = SessionStore(directory)
            session = store.new_session("demo-model")
            session["messages"].append({"role": "user", "content": "你好"})
            store.save(session)
            state = load_shell_state(directory)
            self.assertEqual([item["id"] for item in state.sessions], [session["id"]])
            self.assertFalse(state.ready)

    def test_config_errors_become_notes(self):
        from recapcli.config import ConfigError
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(shell_state, "list_model_profiles", side_effect=ConfigError("坏配置")):
            state = load_shell_state(directory)
            self.assertFalse(state.ready)
            self.assertIn("坏配置", state.notes[0])


@unittest.skipUnless(HAS_TEXTUAL, "需要可选依赖 textual（uv sync --extra tui）")
class ShellTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, state=None, **options):
        from recapcli.tui.app import RecapShell
        return RecapShell(state or demo_state(), **options)

    def test_only_model_ready_dot_is_green(self):
        from recapcli.tui.app import model_label
        ready = model_label(demo_state())
        missing = model_label(demo_state(MISSING))
        self.assertIn(PALETTE["online"], colors(ready))
        self.assertNotIn(PALETTE["online"], colors(missing))
        self.assertIn("demo-model", ready.plain)
        self.assertIn("未配置模型", missing.plain)

    async def test_terminal_layout_is_plain_and_input_is_single_line(self):
        app = self._app()
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.pause()
            for selector in ("#brand", "#studio", "#model", "#rail", "#conversation", "#composer", "#send"):
                self.assertTrue(app.query_one(selector).display)
            self.assertEqual(app.query_one("#rail").region.width, 5)
            self.assertEqual(app.query_one("#prompt").size.height, 1)
            self.assertEqual(len(app.query(".rail-button")), 5)
            self.assertFalse(app.query(".bubble, #pills, #toolbar"))
            self.assertIs(app.focused, app.query_one("#prompt"))

    async def test_narrow_terminal_retains_model_input_and_focus(self):
        app = self._app()
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#studio").display)
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            for selector in ("#model", "#composer", "#prompt", "#send"):
                widget = app.query_one(selector)
                self.assertTrue(widget.display)
                self.assertLessEqual(widget.region.right, 60)
            self.assertIs(app.focused, app.query_one("#prompt"))

    async def test_submit_is_literal_local_preview_and_clear_preserves_focus(self):
        app = self._app()
        with patch("recapcli.core.agent.run_agent") as agent:
            async with app.run_test(size=(144, 40)) as pilot:
                await pilot.press(*"[b]hello[/b]", "enter")
                await pilot.pause()
                turns = [str(widget.render()) for widget in app.query(".turn")]
                self.assertIn("[b]hello[/b]", turns[-2])
                self.assertIn("未发送给模型", turns[-1])
                self.assertEqual(app.query_one("#prompt").value, "")
                await pilot.press("ctrl+l")
                await pilot.pause()
                self.assertEqual(len(app.query(".turn")), 0)
                self.assertIs(app.focused, app.query_one("#prompt"))
        agent.assert_not_called()

    async def test_send_new_history_and_help_buttons(self):
        app = self._app()
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.press(*"hi")
            await pilot.click("#send")
            await pilot.pause()
            self.assertIn("hi", str(app.query(".turn")[-2].render()))
            await pilot.click("#rail-new")
            await pilot.pause()
            self.assertEqual(len(app.query(".turn")), 0)
            await pilot.click("#rail-history")
            await pilot.pause()
            self.assertIn("20261029", str(app.query(".turn")[-1].render()))
            await pilot.click("#rail-help")
            await pilot.pause()
            self.assertIn("Ctrl+Q", str(app.query(".turn")[-1].render()))

    async def test_demo_is_explicit_and_has_cursor_without_requests(self):
        app = self._app(demo=True)
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.pause()
            self.assertIn("preview", str(app.query_one("#model").render()))
            self.assertEqual(len(app.query(".turn")), 2)
            self.assertTrue(app.query_one("#stream-cursor").display)
            await pilot.press("ctrl+b")
            await pilot.pause()
            self.assertFalse(app.query_one("#rail").display)


class EntryTests(unittest.TestCase):
    def test_missing_textual_prints_install_hint(self):
        from recapcli.tui.__main__ import main
        saved = {name: module for name, module in sys.modules.items()
                 if name == "textual" or name.startswith("textual.") or name == "recapcli.tui.app"}
        blocked = {name: None for name in saved if name.startswith("textual")} | {"textual": None}
        for name in saved:
            sys.modules.pop(name, None)
        stderr = io.StringIO()
        try:
            with patch.dict(sys.modules, blocked), redirect_stderr(stderr), \
                    tempfile.TemporaryDirectory() as directory:
                sys.modules.pop("recapcli.tui.app", None)
                code = main(["--workspace", directory])
        finally:
            sys.modules.update(saved)
        self.assertEqual(code, 2)
        self.assertIn("uv sync --extra tui", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
