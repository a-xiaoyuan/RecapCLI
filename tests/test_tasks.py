import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx2
from openai import APIError, OpenAI

from revocli.cli import handle_command, main, run_turn, show_tasks
from revocli.tools.approval import approve_all
from revocli.core.agent import AgentLimits
from revocli.storage.session import SessionStore
from revocli.storage.tasks import TaskStore, _process_identity, project_version
from revocli.ui.terminal_ui import TerminalUI


def reply(text=None, *, name=None, arguments=None, call_id="call", usage=None):
    message = {"role": "assistant", "content": text}
    if name:
        message["tool_calls"] = [{"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
        }}]
    return {"id": "offline-response", "object": "chat.completion", "created": 0,
            "model": "test-model", "usage": usage,
            "choices": [{"index": 0, "finish_reason": "tool_calls" if name else "stop", "message": message}]}


def document(**data):
    return reply(json.dumps(data, ensure_ascii=False))


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.tasks = TaskStore(self.workspace)

    def cleanup(self):
        # Git for Windows 退出后目录句柄偶有短暂延迟；永久占用仍须使测试失败。
        for attempt in range(10):
            try:
                self.temp.cleanup()
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) != 32 or attempt == 9:
                    raise
                time.sleep(0.1)

    def start(self):
        return self.tasks.start("session", "核对代码", "test-model", "react", {"version": "test"})

    def test_events_and_terminal_state_survive_without_any_session_file(self):
        task_id = self.start()
        self.tasks.append(task_id, "tool_start", {"call_id": "call", "name": "read_file"})
        self.tasks.finish(task_id, "cancelled", "用户取消", duration=1.25)
        loaded = TaskStore(self.workspace, create=False).get(task_id)
        self.assertEqual(loaded["status"], "cancelled")
        self.assertEqual(loaded["verification_status"], "unverified")
        self.assertIsNone(loaded["usage"])
        self.assertEqual(loaded["duration_seconds"], 1.25)
        self.assertEqual(loaded["events"][0]["data"]["call_id"], "call")
        self.assertFalse((self.workspace / ".revocli" / "sessions").exists())
        with self.assertRaises(ValueError):
            self.tasks.append(task_id, "message", {})

    def test_dead_owner_is_unknown_and_live_or_unconfirmed_owner_is_preserved(self):
        task_id = self.start()
        self.assertNotIn(_process_identity(os.getpid()), (None, "unknown"))
        self.assertEqual(self.tasks.recover_interrupted(), [])
        with patch("revocli.storage.tasks._process_identity", return_value="unknown"):
            self.assertEqual(self.tasks.recover_interrupted(), [])
        self.assertEqual(self.tasks.get(task_id)["status"], "running")
        with patch("revocli.storage.tasks._process_identity", return_value=None):
            self.assertEqual(self.tasks.recover_interrupted(), [task_id])
            self.assertEqual(self.tasks.recover_interrupted(), [])
        self.assertEqual(self.tasks.get(task_id)["status"], "unknown")

    def test_reused_pid_cannot_keep_an_abandoned_task_running(self):
        task_id = self.start()
        with patch("revocli.storage.tasks._process_identity", return_value="different-process-birth"):
            self.assertEqual(self.tasks.recover_interrupted(), [task_id])
        self.assertEqual(self.tasks.get(task_id)["verification_status"], "unverified")

    def test_missing_usage_in_any_request_remains_unknown(self):
        task_id = self.start()
        for usage in ({"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, None):
            self.tasks.append(task_id, "model_request", {})
            self.tasks.append(task_id, "model_response", {"usage": usage})
        self.tasks.finish(task_id, "returned", "回答结束")
        loaded = self.tasks.get(task_id)
        self.assertEqual(loaded["model_requests"], 2)
        self.assertIsNone(loaded["usage"])

    def test_unknown_schema_corrupt_file_and_workspace_mismatch_are_not_overwritten(self):
        with contextlib.closing(sqlite3.connect(self.tasks.path)) as connection, connection:
            connection.execute("PRAGMA user_version=999")
        before = self.tasks.path.read_bytes()
        with self.assertRaises(ValueError):
            TaskStore(self.workspace)
        self.assertEqual(self.tasks.path.read_bytes(), before)
        self.tasks.path.write_bytes(b"not a database")
        with self.assertRaises(OSError):
            TaskStore(self.workspace)
        self.assertEqual(self.tasks.path.read_bytes(), b"not a database")

    def test_copying_database_to_another_workspace_requires_explicit_migration(self):
        other = self.workspace / "other"
        other.mkdir()
        copied = other / ".revocli" / "tasks" / "tasks.sqlite3"
        copied.parent.mkdir(parents=True)
        shutil.copyfile(self.tasks.path, copied)
        with self.assertRaises(ValueError):
            TaskStore(other, create=False).list_tasks()
        with self.assertRaises(ValueError):
            TaskStore(other)

    def test_invalid_id_and_missing_task_do_not_read_other_files(self):
        for task_id in ("../private", "'; DROP TABLE task_runs;--", "0" * 32):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.tasks.get(task_id)
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_non_git_directory_records_unknown_version(self):
        with patch("revocli.storage.tasks.subprocess.run", side_effect=FileNotFoundError):
            snapshot = project_version(self.workspace)
        self.assertIsNone(snapshot["head"])
        self.assertIsNone(snapshot["fingerprint"])
        self.assertFalse(snapshot["complete"])

    @unittest.skipUnless(shutil.which("git"), "需要 Git 验证版本指纹")
    def test_git_snapshot_detects_tracked_and_untracked_changes_without_storing_contents(self):
        def git(*args):
            subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                            "-c", "core.fsmonitor=false", "-C", str(self.workspace), *args], check=True,
                           capture_output=True, timeout=10)

        git("init", "--quiet")
        (self.workspace / ".gitignore").write_text(".revocli/\n", encoding="utf-8")
        source = self.workspace / "app.py"
        source.write_text("print(1)\n", encoding="utf-8")
        git("add", "app.py", ".gitignore")
        git("-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid", "commit", "-qm", "fixture")
        first = project_version(self.workspace)
        source.write_text("print(2)\n", encoding="utf-8")
        second = project_version(self.workspace)
        extra = self.workspace / "new.py"
        extra.write_text("one", encoding="utf-8")
        third = project_version(self.workspace)
        extra.write_text("two", encoding="utf-8")
        fourth = project_version(self.workspace)
        self.assertFalse(first["dirty"])
        self.assertTrue(second["dirty"])
        self.assertEqual(first["head"], fourth["head"])
        self.assertEqual(len({item["fingerprint"] for item in (first, second, third, fourth)}), 4)
        self.assertTrue(fourth["complete"])
        self.assertNotIn("print(2)", json.dumps(fourth))


class TaskRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.source = self.workspace / "app.py"
        self.source.write_text("print(1)\n", encoding="utf-8")
        self.sessions = SessionStore(self.workspace)
        self.session = self.sessions.new_session("test-model")
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.ui = TerminalUI(self.output, self.errors, color="never")
        self.requests = []

    def run_replies(self, replies, **options):
        def handler(request):
            self.requests.append(json.loads(request.content))
            response = replies[len(self.requests) - 1]
            if isinstance(response, BaseException):
                raise response
            return httpx2.Response(200, json=response)

        with OpenAI(api_key="offline-secret-key", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
            return run_turn(client, "test-model", self.workspace, self.sessions, self.ui, self.session,
                            "核对并处理 app.py", **options)

    def task(self):
        tasks = TaskStore(self.workspace, create=False)
        return tasks.get(tasks.list_tasks()[0]["id"])

    def edit(self):
        return reply(name="edit_file", arguments={"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"})

    def test_command_facts_usage_and_actual_input_are_saved_but_not_promoted_to_acceptance(self):
        usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
        command = {"argv": [sys.executable, "-c", "print('checked')"]}
        self.run_replies([reply(name="run_command", arguments=command, usage=usage),
                          reply("命令运行完毕", usage=usage)], capabilities=("read", "exec"), approver=approve_all)
        task = self.task()
        self.assertEqual(task["status"], "returned")
        self.assertEqual(task["verification_status"], "unverified")
        self.assertEqual(task["usage"]["total_tokens"], 28)
        self.assertEqual(task["model_requests"], 2)
        requests = [event["data"] for event in task["events"] if event["kind"] == "model_request"]
        self.assertEqual(requests[0]["messages"], self.requests[0]["messages"])
        result = next(event["data"]["result"] for event in task["events"] if event["kind"] == "tool_result")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("checked", result["output"])
        artifact = next(event["data"] for event in task["events"] if event["kind"] == "artifact")
        self.assertEqual(artifact["record_id"], result["record_id"])
        self.assertTrue((self.workspace / ".revocli" / "commands" / f"{artifact['record_id']}.json").is_file())
        self.assertEqual(task["metadata"]["provider_host"], "offline.invalid")
        self.assertNotIn("offline-secret-key", json.dumps(task))

    def test_edit_survives_model_failure_and_failed_turn_does_not_enter_session(self):
        with self.assertRaises(APIError):
            self.run_replies([self.edit(), httpx2.ConnectError("offline failure")], capabilities=("read", "write"), approver=approve_all)
        task = self.task()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.source.read_text(), "print(2)\n")
        self.assertEqual(self.session["messages"], [])
        backup = next(event for event in task["events"] if event["kind"] == "artifact")
        result = next(event for event in task["events"] if event["kind"] == "tool_result")
        self.assertLess(backup["sequence"], result["sequence"])
        self.assertEqual(backup["data"]["record_id"], result["data"]["result"]["backup_id"])
        self.assertIsNone(task["usage"])

    def test_human_verification_requires_task_owned_command_and_matching_exit_result(self):
        self.run_replies([reply(name="run_command", arguments={"argv": [sys.executable, "-c", "print('checked')"]}),
                          reply("返回")], capabilities=("read", "exec"), approver=approve_all)
        task = self.task()
        tasks = TaskStore(self.workspace, create=False)
        command_id = next(event["data"]["result"]["record_id"] for event in task["events"] if event["kind"] == "tool_result")
        with self.assertRaises(ValueError):
            tasks.verify(task["id"], "passed", "0" * 32, "不能引用其他命令")
        tasks.verify(task["id"], "failed", command_id, "进程虽正常退出，但输出没有覆盖目标")
        self.assertEqual(tasks.get(task["id"])["verification_status"], "failed")
        handle_command(f"/task-verify {task['id']} passed {command_id} 仅核对输出 checked 和退出码", self.ui,
                       self.sessions, self.session, "test-model")
        loaded = tasks.get(task["id"])
        self.assertEqual(loaded["status"], "returned")
        self.assertEqual(loaded["verification_status"], "passed")
        self.assertEqual(loaded["events"][-1]["data"]["source"], "user")
        self.assertEqual(len(self.requests), 2)

    def test_nonzero_command_can_be_verified_as_failed_but_never_passed(self):
        self.run_replies([reply(name="run_command", arguments={"argv": [sys.executable, "-c", "raise SystemExit(2)"]}),
                          reply("命令失败")], capabilities=("read", "exec"), approver=approve_all)
        task = self.task()
        tasks = TaskStore(self.workspace, create=False)
        command_id = next(event["data"]["result"]["record_id"] for event in task["events"] if event["kind"] == "tool_result")
        with self.assertRaises(ValueError):
            tasks.verify(task["id"], "passed", command_id, "不能误报")
        tasks.verify(task["id"], "failed", command_id, "该验证进程退出码为 2")
        self.assertEqual(tasks.get(task["id"])["verification_status"], "failed")

    def test_cancel_after_edit_preserves_task_backup_and_full_completed_action(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_replies([self.edit(), KeyboardInterrupt()], capabilities=("read", "write"), approver=approve_all)
        self.assertEqual(self.task()["status"], "cancelled")
        self.assertEqual(self.source.read_text(), "print(2)\n")
        self.assertEqual(self.session["messages"], [])
        self.assertTrue(any(event["kind"] == "tool_result" for event in self.task()["events"]))

    def test_trace_failure_before_edit_side_effect_stops_the_task(self):
        real_append = TaskStore.append

        def fail_artifact(store, task_id, kind, data):
            if kind == "artifact":
                self.assertEqual(self.source.read_text(), "print(1)\n")
                raise OSError("offline disk full")
            return real_append(store, task_id, kind, data)

        with patch.object(TaskStore, "append", new=fail_artifact), self.assertRaises(RuntimeError):
            self.run_replies([self.edit()], capabilities=("read", "write"), approver=approve_all)
        self.assertEqual(self.source.read_text(), "print(1)\n")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.task()["status"], "failed")

    def test_model_context_truncation_does_not_truncate_the_recorded_tool_fact(self):
        self.source.write_text("x" * 1000, encoding="utf-8")
        self.run_replies([reply(name="read_file", arguments={"path": "app.py"}), reply("结果超过预算")],
                         limits=AgentLimits(max_tool_calls=1, max_tool_bytes=256))
        task = self.task()
        fact = next(event["data"]["result"] for event in task["events"] if event["kind"] == "tool_result")
        self.assertEqual(fact["content"], "x" * 1000)
        self.assertFalse(json.loads(self.requests[1]["messages"][-1]["content"])["ok"])
        self.assertEqual(task["status"], "incomplete")

    def test_model_review_is_separate_from_acceptance_and_blocked_is_a_terminal_state(self):
        for status, expected in (("completed", "returned"), ("blocked", "blocked")):
            with self.subTest(status=status):
                self.requests = []
                self.run_replies([document(steps=["核对代码"]), reply("执行说明"),
                                  document(status=status, reason="模型判断", steps=[]), reply("汇总")], mode="plan")
                task = self.task()
                self.assertEqual(task["status"], expected)
                self.assertEqual(task["plan"]["steps"][0]["model_review"], "模型判断")
                self.assertEqual(task["verification_status"], "unverified")

    def test_plan_budget_exhaustion_is_incomplete_and_does_not_run_unreviewed_steps(self):
        self.run_replies([document(steps=["读取文件", "修改文件"]),
                          reply(name="read_file", arguments={"path": "app.py"}),
                          reply("已经读取"), reply("预算用尽")], mode="plan", max_steps=4)
        self.assertEqual(self.task()["status"], "incomplete")
        self.assertEqual(self.task()["plan"]["steps"][0]["status"], "reviewing")
        self.assertEqual(self.source.read_text(), "print(1)\n")

    def test_memory_failure_is_recorded_before_any_model_request(self):
        with patch.object(SessionStore, "memory_context", side_effect=ValueError("corrupt memory")), \
                self.assertRaises(ValueError):
            self.run_replies([])
        self.assertEqual(self.task()["status"], "failed")
        self.assertEqual(self.task()["model_requests"], 0)
        self.assertEqual(self.requests, [])

    def test_task_storage_unavailable_prevents_model_and_tool_execution(self):
        with patch.object(TaskStore, "start", side_effect=OSError("offline read only")), self.assertRaises(OSError):
            self.run_replies([self.edit()], capabilities=("read", "write"), approver=approve_all)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.source.read_text(), "print(1)\n")

    def test_terminal_record_failure_keeps_answer_and_does_not_repeat_request(self):
        with patch.object(TaskStore, "finish", side_effect=OSError("offline finish failure")):
            answer = self.run_replies([reply("现有回答")])
        self.assertEqual(answer, "现有回答")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session["messages"][-1]["content"], "现有回答")
        self.assertIn("终态未保存", self.errors.getvalue())

    def test_read_commands_need_no_config_or_client_and_json_is_machine_readable(self):
        self.run_replies([reply("已回答")])
        task_id = self.task()["id"]
        output = io.StringIO()
        with patch("revocli.cli.load_config") as config, patch("revocli.cli.OpenAI") as client, \
                contextlib.redirect_stdout(output):
            code = main(["--workspace", str(self.workspace), "--task", task_id, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["id"], task_id)
        config.assert_not_called()
        client.assert_not_called()
        for command in ("/tasks", f"/task {task_id}"):
            self.assertIs(handle_command(command, self.ui, self.sessions, self.session, "test-model"), self.session)
        self.assertIn(task_id, self.output.getvalue())
        self.assertIn("任务验收：未验证", self.output.getvalue())

    def test_empty_task_view_does_not_create_runtime_files(self):
        empty = self.workspace / "empty"
        empty.mkdir()
        with patch("revocli.cli.load_config") as config, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--workspace", str(empty), "--tasks"]), 0)
        config.assert_not_called()
        self.assertFalse((empty / ".revocli").exists())

    def test_process_crash_after_real_edit_is_recovered_as_unknown_without_replaying(self):
        script = r'''
import json, os, sys
from pathlib import Path
from revocli.storage.tasks import TaskStore
from revocli.tools import execute_tool
from revocli.tools.approval import approve_all
workspace = Path(sys.argv[1])
tasks = TaskStore(workspace)
task_id = tasks.start("crashed-session", "修改文件", "test-model", "react", {})
tasks.append(task_id, "tool_start", {"action_id": "one", "call_id": "call", "name": "edit_file", "arguments": "app.py"})
def record(kind, record_id):
    tasks.append(task_id, "artifact", {"action_id": "one", "call_id": "call", "record_type": kind, "record_id": record_id})
result = execute_tool("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"},
                      workspace, capabilities=("read", "write"), approver=approve_all, on_record=record)
assert result["ok"], result
os._exit(0)
'''
        process = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.workspace)],
                                 capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("revocli.cli.load_config") as config:
            self.assertEqual(main(["--workspace", str(self.workspace), "--tasks", "--json"]), 0)
        config.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())[0]["status"], "unknown")
        task = self.task()
        self.assertEqual(task["verification_status"], "unverified")
        self.assertEqual(self.source.read_text(), "print(2)\n")
        self.assertTrue(any(event["kind"] == "artifact" for event in task["events"]))
        self.assertFalse(any(event["kind"] == "tool_result" for event in task["events"]))
        show_tasks(self.ui, TaskStore(self.workspace, create=False), task["id"])
        self.assertIn("结果未知", self.output.getvalue())
        self.assertIn("操作记录", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
