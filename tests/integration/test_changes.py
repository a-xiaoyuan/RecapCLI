"""在已有 Git 改动的仓库中核对本轮差异、命令与分组撤销。"""

import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import httpx2
from openai import APIError, OpenAI

from recapcli.application.changes import change_report
from recapcli.application.commands import handle_command
from recapcli.application.runtime import run_turn, run_undo
from recapcli.core.agent import AgentLimits
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore
from recapcli.tools import execute_tool
from recapcli.tools.approval import approve_all
from recapcli.ui.terminal_ui import TerminalUI


def response(name=None, arguments=None):
    message = {"role": "assistant", "content": None if name else "完成"}
    if name:
        message["tool_calls"] = [{"id": "call", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}]
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "offline",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if name else "stop", "message": message}]}


@unittest.skipUnless(shutil.which("git"), "需要 Git")
class ChangeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.a = self.workspace / "a.py"
        self.b = self.workspace / "b.py"
        self.a.write_text('note = "base"\nvalue = 1\n', encoding="utf-8")
        self.b.write_text("value = 2\n", encoding="utf-8")
        (self.workspace / "old.txt").write_text("user rename\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "a.py", "b.py", "old.txt")
        self.git("-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("offline")
        self.output = io.StringIO()
        self.ui = TerminalUI(self.output, io.StringIO(), color="never")
        self.requests = []

    def git(self, *arguments):
        return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false", "-c", "maintenance.auto=false",
                               *arguments], cwd=self.workspace, capture_output=True, check=True, timeout=15).stdout

    def run_model(self, replies):
        def reply(request):
            self.requests.append(json.loads(request.content))
            item = replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return httpx2.Response(200, json=item)
        with OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(reply))) as client:
            return run_turn(client, "offline", self.workspace, self.store, self.ui, self.session, "修改并验证",
                            capabilities=("read", "write", "exec"), approver=approve_all, memory_enabled=False,
                            limits=AgentLimits(max_retries=0))

    def latest_task(self):
        tasks = TaskStore(self.workspace, create=False)
        return tasks.get(tasks.list_tasks(limit=1)[0]["id"])

    def test_dirty_git_round_trip_preserves_user_changes_and_index(self):
        self.a.write_text('note = "user staged"\nvalue = 1\n', encoding="utf-8")
        self.git("add", "a.py")
        self.a.write_text(self.a.read_text(encoding="utf-8") + 'draft = "user unstaged"\n', encoding="utf-8")
        self.git("mv", "old.txt", "renamed.txt")
        (self.workspace / "untracked.txt").write_text("user work", encoding="utf-8")
        before_a, before_b = self.a.read_bytes(), self.b.read_bytes()
        index_before = self.git("diff", "--cached", "--binary")
        command = [sys.executable, "-c", "import a,b; assert a.value == 10 and b.value == 20; print('checked')"]
        self.run_model([
            response("edit_files", {"edits": [
                {"path": "a.py", "old_text": "value = 1", "new_text": "value = 10"},
                {"path": "b.py", "old_text": "value = 2", "new_text": "value = 20"}]}),
            response("run_command", {"argv": command}), response()])
        task = self.latest_task()
        report = change_report(task)
        initial = {item["path"]: item for item in report["git"]["files"]}
        self.assertEqual(initial["a.py"]["status"], "MM")
        self.assertEqual(initial["renamed.txt"]["original_path"], "old.txt")
        self.assertEqual(initial["untracked.txt"]["status"], "??")
        self.assertEqual({item["path"]: item["preexisting"] for item in report["files"]}, {"a.py": True, "b.py": False})
        self.assertEqual(report["commands"][0]["argv"], command)
        self.assertEqual(report["commands"][0]["returncode"], 0)
        group = report["groups"][0]
        self.assertIn("+value = 10", group["diff"])
        self.assertNotIn('-note = "user staged"', group["diff"])
        self.assertNotIn('-draft = "user unstaged"', group["diff"])
        handle_command("/diff " + task["id"], self.ui, self.store, self.session, "offline")
        self.assertIn("/undo " + group["id"], self.output.getvalue())
        count = len(self.requests)
        handle_command("/undo " + group["id"], self.ui, self.store, self.session, "offline",
                       capabilities=("read", "write"), approver=approve_all)
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.a.read_bytes(), before_a)
        self.assertEqual(self.b.read_bytes(), before_b)
        self.assertEqual(self.git("diff", "--cached", "--binary"), index_before)
        self.assertEqual((self.workspace / "untracked.txt").read_text(encoding="utf-8"), "user work")
        self.assertEqual(self.latest_task()["mode"], "undo")
        self.assertEqual(self.latest_task()["model_requests"], 0)
        self.assertIn("本地操作事实", self.session["messages"][-1]["content"])

    def test_explicit_undo_group_keeps_later_groups_and_refuses_concurrent_changes(self):
        def edit(path, old, new):
            return execute_tool("edit_file", {"path": path, "old_text": old, "new_text": new}, self.workspace,
                                capabilities=("read", "write"), approver=approve_all)["backup_id"]
        a_group = edit("a.py", "value = 1", "value = 10")
        b_group = edit("b.py", "value = 2", "value = 20")
        denied = run_undo(self.store, self.ui, self.session, "offline", group_id=a_group, approver=approve_all)
        self.assertFalse(denied["ok"])
        self.assertIn("value = 10", self.a.read_text(encoding="utf-8"))
        result = run_undo(self.store, self.ui, self.session, "offline", group_id=a_group,
                          capabilities=("read", "write"), approver=approve_all)
        self.assertTrue(result["ok"])
        self.assertIn("value = 1\n", self.a.read_text(encoding="utf-8"))
        self.assertIn("value = 20", self.b.read_text(encoding="utf-8"))
        self.b.write_text("user follow-up\n", encoding="utf-8")
        refused = run_undo(self.store, self.ui, self.session, "offline", group_id=b_group,
                           capabilities=("read", "write"), approver=approve_all)
        self.assertFalse(refused["ok"])
        self.assertEqual(self.b.read_text(encoding="utf-8"), "user follow-up\n")

    def test_model_failure_keeps_reviewable_diff_and_local_undo(self):
        before = self.a.read_bytes()
        with self.assertRaises(APIError):
            self.run_model([response("edit_file", {"path": "a.py", "old_text": "value = 1", "new_text": "value = 10"}),
                            httpx2.ConnectError("offline failure")])
        task = self.latest_task()
        self.assertEqual(task["status"], "failed")
        group = change_report(task)["groups"][0]
        self.assertIn("+value = 10", group["diff"])
        self.assertIn("/diff", self.output.getvalue())
        result = run_undo(self.store, self.ui, self.session, "offline", group_id=group["id"],
                          capabilities=("read", "write"), approver=approve_all)
        self.assertTrue(result["ok"])
        self.assertEqual(self.a.read_bytes(), before)
        self.assertEqual(len(self.requests), 2)
