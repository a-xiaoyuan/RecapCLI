"""审批菜单、工具逐行显示、差异渲染、输入历史与 @ 文件补全。"""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from recapcli.tui.app import RecapShell
from recapcli.tui.state import ShellState, workspace_files
from recapcli.tui.theme import render_approval, render_line
from recapcli.tui.widgets import ToolProgress

READY = [{"name": "default", "model": "demo-model", "protocol": "chat_completions", "default": True, "configured": True}]
EDIT_SUMMARY = "\n".join([
    "[审批] 需要明确批准后才会执行", "工具：edit_file", "工作区：C:/secret-place",
    "参数：" + json.dumps({"path": "a.py", "old_text": "x", "new_text": "y"}),
    "文件指纹：", "a.py deadbeefdeadbeef", "差异提示：",
    "--- a/a.py", "+++ b/a.py", "@@ -1 +1 @@", "-x", "+y",
    "请输入 approve、reject 或 cancel。其他文字不会被当作批准。", "输入 always 可在本会话允许文件编辑。",
])
COMMAND_SUMMARY = "\n".join([
    "[审批] 需要明确批准后才会执行", "工具：run_command", "工作区：C:/w",
    "参数：" + json.dumps({"argv": ["python", "-m", "unittest"], "cwd": "tests"}),
    "文件指纹：", "无相关文件；批准只绑定工作区与最终参数", "差异提示：", "在已授权目录运行 argv",
    "请输入 approve、reject 或 cancel。其他文字不会被当作批准。",
])


class RenderingTests(unittest.TestCase):
    def test_edit_approval_shows_diff_and_hides_fingerprints(self):
        shown = render_approval(EDIT_SUMMARY).plain
        self.assertIn("需要批准 · edit_file", shown)
        self.assertIn("-x\n+y", shown)
        for hidden in ("deadbeef", "secret-place", "--- a/a.py", "请输入"):
            self.assertNotIn(hidden, shown)

    def test_command_approval_shows_the_command(self):
        shown = render_approval(COMMAND_SUMMARY).plain
        self.assertIn("$ python -m unittest  （tests）", shown)
        self.assertNotIn("在已授权目录", shown)

    def test_unknown_approval_text_is_kept(self):
        self.assertIn("请审批", render_approval("请审批").plain)

    def test_events_drop_internal_tags(self):
        self.assertEqual(render_line("[操作] 已修改 a.py", "event").plain, "⎿ 已修改 a.py")
        self.assertEqual(render_line("[差异]\n--- a\n+++ b\n-x\n+y", "event").plain, "-x\n+y")
        self.assertEqual(render_line("[命令输出]\nok", "event").plain, "ok")


class ToolLinesTests(unittest.TestCase):
    def test_long_turn_keeps_recent_tools_and_every_failure(self):
        progress = ToolProgress()
        with patch.object(progress, "update") as update:
            for index in range(12):
                progress.observe(f"[工具] read_file · f{index}.py")
                progress.observe("[结果] 失败：不存在" if index == 0 else "[结果] 成功")
        shown = update.call_args.args[0].plain
        self.assertIn("f0.py", shown)
        self.assertIn("不存在", shown)
        self.assertIn("f11.py", shown)
        self.assertNotIn("f2.py", shown)
        self.assertIn("还有 3 次工具调用", shown)
        self.assertIn("工具 12 次", shown)


class WorkspaceFilesTests(unittest.TestCase):
    def test_credentials_are_not_offered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("", encoding="utf-8")
            (root / ".env").write_text("KEY=1", encoding="utf-8")
            with patch("recapcli.tui.state.subprocess.run", side_effect=OSError):
                self.assertEqual(workspace_files(root), ["main.py"])


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = RecapShell(ShellState(workspace=Path(directory.name), profiles=READY, sessions=[]))

    def prompt(self):
        return self.app.query_one("#prompt")

    def menu(self):
        return self.app.query_one("#approval-menu")

    async def test_menu_offers_session_rule_and_restores_draft(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            self.prompt().load_text("下一条问题")
            self.app.begin_approval(EDIT_SUMMARY)
            await pilot.pause()
            self.assertEqual(self.menu().values, ["approve", "always", "reject"])
            self.app._approval_since -= 1
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(self.app.approval_queue.get_nowait(), "always")
            self.assertFalse(self.menu().display)
            self.assertFalse(self.app._approval)
            self.assertEqual(self.prompt().text, "下一条问题")

    async def test_keys_right_after_the_menu_opens_do_not_decide(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            self.app.begin_approval(COMMAND_SUMMARY)
            self.app._approval_since = time.monotonic() + 60  # 测试机再慢也停留在保护时间内。
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(self.app.approval_queue.empty())
            self.assertTrue(self.app._approval)

    async def test_arrow_selection_and_typed_answers(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            self.app.begin_approval(COMMAND_SUMMARY)
            await pilot.pause()
            self.assertEqual(self.menu().values, ["approve", "reject"])
            self.app._approval_since -= 1
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertEqual(self.app.approval_queue.get_nowait(), "reject")
            self.app.begin_approval(COMMAND_SUMMARY)
            await pilot.pause()
            self.prompt().load_text("随便说说")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.app.approval_queue.get_nowait(), "随便说说")
            self.assertTrue(self.app._approval, "不是审批答复时继续等待")

    async def test_history_recall_keeps_draft(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            prompt = self.prompt()
            prompt.remember("第一条")
            prompt.remember("/tasks")
            prompt.load_text("草稿")
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "/tasks")
            self.assertFalse(self.app.query_one("#command-menu").display)
            await pilot.press("up")
            self.assertEqual(prompt.text, "第一条")
            await pilot.press("up")
            self.assertEqual(prompt.text, "第一条")
            await pilot.press("down", "down")
            self.assertEqual(prompt.text, "草稿")

    async def test_mention_inserts_path_in_place(self):
        files = ["README.md", "recapcli/core/agent.py"]
        with patch("recapcli.tui.app.workspace_files", return_value=files):
            async with self.app.run_test(size=(100, 30)) as pilot:
                await pilot.press("看", "space", "@", "a", "g")
                await pilot.pause()
                menu = self.app.query_one("#command-menu")
                self.assertEqual([item[0] for item in menu.commands], ["@recapcli/core/agent.py"])
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(self.prompt().text, "看 @recapcli/core/agent.py ")
                self.assertFalse(menu.display)

    async def test_image_reference_is_not_a_mention(self):
        with patch("recapcli.tui.app.workspace_files", return_value=["image:a.png"]):
            async with self.app.run_test(size=(100, 30)) as pilot:
                self.prompt().load_text("@image:a")
                self.prompt().move_cursor(self.prompt().document.end)
                await pilot.press("p")
                await pilot.pause()
                self.assertFalse(self.app.query_one("#command-menu").display)


if __name__ == "__main__":
    unittest.main()
