import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recapcli.tools import execute_tool
from recapcli.tools.approval import approve_all
from recapcli.tools.filesystem import MAX_FILE_BYTES, ScanBudget
from recapcli.storage.session import SessionStore


class ToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def call(self, name, **arguments):
        return execute_tool(name, json.dumps(arguments), self.workspace)

    def link_directory(self, target, link):
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
                self.addCleanup(link.rmdir)
            else:
                link.symlink_to(target, target_is_directory=True)
                self.addCleanup(link.unlink)
        except OSError as error:
            self.skipTest(f"当前环境不允许创建目录链接：{error}")

    def test_search_never_opens_recapcli_credentials(self):
        credentials = self.workspace / ".env"
        credentials.write_text("OPENAI_API_KEY=offline-private-marker\n", encoding="utf-8")
        (self.workspace / "config_example.py").write_text("OPENAI_API_KEY is required", encoding="utf-8")
        real_open = Path.open

        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path.resolve(), credentials)
            return real_open(path, *args, **kwargs)

        with patch("recapcli.tools.filesystem.ENV_FILE", credentials), patch.object(Path, "open", new=guarded_open):
            self.assertFalse(self.call("read_file", path=".env")["ok"])
            self.assertFalse(self.call("search_text", query="OPENAI_API_KEY", path=".env")["ok"])
            result = self.call("search_text", query="OPENAI_API_KEY")
        self.assertTrue(result["ok"])
        self.assertEqual([match["path"] for match in result["matches"]], ["config_example.py"])
        self.assertNotIn("offline-private-marker", json.dumps(result))

    def test_workspace_credentials_share_read_search_edit_and_index_policy(self):
        names = (".env", "nested/.Env.production", "private.pem", "secret.key", ".aws/credentials", ".ssh/id_rsa")
        for name in names:
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("FAKE_CREDENTIAL_MARKER", encoding="utf-8")
        for name in names:
            with self.subTest(path=name):
                self.assertFalse(self.call("read_file", path=name)["ok"])
                self.assertFalse(self.call("search_text", query="FAKE_CREDENTIAL_MARKER", path=name)["ok"])
                result = execute_tool("edit_file", {"path": name, "old_text": "FAKE_CREDENTIAL_MARKER", "new_text": "changed"},
                                      self.workspace, capabilities={"read", "write"}, approver=approve_all)
                self.assertFalse(result["ok"])
                self.assertEqual((self.workspace / name).read_text(encoding="utf-8"), "FAKE_CREDENTIAL_MARKER")
        for tool, arguments in (("search_text", {"query": "FAKE_CREDENTIAL_MARKER"}),
                                ("search_code_index", {"query": "FAKE_CREDENTIAL_MARKER"})):
            self.assertEqual(self.call(tool, **arguments)["matches"], [])
        self.assertEqual(self.call("find_files", pattern="*")["files"], [])
        self.assertNotIn(".env", self.call("list_directory")["tree"])
        example = self.workspace / ".env.example"
        example.write_text("PORT=8000\n", encoding="utf-8")
        self.assertEqual(self.call("read_file", path=example.name)["content"], "PORT=8000\n")
        edited = execute_tool("edit_file", {"path": example.name, "old_text": "8000", "new_text": "9000"},
                              self.workspace, capabilities={"read", "write"}, approver=approve_all)
        self.assertTrue(edited["ok"], edited)
        self.assertEqual(self.call("search_code_index", query="PORT")["matches"][0]["path"], example.name)
        if os.name == "nt":
            for alias in (".env::$DATA", "private.pem::$DATA"):
                self.assertFalse(self.call("read_file", path=alias)["ok"])

    def test_recursive_tools_never_enter_outside_directory_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("SENTINEL private", encoding="utf-8")
        (self.workspace / "public.txt").write_text("SENTINEL public", encoding="utf-8")
        link = self.workspace / "linked"
        self.link_directory(outside, link)
        real_open, real_iterdir = Path.open, Path.iterdir

        def guarded_open(path, *args, **kwargs):
            self.assertTrue(path.resolve().is_relative_to(self.workspace))
            return real_open(path, *args, **kwargs)

        def guarded_iterdir(path):
            self.assertTrue(path.resolve().is_relative_to(self.workspace))
            return real_iterdir(path)

        with patch.object(Path, "open", new=guarded_open), patch.object(Path, "iterdir", new=guarded_iterdir):
            self.assertFalse(self.call("read_file", path="linked/private.txt")["ok"])
            self.assertFalse(self.call("search_text", query="SENTINEL", path="linked")["ok"])
            self.assertFalse(self.call("list_directory", path="linked")["ok"])
            searched = self.call("search_text", query="SENTINEL")
            listed = self.call("list_directory")
            found = self.call("find_files", pattern="*.txt")
        self.assertEqual([match["path"] for match in searched["matches"]], ["public.txt"])
        self.assertNotIn("linked", listed["tree"])
        self.assertNotIn("private.txt", listed["tree"])
        self.assertEqual(found["files"], ["public.txt"])
        result = execute_tool("edit_file", {"path": "linked/private.txt", "old_text": "SENTINEL", "new_text": "changed"},
                              self.workspace, capabilities={"read", "write"}, approver=approve_all)
        self.assertFalse(result["ok"])
        self.assertEqual((outside / "private.txt").read_text(encoding="utf-8"), "SENTINEL private")

    def test_recursive_tools_skip_links_back_to_workspace(self):
        package = self.workspace / "pkg"
        package.mkdir()
        (package / "code.py").write_text("unique-marker", encoding="utf-8")
        self.link_directory(self.workspace, package / "back")
        searched = self.call("search_text", query="unique-marker")
        listed = self.call("list_directory", max_depth=5)
        self.assertTrue(searched["ok"])
        self.assertEqual([match["path"] for match in searched["matches"]], ["pkg/code.py"])
        self.assertNotIn("back", listed["tree"])

    def test_operations_reject_linked_state_directory(self):
        outside = self.root / "outside-state"
        outside.mkdir()
        self.link_directory(outside, self.workspace / ".recapcli")
        (self.workspace / "file.txt").write_text("old", encoding="utf-8")
        with self.assertRaises(ValueError):
            SessionStore(self.workspace)
        result = execute_tool("edit_file", {"path": "file.txt", "old_text": "old", "new_text": "new"},
                              self.workspace, capabilities={"read", "write"}, approver=approve_all)
        self.assertFalse(result["ok"])
        self.assertEqual((self.workspace / "file.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual(list(outside.iterdir()), [])

    def test_large_file_can_be_read_by_line_range(self):
        lines = [f"line {number}" for number in range(1, 20001)]
        path = self.workspace / "large.py"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertGreater(path.stat().st_size, MAX_FILE_BYTES)
        self.assertFalse(self.call("read_file", path="large.py")["ok"])
        for start, end in [(1, 2), (15000, 15002)]:
            with self.subTest(start=start):
                result = self.call("read_file", path="large.py", start_line=start, end_line=end)
                self.assertTrue(result["ok"])
                self.assertEqual(result["content"], "\n".join(lines[start - 1:end]))

    def test_line_range_skips_long_lines_without_splitting_line_numbers(self):
        path = self.workspace / "long_line.txt"
        path.write_text("x" * (MAX_FILE_BYTES * 3) + "\n目标\n最后一行", encoding="utf-8")
        result = self.call("read_file", path=path.name, start_line=2, end_line=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "目标\n最后一行")
        self.assertEqual(result["reference"], "long_line.txt:2-3")
        self.assertFalse(self.call("read_file", path=path.name, start_line=1, end_line=1)["ok"])

    def test_line_range_limit_counts_utf8_bytes(self):
        path = self.workspace / "unicode.txt"
        exact = "中" * (MAX_FILE_BYTES // 3) + "a"
        self.assertEqual(len(exact.encode("utf-8")), MAX_FILE_BYTES)
        path.write_text(exact, encoding="utf-8")
        self.assertEqual(self.call("read_file", path=path.name, start_line=1)["content"], exact)
        path.write_text(exact + "b", encoding="utf-8")
        result = self.call("read_file", path=path.name, start_line=1)
        self.assertFalse(result["ok"])
        self.assertIn("所选内容超过", result["error"])

    def test_line_range_accepts_bom_crlf_and_optional_endpoints(self):
        path = self.workspace / "windows.txt"
        path.write_bytes("甲\r\n乙\r\n丙\r\n".encode("utf-8-sig"))
        for arguments, expected in [
            ({"start_line": 2, "end_line": 2}, "乙"),
            ({"start_line": 2}, "乙\n丙"),
            ({"end_line": 2}, "甲\n乙"),
            ({"start_line": 2, "end_line": 99}, "乙\n丙"),
        ]:
            with self.subTest(arguments=arguments):
                result = self.call("read_file", path=path.name, **arguments)
                self.assertTrue(result["ok"])
                self.assertEqual(result["content"], expected)

    def test_line_range_rejects_invalid_or_missing_lines(self):
        path = self.workspace / "small.txt"
        path.write_text("one\ntwo\n", encoding="utf-8")
        for arguments in [
            {"start_line": 0}, {"end_line": False}, {"end_line": "2"},
            {"start_line": 2, "end_line": 1}, {"start_line": 3},
        ]:
            with self.subTest(arguments=arguments):
                self.assertFalse(self.call("read_file", path=path.name, **arguments)["ok"])
        path.write_text("", encoding="utf-8")
        self.assertFalse(self.call("read_file", path=path.name, start_line=1)["ok"])

    @unittest.skipUnless(shutil.which("git"), "需要 Git 验证忽略规则")
    def test_scans_follow_gitignore_but_keep_tracked_and_explicit_files(self):
        def git(*args):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git("init", "-q")
        (self.workspace / ".gitignore").write_text("ignored/\n*.log\n!keep.log\n", encoding="utf-8")
        for name in ("public.py", "ignored/noise.py", "skip.log", "keep.log", "tracked.log"):
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("needle\n", encoding="utf-8")
        git("add", "-f", "tracked.log")
        expected = {"public.py", "keep.log", "tracked.log"}
        for name in ("search_text", "search_code_index"):
            self.assertEqual({item["path"] for item in self.call(name, query="needle")["matches"]}, expected)
        self.assertEqual(set(self.call("find_files", pattern="*")["files"]), expected | {".gitignore"})
        self.assertNotIn("ignored/", self.call("list_directory")["tree"])
        self.assertEqual(self.call("read_file", path="ignored/noise.py")["content"], "needle\n")

    def test_no_match_search_stops_at_file_budget(self):
        for number in range(8):
            (self.workspace / f"{number}.txt").write_text("other text", encoding="utf-8")
        with patch("recapcli.tools.filesystem.ScanBudget", return_value=ScanBudget(max_files=2)):
            result = self.call("search_text", query="missing")
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["scanned_files"], 2)
        self.assertIn("文件数量", result["limit_reason"])
        self.assertIn("不能据此断言", result["warning"])

    def test_regex_whole_word_and_context_search(self):
        (self.workspace / "code.py").write_text("before\ncat = 1\nscatter = 2\nCAT = 3\nafter\n", encoding="utf-8")
        result = self.call("search_text", query="cat", whole_word=True, context_lines=1)
        self.assertEqual([item["line"] for item in result["matches"]], [2, 4])
        self.assertIn("1: before", result["matches"][0]["context"])
        self.assertIn("3: scatter = 2", result["matches"][0]["context"])
        self.assertEqual(len(self.call("search_text", query=r"^cat\s*=", regex=True)["matches"]), 2)
        self.assertEqual(self.call("search_text", query="cat.*")["matches"], [])
        self.assertFalse(self.call("search_text", query="[", regex=True)["ok"])

    def test_exact_result_limit_is_not_misreported_as_truncated(self):
        (self.workspace / "a.txt").write_text("hit\nhit\n", encoding="utf-8")
        for other, expected in [("other", False), ("hit", True)]:
            with self.subTest(other=other):
                (self.workspace / "b.txt").write_text(other, encoding="utf-8")
                result = self.call("search_text", query="hit", max_results=2)
                self.assertEqual(len(result["matches"]), 2)
                self.assertEqual(result["truncated"], expected)

    def test_directory_scan_is_bounded_before_sorting(self):
        for number in range(20):
            (self.workspace / f"{number}.txt").touch()
        yielded = []
        original_iterdir = Path.iterdir

        def counting_iterdir(path):
            for item in original_iterdir(path):
                yielded.append(item)
                yield item

        with patch("recapcli.tools.filesystem.ScanBudget", return_value=ScanBudget(max_entries=5)), \
                patch.object(Path, "iterdir", new=counting_iterdir):
            result = self.call("list_directory")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["entries"], 5)
        self.assertLessEqual(len(yielded), 6)
        self.assertIn("条目上限", result["limit_reason"])

    def test_search_stops_at_byte_and_time_budgets(self):
        (self.workspace / "file.txt").write_text("x" * 2000, encoding="utf-8")
        for budget, reason in [(ScanBudget(max_bytes=1000), "字节"), (ScanBudget(max_seconds=0), "时间")]:
            with self.subTest(reason=reason), patch("recapcli.tools.filesystem.ScanBudget", return_value=budget):
                result = self.call("search_text", query="missing")
            self.assertTrue(result["truncated"])
            self.assertIn(reason, result["limit_reason"])

    def test_line_range_cannot_scan_unbounded_prefix(self):
        (self.workspace / "file.txt").write_text("x\n" * 2000, encoding="utf-8")
        with patch("recapcli.tools.filesystem.ScanBudget", return_value=ScanBudget(max_bytes=1000)):
            result = self.call("read_file", path="file.txt", start_line=1900)
        self.assertFalse(result["ok"])
        self.assertIn("字节上限", result["error"])


if __name__ == "__main__":
    unittest.main()
