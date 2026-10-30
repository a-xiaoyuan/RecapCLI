import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from recapcli.tools import execute_tool, tool_definitions
from recapcli.tools.approval import approve_all
from recapcli.tools.filesystem import ScanBudget
from recapcli.storage.files import write_json


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()

    def call(self, name, **arguments):
        return execute_tool(name, arguments, self.workspace, capabilities={"read", "write", "exec"},
                             approver=approve_all)

    def test_batch_edit_has_one_approval_and_grouped_undo(self):
        (self.workspace / "a.txt").write_text("alpha omega", encoding="utf-8")
        (self.workspace / "b.txt").write_text("before", encoding="utf-8")
        edits = [{"path": "a.txt", "old_text": "alpha", "new_text": "beta"},
                 {"path": "a.txt", "old_text": "omega", "new_text": "end"},
                 {"path": "b.txt", "old_text": "before", "new_text": "after"}]
        approvals = []
        def approve(request):
            approvals.append(request)
            return approve_all(request)
        result = execute_tool("edit_files", {"edits": edits}, self.workspace,
                              capabilities={"read", "write"}, approver=approve)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(approvals), 1)
        self.assertIn("a/b.txt", approvals[0].summary)
        self.assertEqual(len(result["backup_ids"]), 2)
        self.assertEqual((self.workspace / "a.txt").read_text(), "beta end")
        self.call("edit_file", path="a.txt", old_text="beta", new_text="gamma")
        self.assertTrue(self.call("undo_edit")["ok"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "beta end")
        self.assertTrue(self.call("undo_edit")["ok"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "alpha omega")
        self.assertEqual((self.workspace / "b.txt").read_text(), "before")
        edits[-1]["old_text"] = "missing"
        result = self.call("edit_files", edits=edits)
        self.assertFalse(result["ok"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "alpha omega")

    def test_capabilities_filter_definitions_and_enforce_dispatch(self):
        names = lambda capabilities: {item["function"]["name"] for item in tool_definitions(capabilities)}
        self.assertEqual(names({"read"}), {"read_file", "search_text", "list_directory", "find_files", "search_symbols", "search_code_index"})
        self.assertIn("edit_file", names({"read", "write"}))
        self.assertNotIn("run_command", names({"read", "write"}))
        for tool, arguments in [("edit_file", {"path": "new.txt", "old_text": "", "new_text": "hello"}),
                                 ("run_command", {"argv": [sys.executable, "-c", "print(1)"]})]:
            with self.subTest(tool=tool):
                result = execute_tool(tool, arguments, self.workspace)
                self.assertFalse(result["ok"])
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertFalse((self.workspace / ".recapcli").exists())

    def test_find_files_supports_name_glob_path_and_limit(self):
        (self.workspace / "pkg").mkdir()
        for path in ("pkg/AppService.py", "pkg/Other.py", "README.md"):
            (self.workspace / path).touch()
        self.assertEqual(self.call("find_files", pattern="service")["files"], ["pkg/AppService.py"])
        self.assertEqual(self.call("find_files", pattern="pkg/*.PY")["files"], ["pkg/AppService.py", "pkg/Other.py"])
        result = self.call("find_files", pattern="*.py", max_results=1)
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(result["truncated"])
        self.assertFalse(self.call("find_files", pattern="../*", path="..")["ok"])

    def test_find_files_respects_scan_budget_and_skipped_directories(self):
        for name in (".recapcli", ".revocli", ".archer"):
            hidden = self.workspace / name
            hidden.mkdir()
            (hidden / "private.py").touch()
        for number in range(8):
            (self.workspace / f"file{number}.py").touch()
        with patch("recapcli.tools.filesystem.ScanBudget", return_value=ScanBudget(max_entries=3)):
            result = self.call("find_files", pattern="*.py")
        self.assertTrue(result["truncated"])
        for name in (".recapcli", ".revocli", ".archer"):
            self.assertNotIn(f"{name}/private.py", result["files"])
        self.assertLessEqual(len(result["files"]), 3)

    def test_read_reference_uses_actual_end_and_counts_blank_lines(self):
        (self.workspace / "file.txt").write_bytes(b"first\r\n\r\nlast\r\n")
        result = self.call("read_file", path="file.txt", start_line=2, end_line=99)
        self.assertEqual(result["content"], "\nlast")
        self.assertEqual((result["start_line"], result["end_line"]), (2, 3))
        self.assertEqual(result["reference"], "file.txt:2-3")
        blank = self.call("read_file", path="file.txt", start_line=2, end_line=2)
        self.assertEqual(blank["content"], "")
        self.assertEqual(blank["reference"], "file.txt:2")
        full = self.call("read_file", path="file.txt")
        self.assertEqual(full["reference"], "file.txt:1-3")

    def test_empty_file_has_no_fake_line_reference(self):
        (self.workspace / "empty.py").touch()
        result = self.call("read_file", path="empty.py")
        self.assertEqual((result["start_line"], result["end_line"]), (0, 0))
        self.assertEqual(result["reference"], "empty.py")

    def test_text_search_reference_matches_file_line(self):
        (self.workspace / "file.py").write_text("one\nmarker\n", encoding="utf-8")
        result = self.call("search_text", query="marker")
        self.assertEqual(result["matches"][0]["reference"], "file.py:2")

    def test_edit_diff_backup_and_restore_preserve_bom_crlf(self):
        original = "第一行\r\nold\r\n最后\r\n".encode("utf-8-sig")
        path = self.workspace / "file.py"
        path.write_bytes(original)
        result = self.call("edit_file", path="file.py", old_text="old", new_text="new\nextra")
        self.assertTrue(result["ok"], result)
        self.assertIn("-old", result["diff"])
        self.assertIn("+new", result["diff"])
        self.assertEqual(path.read_bytes(), "第一行\r\nnew\r\nextra\r\n最后\r\n".encode("utf-8-sig"))
        backup = json.loads((self.workspace / ".recapcli/backups" / f"{result['backup_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(base64.b64decode(backup["before_base64"]), original)
        self.assertTrue(self.call("restore_file", backup_id=result["backup_id"])["ok"])
        self.assertEqual(path.read_bytes(), original)

    def test_edit_requires_unique_exact_match_unless_replace_all(self):
        path = self.workspace / "file.py"
        path.write_text("old old", encoding="utf-8")
        for old in ("old", "missing", ""):
            self.assertFalse(self.call("edit_file", path="file.py", old_text=old, new_text="new")["ok"])
            self.assertEqual(path.read_text(encoding="utf-8"), "old old")
        result = self.call("edit_file", path="file.py", old_text="old", new_text="new", replace_all=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(path.read_text(encoding="utf-8"), "new new")

    def test_create_and_restore_new_file(self):
        result = self.call("edit_file", path="pkg/new.py", old_text="", new_text="print('hello')\n")
        self.assertTrue(result["created"])
        self.assertTrue((self.workspace / "pkg/new.py").is_file())
        self.assertTrue(self.call("restore_file", backup_id=result["backup_id"])["ok"])
        self.assertFalse((self.workspace / "pkg/new.py").exists())

    def test_restore_refuses_to_overwrite_later_change(self):
        result = self.call("edit_file", path="file.py", old_text="", new_text="original")
        path = self.workspace / "file.py"
        path.write_text("later", encoding="utf-8")
        self.assertFalse(self.call("restore_file", backup_id=result["backup_id"])["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "later")

    def test_corrupt_backup_cannot_delete_target(self):
        result = self.call("edit_file", path="file.py", old_text="", new_text="original")
        record = self.workspace / ".recapcli/backups" / f"{result['backup_id']}.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data.pop("before_base64")
        record.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.call("restore_file", backup_id=result["backup_id"])["ok"])
        self.assertEqual((self.workspace / "file.py").read_text(encoding="utf-8"), "original")

    def test_edit_cannot_escape_or_modify_internal_data_or_credentials(self):
        credentials = self.workspace / ".env"
        credentials.write_text("offline-key", encoding="utf-8")
        with patch("recapcli.tools.filesystem.ENV_FILE", credentials):
            for path in ("../outside.py", ".git/config", ".GIT/config",
                         ".recapcli/memory.json", ".RECAPCLI/memory.json",
                         ".revocli/memory.json", ".REVOCLI/memory.json", ".env"):
                with self.subTest(path=path):
                    self.assertFalse(self.call("edit_file", path=path, old_text="", new_text="changed")["ok"])
        self.assertEqual(credentials.read_text(encoding="utf-8"), "offline-key")

    def test_atomic_edit_detects_intervening_change(self):
        path = self.workspace / "file.py"
        path.write_text("old", encoding="utf-8")

        def save_and_change(record, data):
            write_json(record, data)
            path.write_text("concurrent change", encoding="utf-8")

        with patch("recapcli.tools.editing.write_json", side_effect=save_and_change):
            result = self.call("edit_file", path="file.py", old_text="old", new_text="new")
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "concurrent change")

    def test_unwritable_backup_prevents_edit(self):
        path = self.workspace / "file.py"
        path.write_text("old", encoding="utf-8")
        with patch("recapcli.tools.editing.write_json", side_effect=PermissionError("offline backup failure")):
            result = self.call("edit_file", path="file.py", old_text="old", new_text="new")
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "old")

    @unittest.skipUnless(os.name == "nt", "Windows 路径规则")
    def test_edit_rejects_reserved_devices_and_alternate_streams(self):
        for path in ("NUL", "CON.txt", "file.py:stream", ".git./config"):
            with self.subTest(path=path):
                self.assertFalse(self.call("edit_file", path=path, old_text="", new_text="x")["ok"])

    def test_large_diff_is_bounded_and_noop_does_not_create_backup(self):
        path = self.workspace / "file.py"
        path.write_text("old", encoding="utf-8")
        result = self.call("edit_file", path="file.py", old_text="old", new_text="old")
        self.assertFalse(result["changed"])
        self.assertFalse((self.workspace / ".recapcli/backups").exists())
        result = self.call("edit_file", path="file.py", old_text="old", new_text="中" * 4000)
        self.assertTrue(result["diff_truncated"])
        self.assertLessEqual(len(result["diff"].encode("utf-8")), 8192)

    def test_command_returns_cwd_unicode_exit_code_and_record(self):
        (self.workspace / "pkg").mkdir()
        result = self.call("run_command", argv=[sys.executable, "-c", "import os; print(os.path.basename(os.getcwd())); print('中文')"], cwd="pkg")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["returncode"], 0)
        self.assertIn("pkg", result["output"])
        self.assertIn("中文", result["output"])
        record = self.workspace / ".recapcli/commands" / f"{result['record_id']}.json"
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"], "finished")

    def test_command_nonzero_and_stderr_are_reported(self):
        result = self.call("run_command", argv=[sys.executable, "-c", "import sys; print('failure', file=sys.stderr); sys.exit(7)"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["success"])
        self.assertEqual(result["returncode"], 7)
        self.assertIn("failure", result["output"])
        result = self.call("run_command", argv=[sys.executable, "-c", "print(input())"], stdin_text="中文输入\n")
        self.assertTrue(result["success"], result)
        self.assertIn("中文输入", result["output"])

    def test_command_does_not_interpret_shell_characters(self):
        value = "a; echo injected && | > marker.txt"
        result = self.call("run_command", argv=[sys.executable, "-c", "import sys; print(sys.argv[1])", value])
        self.assertTrue(result["ok"], result)
        self.assertIn(value, result["output"])
        self.assertFalse((self.workspace / "marker.txt").exists())

    def test_command_timeout_preserves_partial_output(self):
        result = self.call("run_command", argv=[sys.executable, "-u", "-c", "import time; print('started', flush=True); time.sleep(20)"], timeout_seconds=3)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIn("started", result["output"])
        self.assertLess(result["duration_seconds"], 8)

    def test_command_output_limit_stops_without_unbounded_capture(self):
        result = self.call("run_command", argv=[sys.executable, "-c", "print('x' * 100000)"], max_output_bytes=1000)
        self.assertFalse(result["ok"])
        self.assertTrue(result["output_truncated"])
        self.assertEqual(len(result["output"].encode("utf-8")), 1000)

    def test_command_cleans_up_child_even_when_parent_exits(self):
        child = "import time; from pathlib import Path; time.sleep(1.5); Path('orphan.txt').write_text('bad')"
        script = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]); print('done')"
        result = self.call("run_command", argv=[sys.executable, "-c", script])
        self.assertTrue(result["ok"], result)
        time.sleep(1.8)
        self.assertFalse((self.workspace / "orphan.txt").exists())

    def test_command_validates_input_and_cwd_before_launch(self):
        invalid = [{"argv": "echo hello"}, {"argv": []}, {"argv": [1]},
                   {"argv": [sys.executable], "cwd": ".."},
                   {"argv": [sys.executable], "timeout_seconds": 0},
                   {"argv": [sys.executable], "max_output_bytes": 0},
                   {"argv": [sys.executable], "encoding": "base64_codec"},
                   {"argv": [sys.executable], "encoding": "invalid-codec"}]
        for arguments in invalid:
            with self.subTest(arguments=arguments), patch.dict(os.environ, {}, clear=True), \
                    patch("recapcli.tools.commands.subprocess.Popen") as start:
                self.assertFalse(self.call("run_command", **arguments)["ok"])
            self.assertEqual(start.call_count, 0)

    def test_command_environment_uses_allowlist_for_tokens_and_cloud_secrets(self):
        secrets = {name: "fake-review-secret" for name in (
            "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
            "CUSTOM_PASSWORD", "CUSTOM_CREDENTIAL",
            "HTTP_PROXY", "PYTHONPATH", "NODE_OPTIONS", "JAVA_TOOL_OPTIONS")}
        script = "import os,json; print(json.dumps({key: os.getenv(key) for key in " + repr(list(secrets)) + "}))"
        with patch.dict(os.environ, secrets | {"JAVA_HOME": "fake-java-home"}):
            result = self.call("run_command", argv=[sys.executable, "-c", script])
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(result["output"]), dict.fromkeys(secrets))
        with patch.dict(os.environ, {"JAVA_HOME": "fake-java-home"}):
            result = self.call("run_command", argv=[sys.executable, "-c", "import os; print(os.getenv('JAVA_HOME'))"])
        self.assertIn("fake-java-home", result["output"])


if __name__ == "__main__":
    unittest.main()
