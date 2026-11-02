from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recapcli.mcp.config import load_mcp_config
from recapcli.mcp.manager import MCPManager
from recapcli.tui.driver import ShellDriver, ShellOptions


ROOT = Path(__file__).resolve().parents[2]


class MCPConfigTests(unittest.TestCase):
    def test_loads_multiple_servers_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mcp.json").write_text(json.dumps({
                "servers": {
                    "docs": {
                        "command": [sys.executable, "-m", "recapcli.mcp.repo_server",
                                    "--workspace", str(root)],
                        "cwd": str(ROOT),
                        "timeout": 5,
                        "max_result_bytes": 4096,
                        "prefix": "docs",
                    }
                }
            }), encoding="utf-8")
            servers = load_mcp_config(root / "mcp.json", workspace=root)
            self.assertEqual([name for name, _ in servers], ["docs"])
            self.assertEqual(servers[0][1]["timeout"], 5.0)
            self.assertEqual(servers[0][1]["max_result_bytes"], 4096)

    def test_rejects_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text('{"servers": {"bad": {"command": []}}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_mcp_config(path, workspace=directory)

    def test_manager_routes_builtin_and_configured_servers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (root / "mcp.json").write_text(json.dumps({
                "servers": {
                    "docs": {
                        "command": [sys.executable, "-m", "recapcli.mcp.repo_server",
                                    "--workspace", str(root)],
                        "cwd": str(ROOT),
                        "max_result_bytes": 4096,
                        "prefix": "repo_docs",
                        "tool_permissions": {"read_file": "read"},
                    }
                }
            }), encoding="utf-8")
            with MCPManager.from_options(root, repo=True, config_path=root / "mcp.json") as manager:
                self.assertEqual(len(manager.providers), 2)
                names = {item["function"]["name"] for item in manager.definitions}
                self.assertIn("mcp_repo_read_file", names)
                self.assertIn("mcp_repo_docs_read_file", names)
                builtin = manager.call("mcp_repo_read_file", '{"path": "README.md"}')
                configured = manager.call("mcp_repo_docs_read_file", '{"path": "README.md"}')
                self.assertTrue(builtin["ok"], builtin)
                self.assertTrue(configured["ok"], configured)

    def test_result_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("x" * 5000, encoding="utf-8")
            (root / "mcp.json").write_text(json.dumps({
                "servers": {
                    "tiny": {
                        "command": [sys.executable, "-m", "recapcli.mcp.repo_server",
                                    "--workspace", str(root)],
                        "cwd": str(ROOT),
                        "max_result_bytes": 200,
                        "tool_permissions": {"read_file": "read"},
                    }
                }
            }), encoding="utf-8")
            with MCPManager.from_options(root, config_path=root / "mcp.json") as manager:
                result = manager.call("mcp_tiny_read_file", '{"path": "README.md"}')
                self.assertFalse(result["ok"])
                self.assertIn("大小限制", result["error"])


    def test_shell_options_start_mcp_for_tui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            (root / "mcp.json").write_text("untrusted project config must not be loaded", encoding="utf-8")
            driver = ShellDriver(None, ShellOptions(workspace=root, mcp_repo=True),
                                 client_factory=lambda *args, **kwargs: None)
            try:
                notes = driver.prepare()
                self.assertIsNotNone(driver.mcp_manager)
                self.assertEqual(len(driver.mcp_manager.providers), 1)
                self.assertTrue(any("MCP" in note for note in notes))
            finally:
                driver.close()


if __name__ == "__main__":
    unittest.main()
