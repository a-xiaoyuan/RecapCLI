"""最小 MCP stdio 客户端，供 RecapCLI 的可选只读服务接入使用。"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

from ..tools.commands import subprocess_environment


class MCPClientError(RuntimeError):
    pass


class StdioMCPClient:
    """同步 stdio JSON-RPC 客户端；只实现 initialize / tools/list / tools/call。"""

    def __init__(self, command, *, cwd=None, timeout=10.0, env=None):
        self.timeout = timeout
        self._next_id = 0
        self._responses = queue.Queue()
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=subprocess_environment(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self):
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if line:
                    self._responses.put(line)
        except (OSError, UnicodeError, ValueError) as error:
            self._responses.put(MCPClientError(f"MCP 输出读取失败：{error}"))
        else:
            self._responses.put(MCPClientError("MCP 服务已断开连接"))

    def _send(self, message):
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise MCPClientError(f"MCP 发送失败：{error}") from error

    def request(self, method, params=None, *, timeout=None):
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + (self.timeout if timeout is None else min(self.timeout, timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPClientError(f"MCP 请求超时：{method}")
            try:
                line = self._responses.get(timeout=remaining)
            except queue.Empty as error:
                raise MCPClientError(f"MCP 请求超时：{method}") from error
            if isinstance(line, MCPClientError):
                raise line
            try:
                response = json.loads(line)
            except ValueError:
                continue
            if response.get("id") == request_id:
                if "error" in response:
                    raise MCPClientError(str(response["error"].get("message", "MCP 错误")))
                return response.get("result", {})

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "recapcli", "version": "0.6.0"},
        })
        if result.get("protocolVersion") != "2025-06-18":
            raise MCPClientError("MCP 服务未协商到支持的协议版本 2025-06-18")
        if "tools" not in result.get("capabilities", {}):
            raise MCPClientError("MCP 服务没有提供工具能力")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self):
        return self.request("tools/list", {})["tools"]

    def call_tool(self, name, arguments, *, timeout=None):
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def close(self):
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
