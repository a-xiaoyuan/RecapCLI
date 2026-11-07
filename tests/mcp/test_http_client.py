from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx2

from recapcli.mcp.client import PROTOCOL_VERSION
from recapcli.mcp.config import load_mcp_config
from recapcli.mcp.manager import MCPManager
from recapcli.tui.driver import ShellDriver, ShellOptions
from recapcli.tui.launch import dispatch


URL = "http://127.0.0.1:8123/mcp"


class ChunkStream(httpx2.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


class StreamableHTTPTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.workspace / "mcp.json"
        self.requests = []
        self.session_number = 0
        self.call_response = None
        real_client = httpx2.Client
        transport = httpx2.MockTransport(self.handle_request)
        self.enterContext(patch("recapcli.mcp.http_client.httpx2.Client",
                                side_effect=lambda **options: real_client(transport=transport, **options)))
        self.write_config()

    def write_config(self, **config):
        self.config_path.write_text(json.dumps({"servers": {"remote": {
            "url": URL, "tool_permissions": {"lookup": "read", "save": "write"}, **config,
        }}}), encoding="utf-8")

    def manager(self, **options):
        return MCPManager.from_options(self.workspace, config_path=self.config_path, **options)

    @staticmethod
    def reply(message, result, **kwargs):
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}, **kwargs)

    def handle_request(self, request):
        self.assertEqual(str(request.url), URL)
        self.assertEqual(request.headers["Accept"], "application/json, text/event-stream")
        if request.method == "DELETE":
            self.requests.append(("DELETE", request, {}))
            return httpx2.Response(405)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        message = json.loads(request.content)
        method = message["method"]
        self.requests.append((method, request, message))
        if method == "initialize":
            self.assertNotIn("Mcp-Session-Id", request.headers)
            self.assertEqual(message["params"]["protocolVersion"], PROTOCOL_VERSION)
            self.session_number += 1
            return self.reply(message, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}},
                              headers={"Mcp-Session-Id": f"session-{self.session_number}"})
        self.assertEqual(request.headers["Mcp-Session-Id"], f"session-{self.session_number}")
        self.assertEqual(request.headers["MCP-Protocol-Version"], PROTOCOL_VERSION)
        if method == "notifications/initialized":
            self.assertNotIn("id", message)
            return httpx2.Response(202)
        if method == "tools/list":
            if message["params"].get("cursor") == "page-two":
                return self.reply(message, {"tools": [{"name": "save"}, {"name": "unclassified",
                                   "annotations": {"readOnlyHint": True}}]})
            return self.reply(message, {"tools": [{"name": "lookup"}], "nextCursor": "page-two"})
        self.assertEqual(method, "tools/call")
        if self.call_response is not None:
            return self.call_response(request, message)
        return self.reply(message, {"structuredContent": {"answer": message["params"]["arguments"].get("text")}})

    def test_mixed_services_auth_json_and_approvals(self):
        self.write_config(bearer_token_env="RECAP_TEST_MCP_TOKEN")
        with patch.dict("os.environ", {"RECAP_TEST_MCP_TOKEN": "fixture-token"}), self.manager(repo=True) as manager:
            names = {item["function"]["name"] for item in manager.definitions}
            self.assertTrue({"mcp_repo_read_file", "mcp_remote_lookup", "mcp_remote_save"} <= names)
            self.assertIn("--allow-exec", manager.call("mcp_remote_unclassified", {})["error"])
            rejected = manager.call("mcp_remote_save", {}, capabilities=("write",), approver=lambda _: "rejected")
            self.assertFalse(rejected["ok"])
            self.assertFalse(any(method == "tools/call" for method, _, _ in self.requests))
            result = manager.call("mcp_remote_lookup", {"text": "中文结果"})
            self.assertEqual(result, {"ok": True, "answer": "中文结果"})
        self.assertEqual([method for method, _, _ in self.requests], [
            "initialize", "notifications/initialized", "tools/list", "tools/list", "tools/call", "DELETE",
        ])
        for _, request, _ in self.requests:
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-token")
        self.assertEqual(self.requests[-1][1].headers["Mcp-Session-Id"], "session-1")

    def test_sse_matches_response_and_does_not_wait_for_stream_end(self):
        def streamed(_, message):
            request_id = message["id"]
            body = ('\ufeff: 心跳\r\n\r\n'
                    'event: message\rdata: {"jsonrpc":"2.0","method":"notifications/message"}\r\r'
                    f'data: {{"jsonrpc":"2.0","id":{request_id + 1},"result":{{}}}}\n\n'
                    f'data: {{"jsonrpc":"2.0","id":{request_id},\r\n'
                    'data: "result":{"structuredContent":{"answer":"中文"}}}\r\n\r\n').encode("utf-8")

            def chunks():
                for index in range(len(body)):
                    yield body[index:index + 1]
                raise AssertionError("工具结果收到后仍在读取 SSE")

            return httpx2.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"},
                                   stream=ChunkStream(chunks()))

        self.call_response = streamed
        with self.manager() as manager:
            self.assertEqual(manager.call("mcp_remote_lookup", {}), {"ok": True, "answer": "中文"})

    def test_timeout_disconnect_and_gateway_failure_are_unknown_without_replay(self):
        def fail(request, _):
            if outcome == "timeout":
                raise httpx2.ReadTimeout("fake secret should stay out of errors", request=request)
            if outcome == "disconnect":
                return httpx2.Response(200, headers={"Content-Type": "text/event-stream"},
                                       stream=ChunkStream([b": heartbeat\n\n"]))
            return httpx2.Response(502)

        self.call_response = fail
        with self.manager() as manager:
            for count, outcome in enumerate(("timeout", "disconnect", "gateway"), 1):
                with self.subTest(outcome=outcome):
                    result = manager.call("mcp_remote_lookup", {})
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "unknown")
                    self.assertIn("执行结果未知", result["error"])
                    self.assertNotIn("fake secret", result["error"])
                    self.assertEqual(sum(method == "tools/call" for method, _, _ in self.requests), count)

    def test_heartbeats_respect_remaining_time(self):
        clock = SimpleNamespace(now=0.0)

        def streamed(_, __):
            def chunks():
                for _ in range(5):
                    clock.now += 0.1
                    yield b": heartbeat\n\n"
            return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, stream=ChunkStream(chunks()))

        self.call_response = streamed
        with self.manager() as manager, patch("recapcli.mcp.http_client.time", SimpleNamespace(monotonic=lambda: clock.now)):
            result = manager.call("mcp_remote_lookup", {}, time_remaining=0.2)
        self.assertIn("请求超时", result["error"])
        self.assertEqual(result["status"], "unknown")
        self.assertLessEqual(clock.now, 0.3)

    def test_expired_session_is_reopened_only_for_next_call(self):
        self.call_response = lambda request, message: httpx2.Response(404)
        with self.manager() as manager:
            result = manager.call("mcp_remote_lookup", {})
            self.assertIn("会话已失效", result["error"])
            self.assertEqual(self.session_number, 1)
            self.assertEqual(sum(method == "tools/call" for method, _, _ in self.requests), 1)
            self.call_response = None
            self.assertTrue(manager.call("mcp_remote_lookup", {})["ok"])
            self.assertEqual(self.session_number, 2)

    def test_size_limits_cover_tool_output_and_stream_before_parsing(self):
        self.write_config(max_result_bytes=120)
        with self.manager() as manager:
            result = manager.call("mcp_remote_lookup", {"text": "长" * 200})
            self.assertFalse(result["ok"])
            self.assertIn("大小限制", result["error"])
            self.call_response = lambda request, message: httpx2.Response(
                200, headers={"Content-Type": "text/event-stream"},
                stream=ChunkStream([b"data: " + b"x" * (128 * 1024)]),
            )
            result = manager.call("mcp_remote_lookup", {})
            self.assertEqual(result["status"], "unknown")
            self.assertIn("大小限制", result["error"])

    def test_redirect_is_not_followed(self):
        self.call_response = lambda request, message: httpx2.Response(307, headers={"Location": "https://other.test/mcp"})
        with self.manager() as manager:
            result = manager.call("mcp_remote_lookup", {})
            self.assertFalse(result["ok"])
            self.assertIn("HTTP 307", result["error"])

    def test_lists_remote_tools_in_cli_and_tui_without_model(self):
        args = ["--mcp-tools", "--workspace", str(self.workspace), "--mcp-config", str(self.config_path)]
        output = io.StringIO()
        with patch("recapcli.cli.load_config") as load_model_config, contextlib.redirect_stdout(output):
            self.assertEqual(dispatch(args), 0)
        load_model_config.assert_not_called()
        self.assertIn("mcp_remote_lookup", output.getvalue())
        self.assertFalse((self.workspace / ".recapcli").exists())

        messages = []
        driver = ShellDriver(SimpleNamespace(info=messages.append),
                             ShellOptions(workspace=self.workspace, mcp_config=self.config_path),
                             client_factory=lambda *args, **kwargs: self.fail("查看 MCP 工具时创建了模型客户端"))
        self.addCleanup(driver.close)
        self.assertIn("已连接 1 个服务", "\n".join(driver.prepare()))
        driver.show_mcp()
        self.assertIn("mcp_remote_lookup", messages[-1])

    def test_rejects_invalid_config_and_missing_token_before_connecting(self):
        for config in ({"url": "http://example.test/mcp"}, {"url": "https://name:password@example.test/mcp"},
                       {"url": "/mcp"}, {"url": None}, {"command": ["unused"]},
                       {"bearer_token_env": ""}, {"env": {}}, {"timeout": float("inf")}):
            with self.subTest(config=config):
                self.write_config(**config)
                with self.assertRaises(ValueError) as error:
                    load_mcp_config(self.config_path)
                self.assertNotIn("password", str(error.exception))
        self.write_config(bearer_token_env="RECAP_TEST_MCP_TOKEN")
        with patch.dict("os.environ", {"RECAP_TEST_MCP_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "RECAP_TEST_MCP_TOKEN"):
                self.manager()
        self.assertEqual(self.requests, [])
