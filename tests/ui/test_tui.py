"""Violet Studio 终端壳：配色、只读启动、以及接到现有 Agent 的对话。"""

import importlib.util
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from recapcli.config import Config, ConfigError
from recapcli.core.agent import AgentLimits
from recapcli.tui import state as shell_state
from recapcli.tui.driver import ShellDriver, ShellOptions
from recapcli.tui.launch import dispatch, shell_options
from recapcli.tui.state import ShellState, load_shell_state
from recapcli.tui.theme import PALETTE, dot, render_line

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


def reply(content=None, calls=None, finish=None):
    return {
        "id": "test-completion", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": finish or ("tool_calls" if calls else "stop"),
                     "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
    }


def tool_call(call_id="call-1", path="README.md", name="read_file", arguments=None):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": arguments if arguments is not None else json.dumps({"path": path}),
    }}


class Recording:
    def __init__(self):
        self.events = []
        self.queue = __import__("queue").Queue()

    def info(self, message):
        self.events.append(("info", str(message)))

    def error(self, message):
        self.events.append(("error", str(message)))

    def event(self, message):
        self.events.append(("event", str(message)))

    def answer(self, message):
        self.events.append(("answer", str(message)))

    def show_approval(self, message):
        self.events.append(("approval", str(message)))

    def read_approval(self, timeout):
        try:
            return self.queue.get(timeout=timeout)
        except __import__("queue").Empty:
            return None

    def clear_transcript(self):
        self.events.append(("clear", ""))

    def replace_transcript(self, rows):
        self.events.append(("replace", list(rows)))

    def model_changed(self, model, ready):
        self.events.append(("model", str(model), bool(ready)))

    def disarm_approval(self):
        self.events.append(("disarm", ""))


def texts(recording, kind=None):
    return [item[1] for item in recording.events if kind is None or item[0] == kind]


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

    def test_tool_success_is_not_green_and_failure_is_error(self):
        success = render_line("[结果] 成功", "event")
        failure = render_line("[结果] 失败：文件不存在", "event")
        painted = colors(success) + colors(failure) + [str(success.style), str(failure.style)]
        self.assertNotIn(PALETTE["online"], painted)
        self.assertEqual(str(failure.style), PALETTE["error"])
        self.assertEqual(str(success.style), PALETTE["text"])
        self.assertIn("[b]hello[/b]", render_line("[b]hello[/b]", "user").plain)


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
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(shell_state, "list_model_profiles", side_effect=ConfigError("坏配置")):
            state = load_shell_state(directory)
            self.assertFalse(state.ready)
            self.assertIn("坏配置", state.notes[0])


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "README.md").write_text("hello\n", encoding="utf-8")
        self.recording = Recording()

    def options(self, **updates):
        values = dict(workspace=self.workspace, limits=AgentLimits(), max_steps=8, approval_timeout=5)
        values.update(updates)
        return ShellOptions(**values)

    def driver(self, **updates):
        return ShellDriver(self.recording, self.options(**updates), client_factory=OpenAI, ready=False)

    def test_missing_config_does_not_write_or_call_the_model(self):
        driver = self.driver()
        with patch("recapcli.tui.driver.load_config", side_effect=ConfigError("请设置 OPENAI_API_KEY")), \
                patch("recapcli.tui.driver.run_turn") as turn:
            driver.handle_line("你好")
        turn.assert_not_called()
        self.assertFalse((self.workspace / ".recapcli").exists())
        self.assertTrue(any("未发送" in item for item in texts(self.recording, "error")))
        driver.handle_line("/help")
        self.assertTrue(any("/exit" in item for item in texts(self.recording, "info")))

    def test_read_turn_shows_tool_events_and_saves_the_answer(self):
        replies = [reply(calls=[tool_call()]), reply("使用 hello 作为正文")]
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        def factory(**kwargs):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kwargs)

        config = Config("offline-test-key", "test-model", "https://offline.invalid/v1")
        driver = ShellDriver(self.recording, self.options(), client_factory=factory, ready=True)
        with patch("recapcli.tui.driver.load_config", return_value=config):
            driver.handle_line("读取 README.md")
            driver.close()
        blob = "\n".join(texts(self.recording, "event"))
        self.assertIn("[工具] read_file", blob)
        self.assertIn("[结果] 成功", blob)
        self.assertIn("使用 hello 作为正文", texts(self.recording, "answer"))
        self.assertEqual(len(requests), 2)
        saved = list((self.workspace / ".recapcli" / "sessions").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertIn("使用 hello 作为正文", saved[0].read_text(encoding="utf-8"))

    def test_write_turn_waits_for_approval_before_editing(self):
        arguments = json.dumps({"path": "README.md", "old_text": "hello\n", "new_text": "world\n"}, ensure_ascii=False)
        replies = [reply(calls=[tool_call(name="edit_file", arguments=arguments)]), reply("已改好")]
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        def factory(**kwargs):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kwargs)

        def approve_when_asked():
            for _ in range(200):
                if any(item[0] == "approval" for item in self.recording.events):
                    self.recording.queue.put("approve")
                    return
                threading.Event().wait(0.01)

        config = Config("offline-test-key", "test-model", "https://offline.invalid/v1")
        driver = ShellDriver(
            self.recording, self.options(capabilities=("read", "write")), client_factory=factory, ready=True,
        )
        waiter = threading.Thread(target=approve_when_asked)
        waiter.start()
        try:
            with patch("recapcli.tui.driver.load_config", return_value=config):
                driver.handle_line("把 README 改成 world")
                driver.close()
        finally:
            waiter.join(timeout=2)
        self.assertEqual((self.workspace / "README.md").read_text(encoding="utf-8"), "world\n")
        self.assertIn("[结果] 成功", "\n".join(texts(self.recording, "event")))
        self.assertIn("已改好", texts(self.recording, "answer"))
        self.assertTrue(any("approve" in item[1] for item in self.recording.events if item[0] == "approval"))

    def test_open_restores_saved_messages(self):
        from recapcli.storage.session import SessionStore
        store = SessionStore(self.workspace)
        session = store.new_session("demo-model")
        session["messages"].append({"role": "user", "content": "昨天的问题"})
        session["messages"].append({"role": "assistant", "content": "昨天的回答"})
        store.save(session)
        driver = self.driver()
        driver.handle_line(f"/open {session['id']}")
        replaced = [item for item in self.recording.events if item[0] == "replace"]
        self.assertIn(("user", "昨天的问题"), replaced[-1][1])
        self.assertIn(("assistant", "昨天的回答"), replaced[-1][1])
        driver.handle_line("/history")
        self.assertTrue(any("昨天的问题" in item for item in texts(self.recording, "info")))


class LaunchTests(unittest.TestCase):
    def test_multi_keeps_only_local_read(self):
        from recapcli.cli import build_parser
        with tempfile.TemporaryDirectory() as directory:
            parsed = build_parser().parse_args([
                "--mode", "multi", "--allow-write", "--allow-exec", "--allow-web", "--workspace", directory,
            ])
            options = shell_options(parsed)
        self.assertEqual(options.capabilities, ("read",))
        self.assertEqual(options.mode, "multi")

    def test_react_keeps_requested_capabilities(self):
        from recapcli.cli import build_parser
        with tempfile.TemporaryDirectory() as directory:
            parsed = build_parser().parse_args([
                "--allow-write", "--allow-exec", "--workspace", directory, "--max-steps", "4",
            ])
            options = shell_options(parsed)
        self.assertEqual(options.capabilities, ("exec", "read", "write"))
        self.assertEqual(options.max_steps, 4)

    def test_prompt_stays_on_cli_and_tty_without_prompt_opens_shell(self):
        from recapcli.cli import build_parser
        with tempfile.TemporaryDirectory() as directory, \
                patch("recapcli.tui.launch.cli_main", return_value=0) as cli, \
                patch("recapcli.tui.launch.launch_shell", return_value=0) as shell, \
                patch("recapcli.tui.launch._tty", return_value=True):
            self.assertEqual(dispatch(["你好", "--workspace", directory]), 0)
            self.assertEqual(dispatch(["--workspace", directory]), 0)
            self.assertEqual(dispatch(["--models"]), 0)
            self.assertEqual(dispatch(["--demo", "--workspace", directory]), 0)
        cli.assert_any_call(["你好", "--workspace", directory])
        cli.assert_any_call(["--models"])
        self.assertEqual(shell.call_count, 2)
        self.assertFalse(shell.call_args_list[0].kwargs["demo"])
        self.assertTrue(shell.call_args_list[1].kwargs["demo"])

    def test_without_tty_interactive_uses_cli(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("recapcli.tui.launch.cli_main", return_value=0) as cli, \
                patch("recapcli.tui.launch.launch_shell") as shell, \
                patch("recapcli.tui.launch._tty", return_value=False):
            self.assertEqual(dispatch(["--workspace", directory]), 0)
        cli.assert_called_once()
        shell.assert_not_called()

    def test_bad_step_budget_does_not_open_the_shell(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("recapcli.tui.launch._tty", return_value=True), redirect_stderr(io.StringIO()):
            code = dispatch(["--max-steps", "0", "--workspace", directory])
        self.assertEqual(code, 2)


def transcript(app):
    return "\n".join(str(widget.render()) for widget in app.query(".turn"))


async def wait_for(pilot, predicate, rounds=60):
    for _ in range(rounds):
        if predicate():
            return transcript(pilot.app)
        await pilot.pause(0.05)
    raise AssertionError(transcript(pilot.app))


@unittest.skipUnless(HAS_TEXTUAL, "需要可选依赖 textual（uv sync --extra tui）")
class ShellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def _state(self, profiles=READY, sessions=None):
        return ShellState(workspace=self.workspace, profiles=profiles, sessions=sessions or [])

    def _app(self, state=None, **options):
        from recapcli.tui.app import RecapShell
        return RecapShell(state or self._state(), **options)

    def test_only_model_ready_dot_is_green(self):
        from recapcli.tui.app import model_label
        ready = model_label(demo_state())
        missing = model_label(demo_state(MISSING))
        self.assertIn(PALETTE["online"], colors(ready))
        self.assertNotIn(PALETTE["online"], colors(missing))
        self.assertIn("demo-model", ready.plain)
        self.assertIn("未配置模型", missing.plain)

    async def test_terminal_layout_is_plain_and_input_is_single_line(self):
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.pause()
            for selector in ("#brand", "#studio", "#model", "#rail", "#conversation", "#composer", "#send"):
                self.assertTrue(app.query_one(selector).display)
            self.assertEqual(app.query_one("#rail").region.width, 5)
            self.assertEqual(app.query_one("#prompt").size.height, 1)
            self.assertEqual(len(app.query(".rail-button")), 5)
            self.assertFalse(app.query(".bubble, #pills, #toolbar"))
            self.assertFalse(app.query("#stream-cursor"))
            self.assertIs(app.focused, app.query_one("#prompt"))
            self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_narrow_terminal_retains_model_input_and_focus(self):
        app = self._app(self._state(profiles=MISSING))
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

    async def test_submit_without_config_reports_failure_and_keeps_running(self):
        app = self._app(self._state(profiles=MISSING))
        with patch("recapcli.tui.driver.load_config", side_effect=ConfigError("请设置 OPENAI_API_KEY")), \
                patch("recapcli.tui.driver.run_turn") as turn:
            async with app.run_test(size=(144, 40)) as pilot:
                await pilot.press(*"[b]hello[/b]", "enter")
                await wait_for(pilot, lambda: "未发送" in transcript(app))
                visible = transcript(app)
                self.assertIn("[b]hello[/b]", visible)
                self.assertIn("OPENAI_API_KEY", visible)
                self.assertEqual(app.query_one("#prompt").value, "")
                await pilot.press("ctrl+l")
                await pilot.pause()
                self.assertEqual(len(app.query(".turn")), 0)
                self.assertIs(app.focused, app.query_one("#prompt"))
        turn.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_submit_shows_tool_events_and_whole_answer(self):
        from recapcli.tui.driver import ShellDriver as Driver

        def fake_turn(client, model, workspace, store, ui, session, prompt, **kwargs):
            ui.event("[模型] 第 1 次请求")
            ui.event("[工具] read_file")
            ui.event("[结果] 成功")
            answer = "已读取 README"
            session["messages"].extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": answer},
            ])
            return answer

        class FakeClient:
            def __init__(self, config, client_factory=None):
                self.config = config
                self.model = config.model

            def __enter__(self):
                return self

            def __exit__(self, *error):
                return False

            def metadata(self):
                return {"profile": "default", "protocol": "chat_completions", "model": self.model,
                        "context_bytes": 262144, "capabilities": {"tools": True, "images": False, "streaming": False}}

        config = Config("offline-test-key", "test-model", "https://offline.invalid/v1")
        app = self._app(self._state())
        with patch("recapcli.tui.driver.load_config", return_value=config), \
                patch("recapcli.tui.driver.ModelClient", FakeClient), \
                patch("recapcli.tui.driver.run_turn", side_effect=fake_turn), \
                patch.object(Driver, "close", lambda self: None):
            async with app.run_test(size=(144, 40)) as pilot:
                await pilot.press(*"你好", "enter")
                visible = await wait_for(pilot, lambda: "已读取 README" in transcript(app))
        self.assertIn("[工具] read_file", visible)
        self.assertIn("[结果] 成功", visible)
        self.assertNotIn("Working on the summary", visible)
        self.assertFalse(app.query("#stream-cursor"))

    async def test_history_and_help_buttons(self):
        from recapcli.storage.session import SessionStore
        store = SessionStore(self.workspace)
        session = store.new_session("demo-model")
        session["messages"].append({"role": "user", "content": "你好"})
        store.save(session)
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.click("#rail-history")
            await pilot.pause()
            self.assertIn(session["id"], transcript(app))
            await pilot.click("#rail-help")
            await pilot.pause()
            self.assertIn("Ctrl+Q", transcript(app))
            self.assertIn("/open", transcript(app))

    async def test_demo_is_explicit_and_does_not_call_the_model(self):
        app = self._app(demo_state(), demo=True)
        with patch("recapcli.tui.driver.run_turn") as turn:
            async with app.run_test(size=(144, 40)) as pilot:
                await pilot.pause()
                self.assertIn("preview", str(app.query_one("#model").render()))
                self.assertEqual(len(app.query(".turn")), 2)
                self.assertTrue(app.query_one("#stream-cursor").display)
                await pilot.press(*"hi", "enter")
                await pilot.pause()
                self.assertIn("未发送给模型", transcript(app))
                await pilot.press("ctrl+b")
                await pilot.pause()
                self.assertFalse(app.query_one("#rail").display)
        turn.assert_not_called()


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
        self.assertIn("recapcli.cli", stderr.getvalue())

    def test_cli_module_remains_available(self):
        result = __import__("subprocess").run(
            [sys.executable, "-m", "recapcli.cli", "--help"],
            capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--session", result.stdout)
        self.assertIn("--workspace", result.stdout)


if __name__ == "__main__":
    unittest.main()
