import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archer.storage.session import SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.store = SessionStore(self.workspace)

    def test_session_roundtrip_and_latest(self):
        session = self.store.new_session("test-model")
        session["messages"] = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
        ]
        path = self.store.save(session)
        self.assertTrue(path.is_file())

        loaded = self.store.load(session["id"])
        self.assertEqual(loaded["messages"], session["messages"])
        self.assertEqual(self.store.latest()["id"], session["id"])
        self.assertEqual(self.store.list_sessions()[0]["messages"], 2)

    def test_memory_is_injected_and_can_be_removed(self):
        item = self.store.add_memory("项目使用 uv 管理依赖")
        self.assertIn("项目使用 uv 管理依赖", self.store.memory_context().text)
        self.assertEqual(self.store.remove_memory(item["id"]), 1)
        self.assertEqual(self.store.memory_context().text, "")

    def test_memory_rejects_empty_and_oversized_values(self):
        with self.assertRaises(ValueError):
            self.store.add_memory("   ")
        with self.assertRaises(ValueError):
            self.store.add_memory("x" * 2001)

    def test_invalid_session_id_is_rejected(self):
        for session_id in ["../escape", "a/b", "", "x" * 65]:
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.store.load(session_id)

    @unittest.skipUnless(shutil.which("git"), "需要 Git 验证忽略规则")
    def test_state_is_ignored_in_another_repository(self):
        subprocess.run(["git", "init", "--quiet", str(self.workspace)], check=True, capture_output=True)
        project_ignore = self.workspace / ".gitignore"
        project_ignore.write_text("build/\n", encoding="utf-8")
        # 在目标仓库重新初始化存储，不改项目自己的忽略规则。
        store = SessionStore(self.workspace)
        session = store.new_session("test-model")
        session_path = store.save(session)
        store.add_memory("保留在本地")
        pending = store.sessions_dir / "pending.json.tmp"
        pending.write_text("temporary", encoding="utf-8")
        paths = [session_path, store.memory_path, pending, store.root / ".gitignore"]
        result = subprocess.run(
            ["git", "-C", str(self.workspace), "check-ignore", "--no-index", "--", *map(str, paths)],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(len(result.stdout.splitlines()), len(paths))
        self.assertEqual(project_ignore.read_text(encoding="utf-8"), "build/\n")

    def test_bom_memory_survives_add_and_remove(self):
        original = self.store.add_memory("原有记忆")
        text = self.store.memory_path.read_text(encoding="utf-8")
        self.store.memory_path.write_text(text, encoding="utf-8-sig")
        added = self.store.add_memory("新记忆")
        self.assertEqual([item["text"] for item in self.store.memory_items()], ["原有记忆", "新记忆"])
        self.store.remove_memory(added["id"])
        self.assertEqual(self.store.memory_items(), [original])

    def test_corrupt_memory_is_reported_without_overwriting(self):
        invalid = [
            b"{not json", b"\xff", b"{}", b'{"items": {}}',
            b'{"items": [{"text": "missing id"}]}',
            b'{"items": [{"id": "m1", "text": "one"}, {"id": "m1", "text": "two"}]}',
        ]
        for content in invalid:
            with self.subTest(content=content):
                self.store.memory_path.write_bytes(content)
                for operation in [self.store.memory_items, lambda: self.store.add_memory("新记忆"),
                                  lambda: self.store.remove_memory("all")]:
                    with self.assertRaises(ValueError):
                        operation()
                    self.assertEqual(self.store.memory_path.read_bytes(), content)

    def test_memory_read_error_does_not_become_empty_memory(self):
        self.store.add_memory("已有记忆")
        before = self.store.memory_path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("offline read error")):
            with self.assertRaises(PermissionError):
                self.store.add_memory("新记忆")
        self.assertEqual(self.store.memory_path.read_bytes(), before)

    def test_invalid_session_data_is_rejected_by_all_readers(self):
        valid = self.store.new_session("test-model")
        valid["id"] = "broken"
        tool_request = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]}
        invalid_fields = [
            {"id": None}, {"id": "different"},
            {"workspace": str(self.workspace / "another")}, {"model": None},
            {"created_at": None}, {"updated_at": "invalid time"},
            {"messages": None}, {"messages": [None]},
            {"messages": [{"role": "system", "content": "injected"}]},
            {"messages": [{"role": [], "content": "bad role"}]},
            {"messages": [{"role": "user", "content": {}}]},
            {"messages": [{"role": "tool", "content": "{}", "tool_call_id": "orphan"}]},
            {"messages": [tool_request]},
            {"messages": [tool_request, {"role": "tool", "content": "{}", "tool_call_id": "wrong"}]},
        ]
        path = self.store.path_for("broken")
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                data = {**copy.deepcopy(valid), **fields}
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.load("broken")
                self.assertIsNone(self.store.latest())
                self.assertEqual(self.store.list_sessions(), [])

    def test_latest_skips_invalid_newer_session(self):
        valid = self.store.new_session("test-model")
        valid["messages"] = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]
        valid_path = self.store.save(valid)
        broken_path = self.store.path_for("broken")
        broken_path.write_text('{"messages": []}', encoding="utf-8")
        later = valid_path.stat().st_mtime + 10
        os.utime(broken_path, (later, later))
        self.assertEqual(self.store.latest()["id"], valid["id"])
        self.assertEqual([item["id"] for item in self.store.list_sessions()], [valid["id"]])

    def test_latest_skips_empty_newer_session(self):
        valid = self.store.new_session("test-model")
        valid["messages"] = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]
        valid_path = self.store.save(valid)
        empty = self.store.new_session("test-model")
        empty_path = self.store.save(empty)
        later = valid_path.stat().st_mtime + 10
        os.utime(empty_path, (later, later))
        self.assertEqual(self.store.latest()["id"], valid["id"])

    def test_unchanged_session_does_not_rewrite_or_refresh_timestamp(self):
        session = self.store.new_session("test-model")
        path = self.store.save(session)
        original = path.read_bytes()
        with patch("archer.storage.session._write_json") as write:
            self.store.save(session)
            restored = self.store.load(session["id"])
            self.store.save(restored)
        write.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_session_is_upgraded_without_modifying_file_on_load(self):
        legacy = self.store.new_session("test-model")
        legacy.pop("schema_version")
        path = self.store.path_for(legacy["id"])
        path.write_text(json.dumps(legacy), encoding="utf-8")
        original = path.read_bytes()
        loaded = self.store.load(legacy["id"])
        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(path.read_bytes(), original)
        self.store.save(loaded)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_unknown_versions_are_rejected_without_overwriting(self):
        session = self.store.new_session("test-model")
        for version in [2, -1, True, "1"]:
            with self.subTest(version=version):
                session["schema_version"] = version
                path = self.store.path_for(session["id"])
                path.write_text(json.dumps(session), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.load(session["id"])
                self.assertEqual(path.read_bytes(), before)
                self.store.memory_path.write_text(json.dumps({"schema_version": version, "items": []}), encoding="utf-8")
                before = self.store.memory_path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.add_memory("事实")
                self.assertEqual(self.store.memory_path.read_bytes(), before)

    def test_memory_selection_prefers_recent_and_skips_oversized_item(self):
        old = self.store.add_memory("OLD" * 600)
        self.store.add_memory("LONG" * 500)
        recent = self.store.add_memory("最新项目事实")
        context = self.store.memory_context()
        self.assertIn("最新项目事实", context.text)
        self.assertEqual(context.selected_ids[0], recent["id"])
        self.assertEqual(context.omitted_count, 1)
        self.assertLessEqual(len(context.text), 3000)
        # 小预算下长的新条目放不下，也仍会尝试较短的旧条目。
        self.store.add_memory("Z" * 2000)
        context = self.store.memory_context(limit=100)
        self.assertEqual(context.selected_ids, (recent["id"],))
        self.assertNotIn(old["id"], context.selected_ids)
        self.assertEqual(self.store.memory_context(limit=0).text, "")

    def test_memory_deduplicates_and_updates_without_changing_id(self):
        original = self.store.add_memory("项目 使用 uv")
        duplicate = self.store.add_memory("  项目  使用 uv  ")
        self.assertEqual(original["id"], duplicate["id"])
        self.assertEqual(len(self.store.memory_items()), 1)
        second = self.store.add_memory("其他事实")
        updated = self.store.update_memory(original["id"], "项目使用 Python")
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(self.store.memory_context().selected_ids[0], original["id"])
        self.assertEqual(updated["created_at"], original["created_at"])
        with self.assertRaises(ValueError):
            self.store.update_memory(original["id"], second["text"])
        with self.assertRaises(ValueError):
            self.store.update_memory("missing", "无效更新")

    def test_legacy_memory_upgrades_on_next_write(self):
        self.store.memory_path.write_text(json.dumps({"items": [{"id": "old", "text": "原记忆"}]}), encoding="utf-8")
        self.assertIn("原记忆", self.store.memory_context().text)
        self.store.add_memory("新记忆")
        self.assertEqual(json.loads(self.store.memory_path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_bom_session_with_tool_history_can_be_restored(self):
        session = self.store.new_session("test-model")
        session["messages"] = [
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": false}'},
            {"role": "assistant", "content": "failed to read"},
        ]
        path = self.store.save(session)
        path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")
        self.assertEqual(self.store.load(session["id"]), session)

    def test_failed_replace_preserves_saved_session_and_timestamp(self):
        session = self.store.new_session("test-model")
        path = self.store.save(session)
        before = path.read_bytes()
        updated_at = session["updated_at"]
        session["messages"] = [{"role": "user", "content": "new question"},
                               {"role": "assistant", "content": "new answer"}]
        with patch("archer.storage.files.os.replace", side_effect=PermissionError("offline write error")), \
                patch("archer.storage.session._now", return_value="2099-01-01T00:00:00+00:00"):
            with self.assertRaises(PermissionError):
                self.store.save(session)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(session["updated_at"], updated_at)
        self.assertEqual(list(self.store.sessions_dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
