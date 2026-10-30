"""逐次人工审批：拒绝、过期、取消和失效都不得写文件或启动进程。"""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from recapcli.cli import build_parser, main, make_approver, run_turn, show_tasks
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore
from recapcli.tools import execute_tool
from recapcli.tools.approval import (
    InteractiveApprover,
    approve_all,
    fail_closed,
    interpret_approval_line,
)
from recapcli.ui.terminal_ui import TerminalUI


def reply(text=None, *, name=None, arguments=None, call_id="call"):
    message = {"role": "assistant", "content": text}
    if name:
        message["tool_calls"] = [{"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if name else "stop", "message": message}]}


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.output = io.StringIO()
        self.ui = TerminalUI(self.output, io.StringIO(), color="never")

    def edit(self, path, old, new, approver, **extra):
        return execute_tool("edit_file", {"path": path, "old_text": old, "new_text": new}, self.workspace,
                            capabilities={"read", "write"}, approver=approver, **extra)

    def command(self, argv, approver):
        return execute_tool("run_command", {"argv": argv}, self.workspace,
                            capabilities={"read", "exec"}, approver=approver)

    def assert_no_mutation(self, *paths):
        for path in paths:
            self.assertFalse(path.exists(), path)
        self.assertFalse((self.workspace / ".recapcli" / "backups").exists())
        self.assertFalse((self.workspace / ".recapcli" / "commands").exists())

    def test_explicit_commands_are_not_confused_with_clarification(self):
        self.assertEqual(interpret_approval_line("approve"), "approved")
        self.assertEqual(interpret_approval_line("  /Approve  "), "approved")
        self.assertEqual(interpret_approval_line("批准"), "approved")
        self.assertEqual(interpret_approval_line("reject"), "rejected")
        self.assertEqual(interpret_approval_line("/拒绝"), "rejected")
        self.assertEqual(interpret_approval_line("cancel"), "cancelled")
        self.assertEqual(interpret_approval_line("取消"), "cancelled")
        for line in ("yes", "y", "ok", "同意", "看起来可以，但请把函数改名", "approve the rename", "approved", ""):
            self.assertIsNone(interpret_approval_line(line), line)

    def test_reject_does_not_write_or_start_a_process(self):
        target = self.workspace / "new.txt"
        marker = self.workspace / "ran.txt"
        lines = iter(["yes", "同意", "看起来可以，但请把名字改一下", "reject"])
        approver = InteractiveApprover(self.ui, timeout=5, reader=lambda remaining: next(lines))
        edit = self.edit("new.txt", "", "hello", approver)
        command = self.command([sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('x')"],
                                InteractiveApprover(self.ui, timeout=5, reader=lambda remaining: "拒绝"))
        self.assertEqual(edit["approval"], "rejected")
        self.assertEqual(command["approval"], "rejected")
        self.assertFalse(edit["ok"])
        self.assertFalse(command["ok"])
        self.assert_no_mutation(target, marker)
        self.assertIn("不是批准", self.output.getvalue())

    def test_expire_does_not_write_or_start_a_process(self):
        target = self.workspace / "new.txt"
        marker = self.workspace / "ran.txt"
        expired = self.edit("new.txt", "", "hello", InteractiveApprover(
            self.ui, timeout=0.2, reader=lambda remaining: None))
        command = self.command([sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('x')"],
                                lambda request: "expired")
        self.assertEqual(expired["approval"], "expired")
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(command["approval"], "expired")
        self.assert_no_mutation(target, marker)
        self.assertIn("审批已过期", self.output.getvalue())

    def test_cancel_does_not_write_or_start_a_process(self):
        target = self.workspace / "new.txt"
        marker = self.workspace / "ran.txt"
        cancelled = self.edit("new.txt", "", "hello", InteractiveApprover(
            self.ui, timeout=5, reader=lambda remaining: "cancel"))
        command = self.command([sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('x')"],
                                lambda request: "cancelled")
        closed = self.command([sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('x')"],
                               InteractiveApprover(self.ui, timeout=5, reader=self._eof))
        self.assertEqual(cancelled["approval"], "cancelled")
        self.assertEqual(command["approval"], "cancelled")
        self.assertEqual(closed["approval"], "cancelled")
        self.assert_no_mutation(target, marker)

    def test_clarification_then_explicit_approve_writes(self):
        target = self.workspace / "new.txt"
        lines = iter(["请先说明原因", "approve"])
        result = self.edit("new.txt", "", "hello\n", InteractiveApprover(
            self.ui, timeout=5, reader=lambda remaining: next(lines)))
        self.assertTrue(result["ok"], result)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
        self.assertTrue((self.workspace / ".recapcli" / "backups").is_dir())
        self.assertIn("不是批准", self.output.getvalue())

    def test_approve_then_file_change_invalidates_and_does_not_write(self):
        path = self.workspace / "app.py"
        path.write_text("print(1)\n", encoding="utf-8")
        seen = []

        def approver(request):
            seen.append(request)
            path.write_text("print(1)\n# external\n", encoding="utf-8")
            return "approved"

        events = []
        result = self.edit("app.py", "print(1)\n", "print(2)\n", approver, on_approval=events.append)
        self.assertEqual(result["approval"], "invalidated")
        self.assertEqual(result["status"], "invalidated")
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "print(1)\n# external\n")
        self.assertFalse((self.workspace / ".recapcli" / "backups").exists())
        self.assertIn("文件指纹", seen[0].summary)
        self.assertIn("差异提示", seen[0].summary)
        self.assertIn("edit_file", seen[0].summary)
        self.assertRegex(seen[0].summary, r"[0-9a-f]{64}")
        self.assertEqual(events[0]["outcome"], "invalidated")
        self.assertTrue(events[0]["files_changed"])
        path.write_text("print(1)\n", encoding="utf-8")
        other = self.workspace / "other.py"
        other.write_text("old", encoding="utf-8")
        def approve_batch(request):
            other.write_text("external", encoding="utf-8")
            return "approved"
        result = execute_tool("edit_files", {"edits": [
            {"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"},
            {"path": "other.py", "old_text": "old", "new_text": "new"},
        ]}, self.workspace, capabilities={"read", "write"}, approver=approve_batch)
        self.assertEqual(result["approval"], "invalidated")
        self.assertEqual(path.read_text(), "print(1)\n")
        self.assertEqual(other.read_text(), "external")
        self.assertFalse((self.workspace / ".recapcli/backups").exists())

    def test_approve_then_param_change_invalidates_and_does_not_write(self):
        path = self.workspace / "app.py"
        path.write_text("print(1)\n", encoding="utf-8")

        def approver(request):
            request.params["new_text"] = "hacked\n"
            return "approved"

        result = self.edit("app.py", "print(1)\n", "print(2)\n", approver)
        self.assertEqual(result["approval"], "invalidated")
        self.assertEqual(path.read_text(encoding="utf-8"), "print(1)\n")
        self.assertNotIn("hacked", path.read_text(encoding="utf-8"))
        self.assertFalse((self.workspace / ".recapcli" / "backups").exists())

    def test_approve_then_command_target_change_does_not_start(self):
        path = self.workspace / "app.py"
        path.write_text("print(1)\n", encoding="utf-8")

        def approver(request):
            path.write_text("print(9)\n", encoding="utf-8")
            return "approved"

        result = self.command([sys.executable, "app.py"], approver)
        self.assertEqual(result["approval"], "invalidated")
        self.assertEqual(path.read_text(encoding="utf-8"), "print(9)\n")
        self.assertFalse((self.workspace / ".recapcli" / "commands").exists())

    def test_capability_blocks_before_approval_even_if_yes_would_approve(self):
        def approver(request):
            raise AssertionError("能力未开启时不应进入审批")

        result = execute_tool("edit_file", {"path": "new.txt", "old_text": "", "new_text": "nope"},
                              self.workspace, approver=approver)
        denied = execute_tool("edit_file", {"path": "new.txt", "old_text": "", "new_text": "nope"},
                              self.workspace, approver=approve_all)
        command = execute_tool("run_command", {"argv": [sys.executable, "-c", "print(1)"]},
                                self.workspace, approver=approve_all)
        self.assertFalse(result["ok"])
        self.assertNotIn("approval", result)
        self.assertIn("--allow-write", result["error"])
        self.assertIn("--allow-exec", command["error"])
        self.assertFalse(denied["ok"])
        self.assert_no_mutation(self.workspace / "new.txt")

    def test_needs_approval_and_unrecognized_text_do_not_execute(self):
        missing = execute_tool("edit_file", {"path": "new.txt", "old_text": "", "new_text": "x"},
                                self.workspace, capabilities={"read", "write"})
        unrecognized = self.edit("new.txt", "", "x", lambda request: "yes")
        read = execute_tool("read_file", {"path": "new.txt"}, self.workspace, approver=self._forbid)
        self.assertEqual(missing["approval"], "needs_approval")
        self.assertEqual(missing["status"], "needs_approval")
        self.assertIn("--yes", missing["error"])
        self.assertEqual(unrecognized["approval"], "rejected")
        self.assertIn("无法识别", unrecognized["error"])
        self.assertFalse(read["ok"])
        self.assert_no_mutation(self.workspace / "new.txt")

    def test_keyboard_interrupt_and_record_failure_do_not_write(self):
        path = self.workspace / "app.py"
        path.write_text("old\n", encoding="utf-8")
        with self.assertRaises(KeyboardInterrupt):
            self.edit("app.py", "old\n", "new\n", lambda request: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(OSError):
            self.edit("app.py", "old\n", "new\n", approve_all, on_approval=self._disk_full)
        self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
        self.assertFalse((self.workspace / ".recapcli" / "backups").exists())

    def test_restore_reject_does_not_change_the_file(self):
        path = self.workspace / "app.py"
        path.write_text("print(1)\n", encoding="utf-8")
        edited = self.edit("app.py", "print(1)\n", "print(2)\n", approve_all)
        self.assertTrue(edited["ok"], edited)
        result = execute_tool("restore_file", {"backup_id": edited["backup_id"]}, self.workspace,
                              capabilities={"read", "write"}, approver=lambda request: "rejected")
        self.assertEqual(result["approval"], "rejected")
        self.assertEqual(path.read_text(encoding="utf-8"), "print(2)\n")

    def test_make_approver_selects_prompt_auto_or_fail_closed(self):
        self.assertIs(make_approver(self.ui, yes=True, interactive=True, timeout=10), approve_all)
        self.assertIs(make_approver(self.ui, yes=False, interactive=False, timeout=10), fail_closed)
        prompted = make_approver(self.ui, yes=False, interactive=True, timeout=10)
        self.assertIsInstance(prompted, InteractiveApprover)
        self.assertEqual(prompted.timeout, 10)
        help_text = build_parser().format_help()
        self.assertIn("--yes", help_text)
        self.assertIn("--approval-timeout", help_text)

    @staticmethod
    def _eof(remaining):
        raise EOFError

    @staticmethod
    def _forbid(request):
        raise AssertionError("只读工具不应审批")

    @staticmethod
    def _disk_full(payload):
        raise OSError("offline disk full")


class ApprovalFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.source = self.workspace / "app.py"
        self.source.write_text("print(1)\n", encoding="utf-8")
        self.store = SessionStore(self.workspace)
        self.session = self.store.new_session("test-model")
        self.output = io.StringIO()
        self.ui = TerminalUI(self.output, io.StringIO(), color="never")

    def run_replies(self, replies, **options):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        with OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
            answer = run_turn(client, "test-model", self.workspace, self.store, self.ui, self.session,
                              "修改 app.py", **options)
        self.requests = requests
        return answer

    def task(self):
        tasks = TaskStore(self.workspace, create=False)
        return tasks.get(tasks.list_tasks()[0]["id"])

    def test_unattended_turn_records_needs_approval_and_does_not_write(self):
        answer = self.run_replies([
            reply(name="edit_file", arguments={"path": "app.py", "old_text": "print(1)\n", "new_text": "print(2)\n"}),
            reply("没有批准"),
        ], capabilities=("read", "write"))
        self.assertEqual(answer, "没有批准")
        self.assertEqual(self.source.read_text(encoding="utf-8"), "print(1)\n")
        self.assertFalse((self.workspace / ".recapcli" / "backups").exists())
        kinds = [event["kind"] for event in self.task()["events"]
                 if event["kind"] in {"tool_start", "approval", "artifact", "tool_result"}]
        self.assertEqual(kinds, ["tool_start", "approval", "tool_result"])
        approval = next(event["data"] for event in self.task()["events"] if event["kind"] == "approval")
        self.assertEqual(approval["outcome"], "needs_approval")
        result = json.loads(self.requests[1]["messages"][-1]["content"])
        self.assertEqual(result["approval"], "needs_approval")
        show_tasks(self.ui, TaskStore(self.workspace, create=False), self.task()["id"])
        self.assertIn("needs_approval", self.output.getvalue())

    def test_granted_turn_records_approval_before_the_write(self):
        self.run_replies([
            reply(name="edit_file", arguments={"path": "app.py", "old_text": "print(1)\n", "new_text": "print(2)\n"}),
            reply("已修改"),
        ], capabilities=("read", "write"), approver=approve_all)
        self.assertEqual(self.source.read_text(encoding="utf-8"), "print(2)\n")
        kinds = [event["kind"] for event in self.task()["events"]
                 if event["kind"] in {"tool_start", "approval", "artifact", "tool_result"}]
        self.assertEqual(kinds, ["tool_start", "approval", "artifact", "tool_result"])
        approval = next(event["data"] for event in self.task()["events"] if event["kind"] == "approval")
        self.assertEqual(approval["outcome"], "approved")
        self.assertEqual(len(approval["params_sha256"]), 64)


class ApprovalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / "app.py").write_text("print(1)\n", encoding="utf-8")
        self.config = {"OPENAI_API_KEY": "offline-test-key", "OPENAI_MODEL": "test-model", "OPENAI_STREAMING": "false"}

    def run_main(self, argv, replies):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        def factory(**kwargs):
            return OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                          http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

        output, errors = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch("recapcli.config.MODELS_FILE", self.workspace / "no-models.toml"), \
                patch("recapcli.config.dotenv_values", return_value=self.config), \
                patch("recapcli.cli.OpenAI", side_effect=factory), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(argv)
        return code, requests, output.getvalue(), errors.getvalue()

    def test_timeout_flag_is_rejected_before_any_model_call(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["修改", "--approval-timeout", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_one_shot_fails_closed_unless_yes_and_capability_still_wins(self):
        base = ["--workspace", str(self.workspace), "--banner", "off", "--color", "never"]
        edit = reply(name="edit_file", arguments={"path": "app.py", "old_text": "print(1)\n", "new_text": "print(2)\n"})
        code, requests, output, errors = self.run_main(
            ["修改", *base, "--allow-write"], [edit, reply("未执行")])
        self.assertEqual(code, 0, errors)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(1)\n")
        self.assertEqual(json.loads(requests[1]["messages"][-1]["content"])["approval"], "needs_approval")
        self.assertIn("不会执行", output)
        self.assertFalse((self.workspace / ".recapcli" / "backups").exists())

        code, requests, output, errors = self.run_main(
            ["修改", *base, "--allow-write", "--yes"], [edit, reply("已改")])
        self.assertEqual(code, 0, errors)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(2)\n")

        fresh = self.workspace / "other"
        fresh.mkdir()
        code, requests, output, errors = self.run_main(
            ["修改", "--workspace", str(fresh), "--banner", "off", "--yes"],
            [reply(name="edit_file", arguments={"path": "new.txt", "old_text": "", "new_text": "x"}), reply("不能写")])
        self.assertEqual(code, 0, errors)
        self.assertFalse((fresh / "new.txt").exists())
        denied = json.loads(requests[1]["messages"][-1]["content"])
        self.assertFalse(denied["ok"])
        self.assertNotIn("approval", denied)
        self.assertIn("--allow-write", denied["error"])


if __name__ == "__main__":
    unittest.main()
