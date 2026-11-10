"""每轮写文件前的检查点，以及 /rewind 与 /undo 的差别。"""

import io
import json
from pathlib import Path
import random
import tempfile
import time
import unittest

import httpx2
from openai import OpenAI

from recapcli.application.commands import handle_command
from recapcli.core.agent import run_agent
from recapcli.storage.session import SessionStore
from recapcli.tools import execute_tool
from recapcli.tools.approval import approve_all
from recapcli.tools.checkpoints import rewind_checkpoint, turn_checkpoint
from recapcli.ui.terminal_ui import TerminalUI

SEED = 20261107
TRIALS = 40


def measure_rewind(seed=SEED, trials=TRIALS):
    """固定种子下比较：干净回退是否还原，被外部改过时是否拒绝。"""
    rng = random.Random(seed)
    clean_restored = dirty_refused = unexpected = 0
    clean_trials = dirty_trials = 0
    rewind_seconds = 0.0
    for index in range(trials):
        dirty = rng.random() < 0.25
        file_count = rng.randint(1, 3)
        edits = rng.randint(1, 3)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            original = {}
            failed = False
            touched = []
            for number in range(file_count):
                name = f"f{number}.txt"
                text = f"原始-{index}-{number}\n"
                (workspace / name).write_text(text, encoding="utf-8")
                original[name] = text
            with turn_checkpoint(workspace):
                for edit_index in range(edits):
                    name = f"f{rng.randrange(file_count)}.txt"
                    current = (workspace / name).read_text(encoding="utf-8")
                    updated = current + f"改{edit_index}\n"
                    result = execute_tool("edit_file", {"path": name, "old_text": current, "new_text": updated},
                                          workspace, capabilities={"read", "write"}, approver=approve_all)
                    if not result["ok"]:
                        unexpected += 1
                        failed = True
                        break
                    if name not in touched:
                        touched.append(name)
            if failed:
                continue
            if dirty:
                dirty_trials += 1
                victim = touched[rng.randrange(len(touched))]
                (workspace / victim).write_text("外部改动\n", encoding="utf-8")
                rewind_started = time.perf_counter()
                refused = execute_tool("rewind_checkpoint", {}, workspace,
                                       capabilities={"read", "write"}, approver=approve_all)
                rewind_seconds += time.perf_counter() - rewind_started
                if (not refused["ok"] and "后续变化" in refused["error"]
                        and (workspace / victim).read_text(encoding="utf-8") == "外部改动\n"):
                    dirty_refused += 1
                else:
                    unexpected += 1
            else:
                clean_trials += 1
                rewind_started = time.perf_counter()
                restored = execute_tool("rewind_checkpoint", {}, workspace,
                                       capabilities={"read", "write"}, approver=approve_all)
                rewind_seconds += time.perf_counter() - rewind_started
                if restored["ok"] and all((workspace / name).read_text(encoding="utf-8") == text
                                          for name, text in original.items()):
                    clean_restored += 1
                else:
                    unexpected += 1
    return {"seed": seed, "trials": trials, "clean_trials": clean_trials, "dirty_trials": dirty_trials,
            "clean_restored": clean_restored, "dirty_refused": dirty_refused, "unexpected": unexpected,
            "rewind_seconds": rewind_seconds}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()

    def test_checkpoint_keeps_the_first_version_and_rewind_restores_it(self):
        path = self.workspace / "note.txt"
        path.write_text("初稿\n", encoding="utf-8")
        with turn_checkpoint(self.workspace):
            first = execute_tool("edit_file", {"path": "note.txt", "old_text": "初稿\n", "new_text": "第二稿\n"},
                                 self.workspace, capabilities={"read", "write"}, approver=approve_all)
            second = execute_tool("edit_file", {"path": "note.txt", "old_text": "第二稿\n", "new_text": "第三稿\n"},
                                  self.workspace, capabilities={"read", "write"}, approver=approve_all)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "第三稿\n")
        group = first["backup_id"]
        undone = execute_tool("undo_edit", {"group_id": group}, self.workspace,
                              capabilities={"read", "write"}, approver=approve_all)
        self.assertFalse(undone["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "第三稿\n")
        latest = execute_tool("undo_edit", {}, self.workspace, capabilities={"read", "write"}, approver=approve_all)
        self.assertTrue(latest["ok"], latest)
        self.assertEqual(path.read_text(encoding="utf-8"), "第二稿\n")
        blocked = execute_tool("rewind_checkpoint", {}, self.workspace,
                               capabilities={"read", "write"}, approver=approve_all)
        self.assertFalse(blocked["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "第二稿\n")
        self.assertTrue(execute_tool("undo_edit", {}, self.workspace,
                                     capabilities={"read", "write"}, approver=approve_all)["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "初稿\n")

    def test_rewind_command_restores_only_the_latest_turn(self):
        (self.workspace / "a.txt").write_text("A0\n", encoding="utf-8")
        (self.workspace / "b.txt").write_text("B0\n", encoding="utf-8")
        with turn_checkpoint(self.workspace):
            self.assertTrue(execute_tool(
                "edit_file", {"path": "a.txt", "old_text": "A0\n", "new_text": "A1\n"},
                self.workspace, capabilities={"read", "write"}, approver=approve_all)["ok"])
        with turn_checkpoint(self.workspace):
            self.assertTrue(execute_tool(
                "edit_file", {"path": "b.txt", "old_text": "B0\n", "new_text": "B1\n"},
                self.workspace, capabilities={"read", "write"}, approver=approve_all)["ok"])
            self.assertTrue(execute_tool(
                "edit_file", {"path": "a.txt", "old_text": "A1\n", "new_text": "A2\n"},
                self.workspace, capabilities={"read", "write"}, approver=approve_all)["ok"])
        store = SessionStore(self.workspace)
        session = store.new_session("test-model")
        output = io.StringIO()
        ui = TerminalUI(output, io.StringIO(), color="never")
        handle_command("/rewind", ui, store, session, "test-model",
                       capabilities=("read", "write"), approver=approve_all)
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "A1\n")
        self.assertEqual((self.workspace / "b.txt").read_text(encoding="utf-8"), "B0\n")
        self.assertIn("已回退检查点", output.getvalue())

    def test_rewind_picks_the_newest_turn_across_restarts(self):
        """sequence 是进程内计数，重启后从 1 开始；最新一轮要按时间判断。"""
        from recapcli.tools import checkpoints
        (self.workspace / "a.txt").write_text("A0\n", encoding="utf-8")
        (self.workspace / "b.txt").write_text("B0\n", encoding="utf-8")
        original = checkpoints._sequence
        self.addCleanup(setattr, checkpoints, "_sequence", original)
        checkpoints._sequence = 5          # 上一次启动已经跑过几轮
        with turn_checkpoint(self.workspace):
            self.assertTrue(execute_tool(
                "edit_file", {"path": "a.txt", "old_text": "A0\n", "new_text": "A1\n"},
                self.workspace, capabilities={"read", "write"}, approver=approve_all)["ok"])
        time.sleep(0.01)
        checkpoints._sequence = 0          # 重新启动
        with turn_checkpoint(self.workspace):
            self.assertTrue(execute_tool(
                "edit_file", {"path": "b.txt", "old_text": "B0\n", "new_text": "B1\n"},
                self.workspace, capabilities={"read", "write"}, approver=approve_all)["ok"])
        self.assertEqual(sorted(checkpoints.latest_checkpoint(self.workspace)["files"]), ["b.txt"])
        restored = rewind_checkpoint(self.workspace)
        self.assertEqual([item["path"] for item in restored["files"]], ["b.txt"])
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "A1\n")
        self.assertEqual((self.workspace / "b.txt").read_text(encoding="utf-8"), "B0\n")

    def test_agent_turn_creates_a_checkpoint_before_the_write(self):
        (self.workspace / "app.py").write_text("print(1)\n", encoding="utf-8")
        arguments = json.dumps({"path": "app.py", "old_text": "print(1)\n", "new_text": "print(2)\n"})
        created = self.workspace / "new.txt"

        def edit_response():
            return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
                    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [{
                            "id": "edit", "type": "function",
                            "function": {"name": "edit_file", "arguments": arguments}}]}}]}

        def create_response():
            created_arguments = json.dumps({"path": "new.txt", "old_text": "", "new_text": "新建\n"})
            return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
                    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [{
                            "id": "create", "type": "function",
                            "function": {"name": "edit_file", "arguments": created_arguments}}]}}]}

        def text_response(content):
            return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}]}

        replies = [edit_response(), create_response(), text_response("改完了")]
        traces = []

        def handler(request):
            return httpx2.Response(200, json=replies[handler.count])

        handler.count = 0

        def counting(request):
            response = handler(request)
            handler.count += 1
            return response

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(counting)))
        with client:
            result = run_agent(client, "test-model", "修改", self.workspace, capabilities=("read", "write"),
                               approver=approve_all, on_event=lambda _message: None,
                               on_trace=lambda kind, data: traces.append((kind, data)))
        self.assertEqual(result.status, "returned")
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(2)\n")
        self.assertEqual(created.read_text(encoding="utf-8"), "新建\n")
        checkpoint = next(data for kind, data in traces if kind == "checkpoint")
        self.assertEqual(checkpoint["files"], ["app.py", "new.txt"])
        restored = rewind_checkpoint(self.workspace, checkpoint["id"])
        self.assertEqual(len(restored["files"]), 2)
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "print(1)\n")
        self.assertFalse(created.exists())

    def test_measured_rewind_matches_the_seeded_schedule(self):
        measured = measure_rewind()
        self.assertEqual(measured["trials"], 40)
        self.assertEqual(measured["unexpected"], 0)
        self.assertEqual(measured["clean_restored"], measured["clean_trials"])
        self.assertEqual(measured["dirty_refused"], measured["dirty_trials"])
        self.assertGreater(measured["clean_trials"], 0)
        self.assertGreater(measured["dirty_trials"], 0)
        succeeded = measured["clean_restored"] + measured["dirty_refused"]
        self.assertEqual(succeeded, measured["trials"])


if __name__ == "__main__":
    unittest.main()
