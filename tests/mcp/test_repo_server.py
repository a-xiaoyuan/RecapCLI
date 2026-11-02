from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recapcli.mcp.client import MCPClientError, StdioMCPClient


ROOT = Path(__file__).resolve().parents[2]


class RepoMCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "README.md").write_text(
            "# Demo\n\nalpha marker\nbeta\n", encoding="utf-8")
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "app.py").write_text(
            "def alpha():\n    return 1\n", encoding="utf-8")
        self.client = StdioMCPClient(
            [sys.executable, "-m", "recapcli.mcp.repo_server", "--workspace", str(self.workspace)],
            cwd=str(ROOT),
        )
        self.client.initialize()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_lists_only_read_only_tools(self):
        tools = self.client.list_tools()
        names = {tool["name"] for tool in tools}
        self.assertIn("read_file", names)
        self.assertIn("search_text", names)
        self.assertNotIn("edit_file", names)
        self.assertNotIn("run_command", names)

    def test_calls_read_and_search_tools(self):
        read = self.client.call_tool("read_file", {"path": "README.md"})
        self.assertFalse(read["isError"])
        payload = read["structuredContent"]
        self.assertEqual(payload["path"], "README.md")
        self.assertIn("alpha marker", payload["content"])

        search = self.client.call_tool("search_text", {"query": "alpha"})
        self.assertFalse(search["isError"])
        matches = search["structuredContent"]["matches"]
        self.assertTrue(any("README.md" in item["reference"] for item in matches))

    def test_rejects_unknown_tools_and_outside_paths(self):
        with self.assertRaises(MCPClientError):
            self.client.call_tool("edit_file", {"path": "README.md"})
        outside = Path(self.temp.name).parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            result = self.client.call_tool("read_file", {"path": "../outside.txt"})
            self.assertTrue(result["isError"])
            self.assertFalse(result["structuredContent"]["ok"])
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
