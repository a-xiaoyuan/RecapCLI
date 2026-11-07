"""MCP Streamable HTTP 客户端，只开启工具请求所需的 POST 流。"""

from __future__ import annotations

from codecs import BOM_UTF8
from ipaddress import ip_address
import json
import os
import re
import time

import httpx2

from .client import MCPClientError, PROTOCOL_VERSION, initialize_tools


CONTROL_RESPONSE_LIMIT = 1024 * 1024
TOOL_RESPONSE_OVERHEAD = 64 * 1024
SESSION_HEADER = "Mcp-Session-Id"
SSE_LINE_END = re.compile(rb"\r\n?|\n")


class MCPHTTPError(MCPClientError):
    def __init__(self, message, *, unknown=False):
        super().__init__(message)
        self.unknown = unknown


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def parse_mcp_url(value):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("url 必须是非空的 HTTP 地址")
    try:
        url = httpx2.URL(value)
    except httpx2.InvalidURL:
        raise ValueError("url 不是有效的 HTTP 地址") from None
    if not url.is_absolute_url or url.userinfo or "#" in value:
        raise ValueError("url 必须是绝对地址，且不能包含账号密码或片段")
    if url.scheme != "https" and not (url.scheme == "http" and _is_loopback(url.host)):
        raise ValueError("url 必须使用 HTTPS，本机回环地址可用 HTTP")
    return url


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _bounded_chunks(response, deadline, limit):
    size = 0
    for chunk in response.iter_bytes():
        _remaining(deadline)
        size += len(chunk)
        if size > limit:
            raise ValueError("MCP 响应超过大小限制")
        yield chunk
    _remaining(deadline)


def _sse_messages(chunks):
    line_buffer = bytearray()
    data = []
    event_type = b""
    first_line = True
    previous_cr = False
    for chunk in chunks:
        # CR 已经结束上一行。跨块的 LF 属于同一个换行符。
        if previous_cr:
            chunk = chunk.removeprefix(b"\n")
        previous_cr = chunk.endswith(b"\r")
        start = 0
        for end in SSE_LINE_END.finditer(chunk):
            line_buffer.extend(chunk[start:end.start()])
            start = end.end()
            line = bytes(line_buffer)
            line_buffer.clear()
            if first_line:
                line = line.removeprefix(BOM_UTF8)
                first_line = False
            if not line:
                if data and event_type in (b"", b"message"):
                    message = b"\n".join(data)
                    if message:
                        yield json.loads(message.decode("utf-8"))
                data.clear()
                event_type = b""
                continue
            field, _, value = line.partition(b":")
            if field == b"data":
                data.append(value.removeprefix(b" "))
            elif field == b"event":
                event_type = value.removeprefix(b" ")
        line_buffer.extend(chunk[start:])


def _matching_response(response, request_id, deadline, limit):
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("MCP 服务返回了不支持的压缩响应")
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    chunks = _bounded_chunks(response, deadline, limit)
    if media_type == "application/json":
        messages = (json.loads(b"".join(chunks).decode("utf-8")),)
    elif media_type == "text/event-stream":
        messages = _sse_messages(chunks)
    else:
        raise ValueError("MCP 服务未返回 JSON 或 SSE")
    for message in messages:
        if (isinstance(message, dict) and message.get("jsonrpc") == "2.0" and "method" not in message
                and type(message.get("id")) is int and message["id"] == request_id):
            return message
    raise ValueError("MCP 连接已结束，未收到匹配的响应")


class StreamableHTTPMCPClient:
    def __init__(self, url, *, timeout, max_result_bytes, bearer_token_env=None):
        self.url = parse_mcp_url(url)
        self.timeout = timeout
        self._tool_response_limit = max_result_bytes + TOOL_RESPONSE_OVERHEAD
        self._session_id = None
        self._initialized = False
        self._next_id = 0
        headers = {"Accept": "application/json, text/event-stream", "Accept-Encoding": "identity"}
        if bearer_token_env is not None:
            token = os.environ.get(bearer_token_env, "")
            if not re.fullmatch(r"[\x21-\x7e]+", token):
                raise ValueError(f"MCP 环境变量 {bearer_token_env} 缺失或不是有效的 Bearer Token")
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx2.Client(headers=headers, trust_env=False, follow_redirects=False)

    def _headers(self, *, initializing=False):
        if initializing:
            return {}
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session_id is not None:
            headers[SESSION_HEADER] = self._session_id
        return headers

    def _post(self, message, deadline, limit):
        method = message["method"]
        calling_tool = method == "tools/call"
        initializing = method == "initialize"
        try:
            body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as error:
            raise MCPHTTPError("MCP 请求参数无法编码为 JSON") from error
        headers = {**self._headers(initializing=initializing), "Content-Type": "application/json"}
        try:
            timeout = _remaining(deadline)
        except TimeoutError:
            raise MCPHTTPError(f"MCP 请求超时：{method}") from None
        try:
            with self._http.stream("POST", self.url, content=body, headers=headers, timeout=timeout) as response:
                _remaining(deadline)
                status = response.status_code
                if status == 404 and self._session_id is not None and not initializing:
                    self._session_id = None
                    self._initialized = False
                    raise MCPHTTPError("MCP 会话已失效，下次请求将重新连接")
                expected_status = 200 if "id" in message else 202
                if status != expected_status:
                    raise MCPHTTPError(f"MCP 服务返回 HTTP {status}",
                                       unknown=calling_tool and (response.is_success or status >= 500))
                if initializing:
                    session_id = response.headers.get(SESSION_HEADER)
                    if session_id is not None and not re.fullmatch(r"[\x21-\x7e]+", session_id):
                        raise MCPHTTPError("MCP 服务返回了无效的会话 ID")
                    self._session_id = session_id
                if "id" in message:
                    return _matching_response(response, message["id"], deadline, limit)
        except (TimeoutError, httpx2.TimeoutException):
            raise MCPHTTPError(f"MCP 请求超时：{method}", unknown=calling_tool) from None
        except httpx2.HTTPError:
            raise MCPHTTPError(f"MCP 连接中断：{method}", unknown=calling_tool) from None
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise MCPHTTPError("MCP 服务返回了无法解析的 JSON", unknown=calling_tool) from None
        except ValueError as error:
            raise MCPHTTPError(str(error), unknown=calling_tool) from None

    def _request(self, method, params, *, deadline, limit=CONTROL_RESPONSE_LIMIT):
        self._next_id += 1
        response = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params},
                              deadline, limit)
        if "error" in response:
            raise MCPHTTPError(f"MCP 服务返回 JSON-RPC 错误：{method}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPHTTPError("MCP 响应缺少结果对象", unknown=method == "tools/call")
        return result

    def initialize(self, *, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        result = initialize_tools(
            lambda method, params: self._request(method, params, deadline=deadline),
            lambda method: self._post({"jsonrpc": "2.0", "method": method}, deadline, CONTROL_RESPONSE_LIMIT),
        )
        self._initialized = True
        return result

    def list_tools(self):
        deadline = time.monotonic() + self.timeout
        tools, cursors, params = [], set(), {}
        while True:
            result = self._request("tools/list", params, deadline=deadline)
            page = result.get("tools")
            if not isinstance(page, list) or not all(isinstance(tool, dict) for tool in page):
                raise MCPHTTPError("MCP 服务返回了无效的工具清单")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or cursor in cursors:
                raise MCPHTTPError("MCP 服务返回了无效或重复的工具分页游标")
            cursors.add(cursor)
            params = {"cursor": cursor}

    def call_tool(self, name, arguments, *, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else min(self.timeout, timeout))
        if not self._initialized:
            try:
                self.initialize(deadline=deadline)
            except MCPClientError as error:
                raise MCPHTTPError(str(error)) from error
        result = self._request("tools/call", {"name": name, "arguments": arguments},
                               deadline=deadline, limit=self._tool_response_limit)
        content = result.get("content", [])
        if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
            raise MCPHTTPError("MCP 服务返回了无效的工具内容", unknown=True)
        return result

    def close(self):
        try:
            if self._session_id is not None:
                with self._http.stream("DELETE", self.url, headers=self._headers(), timeout=min(self.timeout, 1)):
                    pass
        except httpx2.HTTPError:
            pass
        finally:
            self._session_id = None
            self._initialized = False
            self._http.close()
