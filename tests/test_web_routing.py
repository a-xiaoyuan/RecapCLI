"""两条联网入口合并后的 schema 与实际分发保持一致。"""

import unittest
from unittest.mock import Mock, patch

from recapcli.tools import execute_tool, tool_definitions
from recapcli.tools import registry


class WebRoutingTests(unittest.TestCase):
    def schema(self, capabilities):
        definitions = tool_definitions(capabilities)
        names = [item["function"]["name"] for item in definitions]
        self.assertEqual(len(names), len(set(names)))
        return {item["function"]["name"]: item["function"]["parameters"] for item in definitions}

    def test_schema_matches_selected_backend_and_exposes_both_readers(self):
        web = self.schema({"read", "web"})
        network = self.schema({"read", "network"})
        both = self.schema({"read", "web", "network"})
        self.assertNotIn("timeout_seconds", web["web_search"]["properties"])
        self.assertIn("timeout_seconds", network["web_search"]["properties"])
        self.assertEqual(both["web_search"], network["web_search"])
        self.assertTrue({"read_url", "fetch_url", "web_search"} <= set(both))

    def test_both_flags_use_network_and_web_only_keeps_original_backend(self):
        network_handler = Mock(return_value={"provider": "network"})
        web_handler = Mock(return_value={"provider": "web"})
        with patch.dict(registry.TOOL_REGISTRY["web_search"], handler=network_handler), \
                patch.dict(registry._WEB_SEARCH_SPEC, handler=web_handler):
            both = execute_tool("web_search", {"query": "test"}, ".", capabilities={"network", "web"})
            web = execute_tool("web_search", {"query": "test"}, ".", capabilities={"web"})
        self.assertEqual(both, {"ok": True, "provider": "network"})
        self.assertEqual(web, {"ok": True, "provider": "web"})
        network_handler.assert_called_once()
        web_handler.assert_called_once()

    def test_disabled_search_cannot_reach_either_backend(self):
        with patch.dict(registry.TOOL_REGISTRY["web_search"], handler=Mock()) as _, \
                patch.dict(registry._WEB_SEARCH_SPEC, handler=Mock()):
            result = execute_tool("web_search", {"query": "test"}, ".", capabilities={"read"})
            self.assertFalse(result["ok"])
            registry.TOOL_REGISTRY["web_search"]["handler"].assert_not_called()
            registry._WEB_SEARCH_SPEC["handler"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
