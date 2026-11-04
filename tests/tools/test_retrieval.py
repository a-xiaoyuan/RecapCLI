import os
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

from recapcli.tools import execute_tool, tool_definitions
from recapcli.tools import retrieval
from recapcli.tools.filesystem import ScanBudget


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()

    def call(self, tool, **arguments):
        return execute_tool(tool, arguments, self.workspace)

    def read_match(self, match):
        result = self.call("read_file", path=match["path"], start_line=match["start_line"], end_line=match["end_line"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reference"], match["reference"])
        return result["content"]

    def test_symbols_are_registered_and_locate_python_definitions(self):
        (self.workspace / "app.py").write_text("class Box:\n    def open(self):\n        return 1\n\nasync def fetch():\n    pass\n", encoding="utf-8")
        (self.workspace / "other").mkdir()
        (self.workspace / "other" / "app.py").write_text("async def fetch():\n    pass\n", encoding="utf-8")
        names = {item["function"]["name"] for item in tool_definitions()}
        self.assertTrue({"search_symbols", "search_code_index"} <= names)
        result = self.call("search_symbols", name="open", kind="function")
        self.assertTrue(result["ok"], result)
        match = result["matches"][0]
        self.assertEqual(match["reference"], "app.py:2-3")
        self.assertIn("def open", self.read_match(match))
        self.assertEqual(self.call("search_symbols", kind="class")["matches"][0]["name"], "Box")
        self.assertEqual(self.call("search_symbols", kind="async_function")["matches"][0]["name"], "fetch")
        scoped = self.call("search_symbols", name="fetch", path="other")["matches"]
        self.assertEqual([match["path"] for match in scoped], ["other/app.py"])
        self.assertEqual({match["name"] for match in self.call("search_symbols", path="app.py")["matches"]},
                         {"Box", "open", "fetch"})
        self.assertFalse(self.call("search_symbols", name="fetch", path="..")["ok"])
        self.assertFalse(self.call("search_symbols")["ok"])
        self.assertFalse(self.call("search_symbols", kind="unknown_kind")["ok"])

    def test_language_parsers_locate_definitions_with_real_line_ranges(self):
        samples = {
            "App.java": ("class Box { public int run() { return 1; } }", "run", "method"),
            "app.js": ("export const login = () => true;", "login", "function"),
            "app.ts": ("interface User { id: string; }", "User", "interface"),
            "app.tsx": ("export function View() { return <div/>; }", "View", "function"),
            "app.go": ("package main\nfunc Run() {}\n", "Run", "function"),
            "app.rs": ("struct Box {}", "Box", "struct"),
            "app.c": ("int run() { return 1; }", "run", "function"),
            "app.cpp": ("class Box { public: int run() { return 1; } };", "run", "function"),
            "App.cs": ("class Box { public int Run() { return 1; } }", "Run", "method"),
            "App.kt": ("class Box { fun run() {} }\n", "run", "function"),
            "app.rb": ("class Box\n def run\n  1\n end\nend\n", "run", "method"),
            "app.php": ("<?php class Box { public function run() { return 1; } }", "run", "method"),
        }
        for path, (source, _, _) in samples.items():
            (self.workspace / path).write_text(source, encoding="utf-8")
        for path, (_, name, kind) in samples.items():
            with self.subTest(path=path):
                result = self.call("search_symbols", name=name, kind=kind, path=path)
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(result["matches"]), 1, result)
                self.assertIn(name, self.read_match(result["matches"][0]))

    def test_external_index_keeps_scope_and_cached_results_separate(self):
        from recapcli.base.access import PathAccess
        (self.workspace / "local.py").write_text("# shared marker\ndef local(): pass\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "App.java"
            target.write_text("class App { int run() { return 1; } } // shared marker", encoding="utf-8")
            access = PathAccess(self.workspace, [other])

            def query(tool_name, **arguments):
                return execute_tool(tool_name, arguments, self.workspace, access_policy=access)

            symbols = query("search_symbols", name="run", path=other)
            self.assertEqual(symbols["matches"][0]["language"], "java")
            outside = query("search_code_index", query="shared marker", path=other)
            self.assertEqual(outside["matches"][0]["path"], target.resolve().as_posix())
            local = query("search_code_index", query="shared marker")
            self.assertEqual([match["path"] for match in local["matches"]], ["local.py"])
            self.assertTrue(query("search_symbols", name="run", path=other)["matches"])

    def test_repository_map_and_syntax_references_include_real_callers_and_revalidate_changes(self):
        (self.workspace / "service.py").write_text("def calculate(value):\n    return value + 1\n", encoding="utf-8")
        (self.workspace / "entry.py").write_text("from service import calculate\ndef main():\n    return calculate(2)\n", encoding="utf-8")
        (self.workspace / "Main.java").write_text("class Main { int call() { return calculate(2); } }", encoding="utf-8")
        (self.workspace / "ui.ts").write_text("function open() { return calculate(2); }", encoding="utf-8")
        result = self.call("find_references", name="calculate")
        self.assertEqual({item["path"] for item in result["matches"]}, {"entry.py", "Main.java", "ui.ts"})
        python = next(item for item in result["matches"] if item["path"] == "entry.py")
        self.assertEqual((python["caller"], python["line"]), ("main", 3))
        mapped = self.call("repository_map", query="calculate", max_bytes=2048)
        self.assertIn("def calculate(value)", mapped["map"])
        self.assertIn("service.py", next(item for item in mapped["files"] if item["path"] == "entry.py")["related_candidates"])
        self.assertLessEqual(mapped["bytes"], 2048)
        (self.workspace / "entry.py").write_text("# replaced\n", encoding="utf-8")
        self.assertNotIn("entry.py", {item["path"] for item in self.call("find_references", name="calculate")["matches"]})

    def test_fts_results_are_readable_and_persist(self):
        (self.workspace / "code.py").write_text("first\n# unique fox jumps\nlast\n", encoding="utf-8")
        first = self.call("search_code_index", query="fox jumps")
        self.assertTrue(first["ok"], first)
        match = first["matches"][0]
        self.assertEqual(match["reference"], "code.py:2")
        self.assertEqual(self.read_match(match), match["snippet"])
        db = self.workspace / ".recapcli/retrieval/index.sqlite3"
        self.assertTrue(db.is_file())
        connection = sqlite3.connect(db)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM files").fetchone()[0], 1)
        finally:
            connection.close()
        with patch.object(retrieval, "_index", side_effect=AssertionError("unchanged file reindexed")):
            self.assertEqual(self.call("search_code_index", query="fox jumps")["matches"], first["matches"])
        self.assertEqual(self.call("search_code_index", query='"fox"')["matches"][0]["path"], "code.py")

    def test_changed_same_size_same_mtime_deleted_and_invalid_python(self):
        path = self.workspace / "a.py"
        path.write_text("def old():\n    pass\n", encoding="utf-8")
        original = path.stat()
        self.assertEqual(self.call("search_symbols", name="old")["matches"][0]["start_line"], 1)
        path.write_text("\ndef new():\n   pass\n", encoding="utf-8")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.call("search_symbols", name="old")["matches"], [])
        match = self.call("search_symbols", name="new")["matches"][0]
        self.assertEqual(match["start_line"], 2)
        self.assertIn("def new", self.read_match(match))
        self.assertEqual(self.call("search_code_index", query="old")["matches"], [])
        self.assertEqual(self.call("search_code_index", query="new")["matches"][0]["start_line"], 2)
        path.write_text("def broken(\n", encoding="utf-8")
        self.assertEqual(self.call("search_symbols", name="new")["matches"], [])
        self.assertEqual(len(self.call("search_code_index", query="broken")["matches"]), 1)
        path.unlink()
        self.assertEqual(self.call("search_code_index", query="broken")["matches"], [])

    def test_relevance_ranking_and_cached_queries_avoid_rescan(self):
        (self.workspace / "a.txt").write_text("marker " + "noise " * 80, encoding="utf-8")
        (self.workspace / "z.java").write_text("marker marker marker", encoding="utf-8")
        first = self.call("search_code_index", query="marker")
        self.assertEqual(first["matches"][0]["path"], "z.java")
        with patch.object(retrieval, "_refresh", side_effect=AssertionError("cached query rescanned")):
            second = self.call("search_code_index", query="marker")
        self.assertTrue(second["cached"])
        self.assertEqual(second["scanned_files"], 0)
        self.assertEqual(second["matches"], first["matches"])
        self.assertFalse(self.call("search_code_index", query="marker", refresh=True)["cached"])
        (self.workspace / "new.txt").write_text("new_keyword", encoding="utf-8")
        self.assertEqual(self.call("search_code_index", query="new_keyword")["matches"][0]["path"], "new.txt")

    def test_result_changed_during_query_is_not_returned(self):
        path = self.workspace / "a.py"
        path.write_text("def old():\n    pass\n", encoding="utf-8")
        real_read = retrieval._read_source
        calls = 0
        def change_before_validation(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text("\ndef new():\n    pass\n", encoding="utf-8")
            return real_read(*args, **kwargs)
        with patch.object(retrieval, "_read_source", side_effect=change_before_validation):
            result = self.call("search_symbols", name="old")
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["stale_files"])
        self.assertTrue(result["truncated"])
        self.assertEqual(self.call("search_symbols", name="new")["matches"][0]["start_line"], 2)

    def test_partial_scan_preserves_and_revalidates_unscanned_cached_entries(self):
        for name in ("a.py", "b.py"):
            (self.workspace / name).write_text("def hit():\n    pass\n", encoding="utf-8")
        self.assertEqual(len(self.call("search_symbols", name="hit")["matches"]), 2)
        with patch.object(retrieval, "ScanBudget", side_effect=lambda: ScanBudget(max_files=1)):
            result = self.call("search_symbols", name="hit", refresh=True)
        self.assertTrue(result["truncated"])
        self.assertIn("limit_reason", result)
        self.assertEqual(len(result["matches"]), 2)
        connection = sqlite3.connect(self.workspace / ".recapcli/retrieval/index.sqlite3")
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM files").fetchone()[0], 2)
        finally:
            connection.close()

    def test_partial_scan_never_returns_changed_or_deleted_cached_entries(self):
        for name in ("a.py", "b.py", "c.py"):
            (self.workspace / name).write_text("def hit():\n    pass\n", encoding="utf-8")
        self.assertEqual(len(self.call("search_symbols", name="hit")["matches"]), 3)
        (self.workspace / "b.py").write_text("def changed():\n    pass\n", encoding="utf-8")
        (self.workspace / "c.py").unlink()
        with patch.object(retrieval, "ScanBudget", side_effect=lambda: ScanBudget(max_files=1)):
            result = self.call("search_symbols", name="hit", refresh=True)
        self.assertEqual([item["path"] for item in result["matches"]], ["a.py"])
        self.assertTrue(result["stale_files"])
        self.assertTrue(result["truncated"])
        self.assertEqual(self.call("search_symbols", name="changed")["matches"][0]["path"], "b.py")

    def test_budget_expiry_during_indexing_rolls_back_partial_file(self):
        path = self.workspace / "a.py"
        path.write_text("def old():\n    pass\n", encoding="utf-8")
        self.call("search_symbols", name="old")
        path.write_text("def new():\n    pass\n", encoding="utf-8")
        connection = retrieval._connect(self.workspace)
        def interrupted(db, relative, digest, text, budget):
            retrieval._delete(db, relative)
            budget.stop("test indexing deadline")
        try:
            with connection, patch.object(retrieval, "_index", side_effect=interrupted):
                budget, _ = retrieval._refresh(connection, self.workspace)
            self.assertTrue(budget.reason)
            self.assertEqual(connection.execute("SELECT name FROM symbols").fetchone()[0], "old")
        finally:
            connection.close()

    def test_exclusions_binary_and_result_limit(self):
        (self.workspace / ".env").write_text("sensitive marker", encoding="utf-8")
        (self.workspace / "secret.pem").write_text("sensitive marker", encoding="utf-8")
        (self.workspace / "binary").write_bytes(b"sensitive\x00marker")
        (self.workspace / "a.txt").write_text("marker\nmarker\n", encoding="utf-8")
        result = self.call("search_code_index", query="marker", max_results=1)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["path"], "a.txt")
        self.assertTrue(result["truncated"])
        self.assertFalse(self.call("search_code_index", query=" ")["ok"])
        self.assertFalse(self.call("search_code_index", query="marker", max_results=0)["ok"])

    def test_refuses_index_symlink_and_does_not_index_external_link(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.py"
            outside.write_text("def outside():\n    pass\n", encoding="utf-8")
            link = self.workspace / "linked.py"
            try:
                link.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"无法创建符号链接：{error}")
            self.assertEqual(self.call("search_symbols", name="outside")["matches"], [])
            index = self.workspace / ".recapcli/retrieval/index.sqlite3"
            index.unlink()
            index.symlink_to(outside)
            result = self.call("search_symbols", name="outside")
            self.assertFalse(result["ok"])
            self.assertEqual(outside.read_text(encoding="utf-8"), "def outside():\n    pass\n")

    def test_refuses_index_link_even_without_symlink_creation_privilege(self):
        index = self.workspace / ".recapcli/retrieval/index.sqlite3"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == index or original(path)):
            result = self.call("search_symbols", name="x")
        self.assertFalse(result["ok"])
        self.assertFalse(index.exists())

    def test_fts_database_errors_are_structured(self):
        with patch.object(retrieval.sqlite3, "connect", side_effect=sqlite3.OperationalError("FTS unavailable")):
            result = self.call("search_code_index", query="word")
        self.assertFalse(result["ok"])
        self.assertIn("FTS unavailable", result["error"])

    def test_hybrid_semantic_recall_caches_vectors_and_refreshes_changed_files(self):
        (self.workspace / "auth.py").write_text("def authenticate():\n    return True\n", encoding="utf-8")
        (self.workspace / "math.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        requests = []

        def handler(request):
            data = json.loads(request.content)
            requests.append(data)
            return httpx2.Response(200, json={"object": "list", "model": "embedding-test", "data": [
                {"object": "embedding", "index": index,
                 "embedding": [1.0, 0.0] if "auth" in text or "身份" in text else [0.0, 1.0]}
                for index, text in enumerate(data["input"])]})

        def factory(**options):
            return OpenAI(**options, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

        values = {"RECAPCLI_EMBEDDING_MODEL": "embedding-test", "RECAPCLI_EMBEDDING_BASE_URL": "https://offline.invalid/v1"}
        with patch("recapcli.llm.embeddings.load_values", return_value=values), \
                patch("recapcli.llm.embeddings.OpenAI", side_effect=factory):
            first = self.call("search_code_index", query="身份验证")
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["semantic_status"], "enabled")
            match = first["matches"][0]
            self.assertEqual(match["path"], "auth.py")
            self.assertEqual(match["sources"], ["semantic"])
            self.assertEqual(self.read_match(match), match["snippet"])
            self.call("search_code_index", query="身份验证")
            self.assertEqual(len(requests), 3)  # 文档批次一次，两次查询向量。
            (self.workspace / "auth.py").write_text("def authenticate():\n    return False\n", encoding="utf-8")
            refreshed = self.call("search_code_index", query="身份验证", refresh=True)
            self.assertIn("False", refreshed["matches"][0]["snippet"])
            self.assertEqual(len(requests), 5)
            self.assertTrue(all(request["encoding_format"] == "float" for request in requests))

    def test_hybrid_without_embedding_uses_names_and_never_calls_provider(self):
        (self.workspace / "token_store.py").write_text("def save_token():\n    return 1\n", encoding="utf-8")
        with patch("recapcli.llm.embeddings.load_values", return_value={}), \
                patch("recapcli.llm.embeddings.OpenAI") as provider:
            result = self.call("search_code_index", query="token save")
            self.assertTrue(result["matches"], result)
            self.assertEqual(result["semantic_status"], "disabled")
            self.assertTrue(any("name" in match["sources"] for match in result["matches"]))
            provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
