"""薄荷绿终端：响应布局、只读启动、以及接到现有 Agent 的对话。"""

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

    def task_update(self, task):
        self.events.append(("task", task))

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
    def test_terminal_accents_and_local_config_status(self):
        css = TCSS.read_text(encoding="utf-8").lower()
        self.assertIn(PALETTE["accent"].lower(), css)
        self.assertEqual(colors(dot(True)), [PALETTE["online"]])
        self.assertNotIn(PALETTE["online"], colors(dot(False)) + colors(dot()))

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
        for response in replies:
            response["usage"] = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
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
        tasks = texts(self.recording, "task")
        self.assertEqual(tasks[0]["status"], "running")
        self.assertEqual(tasks[-1]["status"], "returned")
        self.assertEqual(tasks[-1]["usage"]["total_tokens"], 50)
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
        from recapcli.tui.status import TaskStatus
        self.assertEqual(TaskStatus.from_task(texts(self.recording, "task")[-1]).modified_files, ("README.md",))
        approvals = sum(item[0] == "approval" for item in self.recording.events)

        def approve_undo():
            for _ in range(200):
                if sum(item[0] == "approval" for item in self.recording.events) > approvals:
                    self.recording.queue.put("approve")
                    return
                threading.Event().wait(0.01)

        waiter = threading.Thread(target=approve_undo)
        waiter.start()
        try:
            with patch("recapcli.tui.driver.load_config", side_effect=AssertionError("撤销不应打开模型")):
                driver.handle_line("/undo")
        finally:
            waiter.join(timeout=2)
        self.assertEqual((self.workspace / "README.md").read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(len(requests), 2)
        self.assertIsNone(driver.client)

    def test_open_restores_saved_messages(self):
        from recapcli.storage.session import SessionStore
        store = SessionStore(self.workspace)
        session = store.new_session("demo-model")
        session["messages"].append({"role": "user", "content": "昨天的问题"})
        session["messages"].append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "saved-read", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"README.md"}',
            }},
        ]})
        session["messages"].append({"role": "tool", "tool_call_id": "saved-read", "content": "hello"})
        session["messages"].append({"role": "assistant", "content": "昨天的回答"})
        store.save(session)
        driver = self.driver()
        driver.handle_line(f"/open {session['id']}")
        replaced = [item for item in self.recording.events if item[0] == "replace"]
        self.assertEqual(replaced[-1][1], [("user", "昨天的问题"), ("assistant", "昨天的回答")])
        self.assertEqual(driver.session["messages"], session["messages"])
        self.assertEqual(store.load(session["id"])["messages"], session["messages"])
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
            self.assertEqual(dispatch(["--color", "never", "--workspace", directory]), 0)
            self.assertEqual(dispatch(["--demo", "--workspace", directory]), 0)
        cli.assert_any_call(["你好", "--workspace", directory])
        cli.assert_any_call(["--models"])
        cli.assert_any_call(["--color", "never", "--workspace", directory])
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
    return "\n".join(widget.content_text for widget in app.query(".turn"))


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

    async def test_terminal_layout_gives_conversation_full_width(self):
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.pause()
            for selector in ("#brand", "#version", "#conversation", "#composer", "#send", "#status-line", "#key-hints"):
                self.assertTrue(app.query_one(selector).display)
            self.assertEqual(app.query_one("#stage").region.x, 0)
            self.assertEqual(app.query_one("#stage").region.width, 144)
            self.assertEqual(app.query_one("#prompt").size.height, 1)
            self.assertFalse(app.query("#rail, .rail-button, #inspector"))
            self.assertFalse(app.query(".bubble, #pills, #toolbar"))
            self.assertFalse(app.query("#stream-cursor"))
            welcome = app.query_one("#welcome")
            self.assertEqual(welcome.region.y, app.query_one("#conversation").region.y + 1)
            self.assertGreaterEqual(app.query(".turn").first().region.y, welcome.region.bottom)
            self.assertLessEqual(welcome.region.right, 144)
            self.assertFalse(app.query("#model-header, #welcome-purpose, #welcome-example"))
            self.assertFalse(app.query_one("#command-menu").display)
            self.assertIn("/config", str(app.query_one("#key-hints").render()))
            self.assertIs(app.focused, app.query_one("#prompt"))
            self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_narrow_terminal_retains_version_input_and_focus(self):
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query("#inspector"))
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            for selector in ("#version", "#composer", "#prompt", "#send", "#key-hints"):
                widget = app.query_one(selector)
                self.assertTrue(widget.display)
                self.assertLessEqual(widget.region.right, 60)
            self.assertIs(app.focused, app.query_one("#prompt"))

    async def test_restored_profile_and_override_remain_available_in_details(self):
        from recapcli.storage.session import SessionStore
        store = SessionStore(self.workspace)
        session = store.new_session("saved-vision-model")
        session["model_profile"] = "vision"
        session["messages"].append({"role": "user", "content": "旧问题"})
        store.save(session)
        profiles = [
            dict(name="default", model="original-model", default=True, configured=True),
            dict(name="vision", model="saved-vision-model", default=False, configured=True),
        ]
        for explicit, expected in ((None, "saved-vision-model"), ("default", "original-model")):
            with self.subTest(profile=explicit):
                options = ShellOptions(workspace=self.workspace, continue_session=True, profile=explicit)
                app = self._app(self._state(profiles=profiles), options=options)
                with patch("recapcli.tui.driver.run_turn") as turn:
                    async with app.run_test(size=(80, 24)) as pilot:
                        await pilot.pause()
                        self.assertIn(explicit or "vision", str(app._status_details()))
                        self.assertIn(expected, str(app._status_details()))
                        self.assertIsNone(app.driver.client)
                turn.assert_not_called()

    async def test_chinese_survives_markdown_styles_and_terminal_resize(self):
        from textual.geometry import Region
        app = self._app(self._state(profiles=MISSING))
        answer = ("你好！我是 RecapCLI，一个代码助手。\n\n"
                  "- **读取和分析代码文件**（Python、JavaScript、HTML 等）\n"
                  "- **搜索项目中的符号、函数或类**\n"
                  "- **浏览目录结构**")
        expected = ("一个代码助手", "读取和分析代码文件", "搜索项目中的符号、函数或类", "浏览目录结构")
        async with app.run_test(size=(144, 40)) as pilot:
            await app.replace_transcript_sync([("assistant", answer)])
            for width in (144, 80, 60):
                await pilot.resize_terminal(width, 40)
                await pilot.pause()
                widget = app.query(".assistant-turn").last()
                strips = widget.render_lines(Region(0, 0, widget.region.width, widget.region.height))
                rendered = "".join("".join(strip.text for strip in strips).split())
                for phrase in expected:
                    self.assertIn(phrase, rendered, f"终端宽度 {width}")

    async def test_status_details_use_task_facts_and_restore_focus(self):
        from recapcli.tui.status import usage_text
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.pause()
            app.driver.presenter.task_update({
                "id": "a" * 32, "status": "returned", "duration_seconds": 2.5,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                "events": [{"kind": "project_version", "data": {"dirty": True, "files": [{"path": "README.md", "status": " M"}]}},
                           {"kind": "project_rules", "data": {"source": "AGENTS.md", "status": "loaded", "bytes": 80}},
                           {"kind": "tool_result", "data": {"name": "edit_file", "result": {
                    "ok": True, "changed": True, "path": "README.md"}}}],
            })
            await pilot.pause()
            self.assertIn("120", usage_text(app._task_status))
            self.assertIn("120", str(app.query_one("#status-line").render()))
            self.assertIn("已回答", str(app.query_one("#status-line").render()))
            await pilot.press("ctrl+g")
            self.assertIn("README.md", str(app.screen.query_one("#status-body").render()))
            self.assertIn("AGENTS.md · 已加载 80 字节", str(app.screen.query_one("#status-body").render()))
            self.assertIn("开始时已有改动", str(app.screen.query_one("#status-body").render()))
            self.assertIn("120", str(app.screen.query_one("#status-body").render()))
            await pilot.press("escape")
            app._lock()
            app.begin_approval("请审批")
            self.assertIn("等待审批", str(app.query_one("#status-line").render()))
            app.end_approval()
            app._unlock()
            await pilot.press("ctrl+g")
            self.assertTrue(app.screen.query_one("#status-dialog").display)
            await pilot.press("ctrl+g")
            self.assertFalse(app.query("#status-dialog"))
            self.assertIs(app.focused, app.query_one("#prompt"))
            await app.replace_transcript_sync([])
            self.assertEqual(app._task_status.task_id, "")
            self.assertEqual(list(self.workspace.iterdir()), [])

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
                self.assertEqual(app.query_one("#prompt").text, "")
                await pilot.press("ctrl+l")
                await pilot.pause()
                self.assertEqual(len(app.query(".turn")), 0)
                self.assertIs(app.focused, app.query_one("#prompt"))
        turn.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_submit_shows_tools_and_results_without_model_request_noise(self):
        from recapcli.tui.driver import ShellDriver as Driver

        def fake_turn(client, model, workspace, store, ui, session, prompt, **kwargs):
            ui.info("任务 ID：offline-task（/task ID 查看事实）")
            ui.info("长期记忆：本轮选用 1 条，省略 0 条。")
            ui.event("[模型] 第 1 次请求")
            ui.event("[工具] read_file")
            ui.event("[结果] 成功")
            ui.event("[analysis] [模型] 第 2 次请求")
            ui.event("[review] [工具] read_file")
            ui.event("[analysis] [结果] 成功")
            ui.event("[操作] 已修改 README.md")
            ui.event("[结果] 失败：文件不存在")
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
        self.assertNotIn("任务 ID：", visible)
        self.assertNotIn("长期记忆：本轮选用", visible)
        self.assertNotIn("[模型]", visible)
        self.assertIn("[工具] read_file", visible)
        self.assertIn("[结果] 成功", visible)
        self.assertIn("[操作] 已修改 README.md", visible)
        self.assertIn("[结果] 失败：文件不存在", visible)
        self.assertNotIn("Working on the summary", visible)
        self.assertFalse(app.query("#stream-cursor"))

    async def test_long_replies_follow_the_bottom_after_layout_and_resize(self):
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(144, 40)) as pilot:
            await pilot.pause()
            conversation = app.query_one("#conversation")
            app.append_line("\n".join(f"旧回答第 {index} 行" for index in range(60)), "assistant")
            await pilot.pause()
            self.assertGreater(conversation.max_scroll_y, 0)
            self.assertEqual(conversation.scroll_y, conversation.max_scroll_y)

            conversation.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            self.assertEqual(conversation.scroll_y, 0)
            app.append_line("新回答：" + "这是一段会自动换行的中文回答。" * 160, "assistant")
            await pilot.pause()
            self.assertEqual(conversation.scroll_y, 0)
            self.assertTrue(app.query_one("#jump-latest").display)
            await pilot.press("ctrl+end")
            await pilot.pause()
            self.assertEqual(conversation.scroll_y, conversation.max_scroll_y)

            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            self.assertEqual(conversation.scroll_y, conversation.max_scroll_y)

            await app.replace_transcript_sync([
                ("user", "恢复之前的会话"),
                ("assistant", "\n".join(f"恢复回答第 {index} 行" for index in range(80))),
            ])
            await pilot.pause()
            self.assertEqual(conversation.scroll_y, conversation.max_scroll_y)
            self.assertIs(app.focused, app.query_one("#prompt"))

    async def test_stream_updates_one_answer_before_completion_and_marks_interruptions(self):
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.pause()
            presenter = app.driver.presenter
            count = len(app.query(".turn"))
            presenter.stream_update("start", "")
            presenter.stream_update("delta", "第一部分")
            await pilot.pause()
            self.assertIn("第一部分", transcript(app))
            self.assertEqual(len(app.query(".turn")), count + 1)
            presenter.stream_update("delta", "\n" + "追加内容\n" * 45)
            await pilot.pause()
            conversation = app.query_one("#conversation")
            self.assertEqual(conversation.scroll_y, conversation.max_scroll_y)
            presenter.stream_update("end", "")
            presenter.answer("第一部分\n" + "追加内容\n" * 45)
            await pilot.pause()
            self.assertEqual(len(app.query(".turn")), count + 1)
            presenter.stream_update("start", "")
            presenter.stream_update("delta", "未收齐")
            presenter.stream_update("abort", "")
            await pilot.pause()
            self.assertIn("[回答未完成]", transcript(app))

    async def test_history_and_help_commands(self):
        from recapcli.storage.session import SessionStore
        store = SessionStore(self.workspace)
        session = store.new_session("demo-model")
        session["messages"].append({"role": "user", "content": "你好"})
        store.save(session)
        app = self._app(self._state(profiles=MISSING))
        async with app.run_test(size=(144, 40)) as pilot:
            app._submit_value("/sessions")
            await wait_for(pilot, lambda: not app._busy)
            self.assertIn(session["id"], transcript(app))
            app._submit_value("/help")
            await wait_for(pilot, lambda: not app._busy)
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            menu = app.query_one("#command-menu")
            self.assertTrue(menu.display)
            self.assertIn("/open", [item[0] for item in menu.commands])
            self.assertNotIn("/memory-reject", [item[0] for item in menu.commands])
            self.assertNotIn("/memory-reject", transcript(app))
            await pilot.press("escape")
            self.assertIs(app.focused, app.query_one("#prompt"))
            self.assertFalse(menu.display)
            await pilot.press("/", "m", "o")
            self.assertEqual([item[0] for item in menu.commands], ["/model", "/models"])
            await pilot.press("down", "tab")
            self.assertEqual(app.query_one("#prompt").text, "/models ")
            self.assertFalse(menu.display)
            app.query_one("#prompt").load_text("/task")
            await pilot.pause()
            await pilot.press("down", "enter")
            self.assertEqual(app.query_one("#prompt").text, "/task ")
            self.assertIsNone(app.driver.client)
            app.query_one("#prompt").load_text("/help")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(menu.display)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_model_settings_save_and_switch_without_requesting_api(self):
        import os
        from recapcli.config import list_model_profiles, load_config
        from textual.widgets import Input
        env_file = self.workspace / ".env"
        models_file = self.workspace / "models.toml"
        models_file.write_text('# keep this comment\n[models.other]\nmodel = "other-model"\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch("recapcli.config.ENV_FILE", env_file), \
                patch("recapcli.config.MODELS_FILE", models_file), patch("recapcli.tui.driver.run_turn") as turn:
            def offline_factory(**options):
                return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(
                    lambda request: self.fail("模型设置不应发送请求")), verify=False))
            app = self._app(self._state(profiles=MISSING), client_factory=offline_factory)
            async with app.run_test(size=(100, 36)) as pilot:
                await pilot.press("f3")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#config-key", Input).value, "")
                app.screen.query_one("#config-model", Input).value = "my-test-model"
                app.screen.query_one("#config-url", Input).value = "https://offline.invalid/v1"
                app.screen.query_one("#config-key", Input).value = "fake-ui-key"
                await pilot.click("#save-config")
                await wait_for(pilot, lambda: not app._busy and app.driver.client is not None)
                self.assertEqual(load_config().model, "my-test-model")
                self.assertEqual(load_config().api_key, "fake-ui-key")
                self.assertIn("my-test-model", str(app.query_one("#key-hints").render()))
                self.assertIn("# keep this comment", models_file.read_text(encoding="utf-8"))
                self.assertNotIn("fake-ui-key", models_file.read_text(encoding="utf-8") + transcript(app) + str(list_model_profiles()))
                await pilot.press("f2")
                await pilot.pause()
                self.assertTrue(app.screen.query_one("#model-options").display)
                app.screen.query_one("#model-filter", Input).value = "other"
                await pilot.pause()
                await pilot.press("down")
                self.assertEqual(app.focused.id, "model-options")
                app.screen.query_one("#model-filter", Input).focus()
                await pilot.press("enter")
                await wait_for(pilot, lambda: not app._busy and app.driver.model == "other-model")
                await pilot.press("f2")
                await pilot.pause()
                await pilot.press("escape")
                app._busy = True
                await pilot.press("f2")
                self.assertEqual(len(app.screen_stack), 1)
                app._busy = False
            turn.assert_not_called()

    async def test_demo_is_explicit_and_does_not_call_the_model(self):
        app = self._app(demo_state(), demo=True)
        with patch("recapcli.tui.driver.run_turn") as turn:
            async with app.run_test(size=(144, 40)) as pilot:
                await pilot.pause()
                self.assertIn("不请求模型", str(app.query_one("#status-line").render()))
                self.assertEqual(len(app.query(".turn")), 2)
                self.assertTrue(app.query_one("#stream-cursor").display)
                await pilot.press(*"hi", "enter")
                await pilot.pause()
                self.assertIn("未发送给模型", transcript(app))
                self.assertFalse(app.query("#rail"))
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


if __name__ == "__main__":
    unittest.main()
