import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from revocli.cli import main
from revocli.storage.session import SessionStore
from revocli.storage.tasks import TaskStore
from revocli.tools import execute_tool
from revocli.tools.approval import approve_all


class LegacyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.legacy = self.workspace / ".archer"
        self.legacy.mkdir()

    def test_old_session_and_memory_are_loaded_without_creating_a_second_store(self):
        sessions = self.legacy / "sessions"
        sessions.mkdir()
        session = {
            "id": "legacy-session", "workspace": str(self.workspace), "model": "offline-model",
            "created_at": "2026-10-26T09:00:00+08:00", "updated_at": "2026-10-26T09:00:00+08:00",
            "messages": [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}],
        }
        session_file = sessions / "legacy-session.json"
        session_file.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        before = session_file.read_bytes()
        (self.legacy / "memory.json").write_text(json.dumps({"items": [{"id": "m1", "text": "旧项目记忆"}]}), encoding="utf-8")
        store = SessionStore(self.workspace)
        self.assertEqual(store.root, self.legacy)
        self.assertEqual(store.latest()["messages"], session["messages"])
        self.assertEqual(session_file.read_bytes(), before)
        self.assertEqual(store.memory_items()[0]["text"], "旧项目记忆")
        store.add_memory("新增记忆")
        self.assertEqual(len(store.memory_items()), 2)
        self.assertFalse((self.workspace / ".revocli").exists())

    def test_legacy_backup_can_still_be_created_and_restored(self):
        source = self.workspace / "app.py"
        source.write_text("print(1)\n", encoding="utf-8")
        result = execute_tool("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"},
                              self.workspace, capabilities=("read", "write"), approver=approve_all)
        self.assertTrue(result["ok"], result)
        backup = self.legacy / "backups" / f"{result['backup_id']}.json"
        self.assertTrue(backup.is_file())
        restored = execute_tool("restore_file", {"backup_id": result["backup_id"]}, self.workspace,
                                capabilities=("read", "write"), approver=approve_all)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(source.read_text(), "print(1)\n")
        self.assertFalse((self.workspace / ".revocli").exists())

    def test_legacy_task_database_and_artifact_links_are_read_without_model_configuration(self):
        tasks = TaskStore(self.workspace)
        task_id = tasks.start("legacy-session", "旧任务", "offline-model", "react", {})
        tasks.append(task_id, "artifact", {"action_id": "one", "call_id": "call", "record_type": "backup", "record_id": "a" * 32})
        tasks.finish(task_id, "cancelled", "旧任务已取消")
        output = io.StringIO()
        with patch("revocli.cli.load_config") as config, patch("revocli.cli.OpenAI") as client, \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(["--workspace", str(self.workspace), "--task", task_id]), 0)
        config.assert_not_called()
        client.assert_not_called()
        self.assertIn(".archer/backups/", output.getvalue())
        self.assertIn("cancelled", output.getvalue())
        self.assertEqual(tasks.path.parent.parent, self.legacy)
        self.assertFalse((self.workspace / ".revocli").exists())

    def test_two_data_roots_are_rejected_without_overwriting_either(self):
        memory = self.legacy / "memory.json"
        memory.write_text("original legacy data", encoding="utf-8")
        (self.workspace / ".revocli").mkdir()
        for factory in (SessionStore, TaskStore, lambda workspace: TaskStore(workspace, create=False)):
            with self.subTest(factory=factory), self.assertRaisesRegex(ValueError, "同时存在"):
                factory(self.workspace)
        self.assertEqual(memory.read_text(), "original legacy data")
        self.assertEqual(list((self.workspace / ".revocli").iterdir()), [])

    def test_both_names_are_hidden_from_search_and_protected_from_editing(self):
        for folder in (self.legacy, self.workspace / ".revocli"):
            folder.mkdir(exist_ok=True)
            (folder / "private.txt").write_text("private marker", encoding="utf-8")
        found = execute_tool("find_files", {"pattern": "private"}, self.workspace)
        searched = execute_tool("search_text", {"query": "private marker"}, self.workspace)
        self.assertEqual(found["files"], [])
        self.assertEqual(searched["matches"], [])
        for folder in (".archer", ".ARCHER", ".revocli", ".REVOCLI"):
            result = execute_tool("edit_file", {"path": f"{folder}/private.txt", "old_text": "private", "new_text": "changed"},
                                  self.workspace, capabilities=("read", "write"), approver=approve_all)
            self.assertFalse(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
