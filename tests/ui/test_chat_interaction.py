"""真实终端组件和 SDK 流程：多行、停止、草稿、审批与阅读位置。"""

import importlib.util
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

if importlib.util.find_spec("textual") is None:
    raise unittest.SkipTest("需要可选依赖 textual（uv sync --extra tui）")

import httpx2
from openai import OpenAI, APITimeoutError
from textual import events

from recapcli.config import Config
from recapcli.storage.tasks import TaskStore
from recapcli.tui.app import RecapShell
from recapcli.tui.driver import ShellOptions
from recapcli.tui.state import ShellState
from recapcli.tui.widgets import StatusScreen
from tests.integration.test_multimodal_streaming import chunk, sse, text_events
from tests.ui.test_tui import reply, tool_call, transcript, wait_for, rendered_text


class ChatInteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.requests = []
        self.config = Config("offline-test-key", "test-model", "https://offline.invalid/v1")

    def app(self, handler=None, **options):
        def transport(request):
            self.requests.append(json.loads(request.content))
            return handler(request) if handler else httpx2.Response(200, json=reply("完整回答"))

        def factory(**kwargs):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(transport)), **kwargs)

        state = ShellState(self.workspace, profiles=[dict(name="default", model="test-model", default=True, configured=True)])
        return RecapShell(state, options=ShellOptions(workspace=self.workspace, memory=False, **options), client_factory=factory)

    async def test_current_tool_and_busy_quit_show_in_status_line(self):
        app = self.app()
        async with app.run_test(size=(80, 24)):
            app._lock()
            app.driver.presenter.event("[工具] read_file")
            self.assertIn("read_file", str(app.query_one("#status-line").render()))
            app.action_quit()
            self.assertTrue(app.driver.quitting)
            self.assertTrue(app.driver.cancel_event.is_set())
            self.assertIn("停止中", str(app.query_one("#status-line").render()))

    async def test_ctrl_a_selects_multiline_input_and_then_the_full_conversation(self):
        app = self.app()
        draft = "第一行\n第二行，包含中文"
        answer = "完整回答\n" * 1400
        with patch("recapcli.tui.app._copy_windows_clipboard"):
            async with app.run_test(size=(80, 24)) as pilot:
                app.append_line("请解释调用链", "user")
                app.append_line(answer, "assistant")
                prompt = app.query_one("#prompt")
                prompt.load_text(draft)
                await pilot.press("ctrl+a", "ctrl+c")
                self.assertEqual(prompt.selected_text, draft)
                self.assertEqual(app.clipboard, draft)
                self.assertTrue(app.is_running)

                prompt.load_text("")
                await pilot.pause()
                await pilot.press("ctrl+a", "ctrl+c")
                self.assertIn("请解释调用链", app.clipboard)
                self.assertIn(answer.rstrip(), app.clipboard)
                self.assertNotIn("显示已截断", app.clipboard)
                self.assertTrue(app.is_running)

    async def test_copy_commands_keep_full_answers_and_do_not_request_the_model(self):
        app = self.app()
        answer = "## 完整回答\n\n" + "中文与代码 `value`\n" * 900
        with patch("recapcli.tui.app._copy_windows_clipboard"):
            async with app.run_test(size=(80, 24)) as pilot:
                app.append_line("解释代码", "user")
                app.append_line(answer, "assistant")
                await pilot.pause()
                app._submit_value("/copy")
                self.assertEqual(app.clipboard, answer)
                app._submit_value("/copy all")
                self.assertEqual(app.clipboard, "用户\n解释代码\n\nRecapCLI\n" + answer)
                self.assertEqual(self.requests, [])

    async def test_long_answer_is_rendered_without_display_truncation(self):
        app = self.app()
        answer = "开头\n" + "中间内容\n" * 1200 + "尾部唯一标记"
        async with app.run_test(size=(100, 30)) as pilot:
            app.append_line(answer, "assistant")
            await pilot.pause()
            turns = app.query(".assistant-turn")
            shown = rendered_text(turns.last())
            self.assertIn("尾部唯一标记", shown)
            self.assertNotIn("显示已截断", shown)

    async def test_tool_progress_collapses_completed_reads_into_summary(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            app._lock()
            for message in ("[模型] 第 1 次请求", "[工具] read_file · README.md", "[结果] 成功",
                            "[工具] read_file · pyproject.toml", "[结果] 成功",
                            "[工具] search_symbols", "[结果] 成功"):
                app.driver.presenter.event(message)
            await pilot.pause()
            shown = rendered_text(app._progress_widget)
            self.assertIn("模型请求 1 次", shown)
            self.assertIn("工具 3 次", shown)
            self.assertNotIn("README.md", shown)
            self.assertNotIn("pyproject.toml", shown)
            app._unlock()

    async def test_tool_progress_distinguishes_success_failure_and_unknown_outcomes(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            app._lock()
            for message in ("[工具] read_file · README.md", "[结果] 成功",
                            "[工具] run_command", "[结果] 命令已结束，退出码 1",
                            "[工具] read_file · missing.py", "[结果] 失败：文件不存在",
                            "[工具] search_code_index"):
                app.driver.presenter.event(message)
            await pilot.pause()
            progress = app._progress_widget
            self.assertEqual([row["state"] for row in progress.rows], ["done", "warning", "failed", "running"])
            app._unlock()
            self.assertEqual(progress.rows[-1]["state"], "unknown")
            self.assertIn("结果待核对", progress.rows[-1]["detail"])
            self.assertIn("文件不存在", transcript(app))

    async def test_plain_turn_keeps_request_progress_and_each_answers_own_usage(self):
        def handler(request):
            response = reply("你好，我可以阅读代码。")
            total = len(self.requests) * 100
            response["usage"] = {"prompt_tokens": total - 10, "completion_tokens": 10, "total_tokens": total}
            return httpx2.Response(200, json=response)

        self.config.streaming = False
        app = self.app(handler)
        with patch("recapcli.tui.driver.load_config", return_value=self.config):
            async with app.run_test(size=(144, 36)) as pilot:
                for question in ("你好", "继续"):
                    app.query_one("#prompt").load_text(question)
                    await pilot.press("enter")
                    await wait_for(pilot, lambda: not app._busy)
                await pilot.pause()
                answers = list(app.query(".assistant-turn"))
                self.assertEqual(len(answers), 2)
                self.assertIn("100 tokens", rendered_text(answers[0]).splitlines()[0])
                self.assertIn("200 tokens", rendered_text(answers[1]).splitlines()[0])
                self.assertFalse(app.query_one("#status-row").display)
                self.assertLess(app.query(".user-turn").first().region.width, 20)
                for progress in app.query(".progress-turn"):
                    self.assertEqual(len(progress.rows), 1)
                    self.assertEqual(progress.rows[0]["kind"], "request")
                    self.assertEqual(progress.rows[0]["state"], "done")

    async def test_waiting_time_and_timeout_remain_visible_after_bounded_retries(self):
        def handler(request):
            raise APITimeoutError(request=request)

        app = self.app(handler)
        with patch("recapcli.tui.driver.load_config", return_value=self.config):
            async with app.run_test(size=(80, 24)) as pilot:
                app._lock()
                app._busy_since -= 7
                app._tick_status()
                self.assertIn("7s", str(app.query_one("#status-line").render()))
                app._unlock()
                app.query_one("#prompt").load_text("你好")
                await pilot.press("enter")
                await wait_for(pilot, lambda: not app._busy)
                self.assertEqual(len(self.requests), 3)
                self.assertIn("[重试]", transcript(app))
                self.assertEqual(app.driver.model, "test-model")
                self.assertIn("返回数据超时", transcript(app))
                self.assertIn("/models", transcript(app))
                self.assertIn("模型请求超时", str(app.query_one("#status-line").render()))
                self.assertEqual(app._task_status.status, "failed")
                self.assertEqual(app.driver.session["messages"], [])

    async def test_multiline_paste_and_shift_enter_reach_model_and_session_intact(self):
        pasted = "    第一行缩进\n第二行堆栈\n"
        expected = pasted + "\n后续内容"
        app = self.app()
        with patch("recapcli.tui.driver.load_config", return_value=self.config):
            async with app.run_test(size=(80, 24)) as pilot:
                prompt = app.query_one("#prompt")
                app.post_message(events.Paste(pasted))
                await pilot.pause()
                await pilot.press("shift+enter", *"后续内容")
                await pilot.pause()
                self.assertEqual(prompt.text, expected)
                self.assertGreaterEqual(prompt.size.height, 3)
                self.assertEqual(len(self.requests), 0)
                await pilot.press("enter")
                await wait_for(pilot, lambda: not app._busy)
                self.assertEqual(self.requests[0]["messages"][-1]["content"], expected)
                self.assertEqual(app.driver.session["messages"][0]["content"], expected)
                self.assertEqual(prompt.text, "")
                self.assertEqual(prompt.size.height, 1)

    async def test_large_paste_stays_editable_in_a_short_narrow_window(self):
        app = self.app()
        content = "\n".join(f"    第 {i} 行日志：中文与代码混排" for i in range(100))
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt")
            app.post_message(events.Paste(content))
            await pilot.pause()
            await pilot.resize_terminal(60, 16)
            await pilot.pause()
            self.assertEqual(prompt.text, content)
            self.assertLessEqual(prompt.size.height, 6)
            self.assertLessEqual(app.query_one("#key-hints").region.bottom, 16)
            self.assertGreater(app.query_one("#conversation").size.height, 0)
            self.assertIs(app.focused, prompt)

    async def test_escape_cancels_stream_keeps_draft_and_allows_next_turn(self):
        gate, started = Event(), Event()

        class Stream(httpx2.SyncByteStream):
            def __iter__(self):
                yield ("data: " + json.dumps(chunk("还未完成的回答")) + "\n\n").encode()
                started.set()
                if not gate.wait(20):
                    raise TimeoutError("测试未释放流")
                yield ("data: " + json.dumps(chunk("后续内容")) + "\n\n").encode()
                yield b"data: [DONE]\n\n"

        def handler(request):
            if len(self.requests) == 1:
                return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
            return sse(text_events("可以继续回答"))

        self.config.streaming = True
        app = self.app(handler)
        draft = "下一条问题\n保留这行"
        with patch("recapcli.tui.driver.load_config", return_value=self.config):
            try:
                async with app.run_test(size=(100, 30)) as pilot:
                    prompt = app.query_one("#prompt")
                    prompt.load_text("开始回答")
                    await pilot.press("enter")
                    await wait_for(pilot, lambda: started.is_set() and "还未完成" in transcript(app))
                    app.post_message(events.Paste(draft))
                    await pilot.pause()
                    await pilot.press("escape")
                    self.assertTrue(app.driver.cancel_event.is_set())
                    self.assertFalse(app.driver.quitting)
                    self.assertEqual(prompt.text, draft)
                    self.assertIn("停止中", str(app.query_one("#status-line").render()))
                    gate.set()
                    await wait_for(pilot, lambda: not app._busy)
                    self.assertTrue(app.is_running)
                    self.assertEqual(prompt.text, draft)
                    self.assertIn("[回答未完成]", transcript(app))
                    self.assertEqual(TaskStore(self.workspace).list_tasks()[0]["status"], "cancelled")
                    await pilot.press("enter")
                    await wait_for(pilot, lambda: not app._busy)
                    self.assertIn("可以继续回答", transcript(app))
                    self.assertEqual(self.requests[1]["messages"][-1]["content"], draft)
            finally:
                gate.set()

    async def test_escape_before_worker_starts_is_not_cleared(self):
        app = self.app()
        with patch("recapcli.tui.driver.load_config", return_value=self.config):
            async with app.run_test(size=(80, 24)) as pilot:
                app._submit_value("立即停止，不应请求模型")
                app.action_cancel_turn()
                await wait_for(pilot, lambda: not app._busy)
                self.assertFalse(app.driver.quitting)
                self.assertEqual(self.requests, [])
                self.assertEqual(TaskStore(self.workspace).list_tasks()[0]["status"], "cancelled")

    async def test_escape_during_approval_does_not_edit_and_restores_draft(self):
        gate, started = Event(), Event()
        target = self.workspace / "README.md"
        target.write_text("hello", encoding="utf-8")

        def handler(request):
            started.set()
            if not gate.wait(20):
                raise TimeoutError("测试未释放工具响应")
            args = json.dumps({"path": "README.md", "old_text": "hello", "new_text": "world"})
            return httpx2.Response(200, json=reply(calls=[tool_call(name="edit_file", arguments=args)]))

        app = self.app(handler, capabilities=("read", "write"))
        with patch("recapcli.tui.driver.load_config", return_value=self.config):
            try:
                async with app.run_test(size=(100, 30)) as pilot:
                    app._submit_value("修改 README")
                    await wait_for(pilot, started.is_set)
                    prompt = app.query_one("#prompt")
                    prompt.load_text("之后要问的问题")
                    gate.set()
                    await wait_for(pilot, lambda: app._approval)
                    self.assertEqual(prompt.text, "")
                    await pilot.press("escape")
                    await wait_for(pilot, lambda: not app._busy)
                    self.assertEqual(target.read_text(encoding="utf-8"), "hello")
                    self.assertEqual(prompt.text, "之后要问的问题")
                    self.assertEqual(len(self.requests), 1)
                    self.assertFalse(app.driver.quitting)
            finally:
                gate.set()

    async def test_stream_respects_reading_position_and_resume_button(self):
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await app.clear_transcript_sync()
            app.stream_update("start", "")
            answer = "\n".join(f"回答第 {i} 行" for i in range(80))
            app.stream_update("delta", answer)
            await pilot.pause()
            view = app.query_one("#conversation")
            self.assertEqual(view.scroll_y, view.max_scroll_y)
            view.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            app.stream_update("delta", "\n后续段落\n" * 10)
            await pilot.pause()
            self.assertEqual(view.scroll_y, 0)
            self.assertFalse(view.following)
            self.assertTrue(app.query_one("#jump-latest").display)
            app.finish_answer(answer + "\n后续段落\n" * 10)
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            self.assertEqual(view.scroll_y, 0)
            await pilot.click("#jump-latest")
            await pilot.pause()
            self.assertTrue(view.following)
            self.assertEqual(view.scroll_y, view.max_scroll_y)
            self.assertFalse(app.query_one("#jump-latest").display)
            view.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            view.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            self.assertTrue(view.following)
            app.append_line("又有一段\n" * 10, "assistant")
            await pilot.pause()
            self.assertEqual(view.scroll_y, view.max_scroll_y)

    async def test_details_work_on_narrow_screen_escape_does_not_cancel_task(self):
        app = self.app()
        async with app.run_test(size=(60, 24)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.load_text("保留草稿")
            app._lock()
            await pilot.press("ctrl+g")
            self.assertIsInstance(app.screen, StatusScreen)
            self.assertLessEqual(app.screen.query_one("#status-dialog").region.right, 60)
            self.assertIn("test-model", str(app.screen.query_one("#status-body").render()))
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, StatusScreen)
            self.assertFalse(app.driver.cancel_event.is_set())
            self.assertEqual(prompt.text, "保留草稿")
            app._unlock()
            await pilot.press("escape")
            self.assertEqual(prompt.text, "保留草稿")
