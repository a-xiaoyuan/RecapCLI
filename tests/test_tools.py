import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archer.tools import execute_tool
from archer.tools.filesystem import MAX_FILE_BYTES, ScanBudget
from archer.tools.registry import TOOL_REGISTRY, tool_definitions


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

    def test_search_never_opens_archer_credentials(self):
        credentials = self.workspace / ".env"
        credentials.write_text("OPENAI_API_KEY=offline-private-marker\n", encoding="utf-8")
        (self.workspace / "config_example.py").write_text("OPENAI_API_KEY is required", encoding="utf-8")
        real_open = Path.open

        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path.resolve(), credentials)
            return real_open(path, *args, **kwargs)

        with patch("archer.tools.filesystem.ENV_FILE", credentials), patch.object(Path, "open", new=guarded_open):
            self.assertFalse(self.call("read_file", path=".env")["ok"])
            self.assertFalse(self.call("search_text", query="OPENAI_API_KEY", path=".env")["ok"])
            result = self.call("search_text", query="OPENAI_API_KEY")
        self.assertTrue(result["ok"])
        self.assertEqual([match["path"] for match in result["matches"]], ["config_example.py"])
        self.assertNotIn("offline-private-marker", json.dumps(result))

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
        self.assertEqual([match["path"] for match in searched["matches"]], ["public.txt"])
        self.assertNotIn("linked", listed["tree"])
        self.assertNotIn("private.txt", listed["tree"])

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
        self.assertEqual(result, {"ok": True, "content": "目标\n最后一行"})
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
                self.assertEqual(self.call("read_file", path=path.name, **arguments), {"ok": True, "content": expected})

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

    def test_line_range_reads_in_bounded_chunks(self):
        class BoundedReader(io.StringIO):
            def readline(self, size=-1):
                if size < 0 or size > MAX_FILE_BYTES + 1:
                    raise AssertionError("行读取必须有长度上限")
                return super().readline(size)

            def read(self, size=-1):
                raise AssertionError("按行读取不能一次加载整个文件")

        path = self.workspace / "stream.txt"
        path.touch()
        stream = BoundedReader("x" * (MAX_FILE_BYTES * 3) + "\nselected\n")
        with patch.object(Path, "open", return_value=stream):
            self.assertEqual(self.call("read_file", path=path.name, start_line=2),
                             {"ok": True, "content": "selected"})

    def test_missing_tool_handler_is_explicit_error_not_directory_fallback(self):
        spec = {"description": "未来工具", "parameters": {"type": "object", "properties": {}}}
        with patch.dict(TOOL_REGISTRY, {"future_tool": spec}):
            result = self.call("future_tool")
            self.assertFalse(result["ok"])
            self.assertIn("未注册执行函数", result["error"])
            self.assertNotIn("tree", result)
            with self.assertRaises(ValueError):
                tool_definitions()

    def test_new_tool_schema_and_execution_share_one_registration(self):
        received = []

        def handler(workspace, text):
            received.append((workspace, text))
            return {"echo": text}

        spec = {"description": "测试工具", "handler": handler, "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
            "additionalProperties": False,
        }}
        with patch.dict(TOOL_REGISTRY, {"echo": spec}):
            self.assertIn("echo", [item["function"]["name"] for item in tool_definitions()])
            self.assertEqual(self.call("echo", text="hello"), {"ok": True, "echo": "hello"})
            self.assertFalse(self.call("echo")["ok"])
            self.assertFalse(self.call("echo", text="x", extra=True)["ok"])
            self.assertFalse(self.call("echo", text=1)["ok"])
        self.assertEqual(received, [(self.workspace, "hello")])

    def test_no_match_search_stops_at_file_budget(self):
        for number in range(8):
            (self.workspace / f"{number}.txt").write_text("other text", encoding="utf-8")
        with patch("archer.tools.filesystem.ScanBudget", return_value=ScanBudget(max_files=2)):
            result = self.call("search_text", query="missing")
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["scanned_files"], 2)
        self.assertIn("文件数量", result["limit_reason"])

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

        with patch("archer.tools.filesystem.ScanBudget", return_value=ScanBudget(max_entries=5)), \
                patch.object(Path, "iterdir", new=counting_iterdir):
            result = self.call("list_directory")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["entries"], 5)
        self.assertLessEqual(len(yielded), 6)
        self.assertIn("条目上限", result["limit_reason"])

    def test_search_stops_at_byte_and_time_budgets(self):
        (self.workspace / "file.txt").write_text("x" * 2000, encoding="utf-8")
        for budget, reason in [(ScanBudget(max_bytes=1000), "字节"), (ScanBudget(max_seconds=0), "时间")]:
            with self.subTest(reason=reason), patch("archer.tools.filesystem.ScanBudget", return_value=budget):
                result = self.call("search_text", query="missing")
            self.assertTrue(result["truncated"])
            self.assertIn(reason, result["limit_reason"])

    def test_line_range_cannot_scan_unbounded_prefix(self):
        (self.workspace / "file.txt").write_text("x\n" * 2000, encoding="utf-8")
        with patch("archer.tools.filesystem.ScanBudget", return_value=ScanBudget(max_bytes=1000)):
            result = self.call("read_file", path="file.txt", start_line=1900)
        self.assertFalse(result["ok"])
        self.assertIn("字节上限", result["error"])


if __name__ == "__main__":
    unittest.main()
