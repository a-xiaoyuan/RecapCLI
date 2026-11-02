from __future__ import annotations

import tempfile
import unittest
import os
import sys
from pathlib import Path
from unittest.mock import patch

from recapcli.mcp.client import MCPClientError, StdioMCPClient
from recapcli.mcp.provider import PREFIX, MCPToolProvider
from recapcli.tools.approval import approve_all


STRICT_SERVER = r'''
import json, os, sys
from pathlib import Path
phase = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'initialize':
        phase = 1
        result = {'protocolVersion':'2025-06-18', 'capabilities':{'tools':{}}, 'serverInfo':{'name':'strict-fixture','version':'1'}}
    elif method == 'notifications/initialized':
        phase = 2
        continue
    elif phase != 2:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32000,'message':'initialize required'}}), flush=True)
        continue
    elif method == 'tools/list':
        result = {'tools':[{'name':name,'inputSchema':{'type':'object','properties':{}},'annotations':{'readOnlyHint':True}}
                           for name in ('lookup','write_note','unclassified')]}
    elif request['params']['name'] == 'lookup':
        if request['params']['arguments'].get('fail'):
            result = {'isError':True,'content':[{'type':'text','text':'中文错误'}]}
        else:
            result = {'content':[{'type':'text','text':'中文结果'}], 'structuredContent':{'answer':'中文结果',
                      'inherited_secret':bool(os.environ.get('GITHUB_TOKEN')), 'explicit':os.environ.get('MCP_EXPLICIT_TEST')}}
    else:
        Path('write-marker.txt').write_text('written', encoding='utf-8')
        result = {'content':[], 'structuredContent':{'saved':True}}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}, ensure_ascii=False), flush=True)
'''


class MCPProviderTests(unittest.TestCase):
    def test_exposes_prefixed_read_only_tools_and_calls_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# demo\n", encoding="utf-8")
            with MCPToolProvider(root) as provider:
                names = {item["function"]["name"] for item in provider.definitions}
                self.assertIn(PREFIX + "read_file", names)
                self.assertNotIn(PREFIX + "edit_file", names)
                result = provider.call(PREFIX + "read_file", '{"path": "README.md"}')
                self.assertTrue(result["ok"], result)
                self.assertIn("demo", result["content"])

    def test_external_permissions_handshake_unicode_and_standard_results(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "GITHUB_TOKEN": "fake-inherited-token", "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp936"}):
            root = Path(directory)
            marker = root / "write-marker.txt"
            with MCPToolProvider(root, command=[sys.executable, "-c", STRICT_SERVER], cwd=root, prefix="probe",
                                 env={"MCP_EXPLICIT_TEST": "explicit-test-only"},
                                 tool_permissions={"lookup": "read", "write_note": "write"}) as provider:
                read = provider.call("mcp_probe_lookup", {})
                self.assertTrue(read["ok"], read)
                self.assertEqual(read["answer"], "中文结果")
                self.assertFalse(read["inherited_secret"])
                self.assertEqual(read["explicit"], "explicit-test-only")
                failed = provider.call("mcp_probe_lookup", {"fail": True})
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["error"], "中文错误")

                for name in ("mcp_probe_write_note", "mcp_probe_unclassified"):
                    denied = provider.call(name, {}, approver=approve_all)
                    self.assertFalse(denied["ok"])
                    self.assertFalse(marker.exists())
                waiting = provider.call("mcp_probe_write_note", {}, capabilities=("read", "write"))
                self.assertEqual(waiting["approval"], "needs_approval")
                rejected = provider.call("mcp_probe_write_note", {}, capabilities=("read", "write"),
                                         approver=lambda request: "rejected")
                self.assertFalse(rejected["ok"])
                self.assertFalse(marker.exists())
                approvals = []
                written = provider.call("mcp_probe_write_note", {}, capabilities=("read", "write"),
                                        approver=approve_all, on_approval=approvals.append)
                self.assertTrue(written["ok"], written)
                self.assertTrue(marker.exists())
                self.assertEqual(approvals[-1]["outcome"], "approved")

    def test_reader_reports_decode_errors_and_disconnects_as_connection_errors(self):
        for script in ("import sys; sys.stdin.readline()",
                       "import sys; sys.stdin.readline(); sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.buffer.flush()"):
            with self.subTest(script=script):
                client = StdioMCPClient([sys.executable, "-c", script], timeout=2)
                try:
                    with self.assertRaisesRegex(MCPClientError, "输出读取失败|已断开连接"):
                        client.list_tools()
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
