"""联网 B 档：Tavily 搜索与有界静态网页读取，不运行网页脚本。

仅访问公网 HTTP(S)，逐跳校验并固定连接 IP，TLS 仍验证原始主机名。
不使用环境代理、Cookie、浏览器登录态，不向目标网页发送搜索密钥。
"""

from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from ..config import load_search_api_key
from .filesystem import ToolError

SEARCH_URL = "https://api.tavily.com/search"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CONTENT_BYTES = 32 * 1024
MAX_REDIRECTS = 3
TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clip(text, size):
    encoded = text.encode("utf-8")
    return encoded[:size].decode("utf-8", errors="ignore"), len(encoded) > size


def _integer(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ToolError(f"{name} 必须是 {lower} 到 {upper} 的整数")


def _deadline(timeout_seconds):
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 30):
        raise ToolError("timeout_seconds 必须大于 0 且不超过 30 秒")
    return time.monotonic() + timeout_seconds


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError("联网请求超时")
    return remaining


def _parse_url(url):
    if (not isinstance(url, str) or not url or len(url) > 8192
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
            or "\\" in url):
        raise ToolError("URL 为空、过长或包含不支持的字符")
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError()
        if parts.username is not None or parts.password is not None or "%" in parts.hostname:
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ToolError("仅支持无用户凭据的 HTTP/HTTPS URL") from None
    authority = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        authority += f":{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="%/?@!$&'()*+,;=:-._~")
    normalized = urlunsplit((parts.scheme, authority, path, query, ""))
    return normalized, host, port, path + ("?" + query if query else "")


def _public_addresses(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ToolError("域名解析失败") from None
    if not addresses:
        raise ToolError("域名解析失败")
    for _, _, _, _, address in addresses:
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            raise ToolError("域名解析结果无效") from None
        mapped = getattr(ip, "ipv4_mapped", None)
        embedded = getattr(ip, "sixtofour", None)
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (mapped is not None and not mapped.is_global)
                or (embedded is not None and not embedded.is_global)
                or getattr(ip, "teredo", None) is not None):
            raise ToolError("禁止访问内网、回环、链路本地或其他非公网地址")
    return addresses


def _connect(addresses, deadline):
    for family, kind, protocol, _, address in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(_remaining(deadline))
            sock.connect(address)
            return sock
        except BaseException as error:
            sock.close()
            if not isinstance(error, OSError):
                raise
    raise ToolError("无法连接目标网站")


def _request(url, *, deadline, method="GET", body=None, headers=None,
             redirects=MAX_REDIRECTS, allowed_types=TEXT_TYPES):
    """重定向共享期限与字节上限；带凭据的搜索 POST 不允许重定向。"""
    for hop in range(redirects + 1):
        normalized, host, port, target = _parse_url(url)
        _remaining(deadline)
        addresses = _public_addresses(host, port)
        timeout = _remaining(deadline)
        if normalized.startswith("https:"):
            connection = http.client.HTTPSConnection(host, port, timeout=timeout,
                                                     context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        # HTTPConnection 的连接钩子；HTTPSConnection 保留基于 host 的 SNI 与证书校验。
        connection._create_connection = lambda *args, **kwargs: _connect(addresses, deadline)
        response = None
        try:
            connection.request(method, target, body=body, headers={
                "User-Agent": "RecapCLI/0.6 (+static-text-reader)",
                "Accept": ", ".join(sorted(allowed_types)), "Accept-Encoding": "identity",
                **(headers or {}),
            })
            if connection.sock is not None:
                connection.sock.settimeout(_remaining(deadline))
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if hop >= redirects or not location:
                    raise ToolError("重定向次数超限或目标缺失；搜索 API 不允许重定向")
                next_url = urljoin(normalized, location)
                if normalized.startswith("https:") and not next_url.startswith("https:"):
                    raise ToolError("禁止 HTTPS 重定向降级")
                url = next_url
                continue
            if not 200 <= response.status < 300:
                # 不回显错误正文、请求头或底层异常，避免上游回显凭据。
                reason = {401: "认证失败", 403: "访问被拒绝", 429: "请求限流"}.get(response.status, "请求失败")
                raise ToolError(f"HTTP {response.status}：{reason}")
            content_type = Message()
            content_type["content-type"] = response.getheader("Content-Type", "application/octet-stream")
            media_type = content_type.get_content_type().lower()
            if media_type not in allowed_types:
                raise ToolError("不支持此响应类型；仅读取静态 HTML、纯文本、Markdown 或 JSON")
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ToolError("不支持压缩响应，已请求 identity 编码")
            data = bytearray()
            while len(data) <= MAX_RESPONSE_BYTES:
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                _remaining(deadline)
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            _remaining(deadline)
            return normalized, content_type, bytes(data[:MAX_RESPONSE_BYTES]), len(data) > MAX_RESPONSE_BYTES
        except (OSError, http.client.HTTPException):
            raise ToolError("联网请求失败或超时；请检查网络、TLS 证书或稍后重试") from None
        finally:
            if response is not None:
                response.close()
            connection.close()


class _PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "pre", "hr", "blockquote", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skipping = []
        self.in_title = False
        self.title = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping.append(tag)
        if self.skipping:
            return
        if tag == "title":
            self.in_title = True
        if tag in self.BLOCK:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.skipping:
            if tag == self.skipping[-1]:
                self.skipping.pop()
            return
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCK:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            (self.title if self.in_title else self.text).append(data)


def _decode(data, content_type):
    charset = content_type.get_content_charset()
    if not charset and content_type.get_content_type() in {"text/html", "application/xhtml+xml"}:
        match = re.search(br'charset\s*=\s*["\x27]?([\w-]+)', data[:4096], re.I)
        charset = match.group(1).decode("ascii") if match else None
    try:
        return data.decode(charset or "utf-8-sig", errors="replace")
    except LookupError:
        raise ToolError("网页声明了不支持的字符编码") from None


def read_url(workspace, url, max_content_bytes=16384, timeout_seconds=20):
    """无需搜索密钥；网页内容始终作为不可信外部资料返回。"""
    _integer(max_content_bytes, "max_content_bytes", 512, MAX_CONTENT_BYTES)
    final_url, content_type, data, raw_truncated = _request(url, deadline=_deadline(timeout_seconds))
    text = _decode(data, content_type)
    title = ""
    if content_type.get_content_type() in {"text/html", "application/xhtml+xml"}:
        page = _PageText()
        page.feed(text)
        page.close()
        title = " ".join("".join(page.title).split())
        text = "\n".join(line for raw in "".join(page.text).splitlines() if (line := " ".join(raw.split())))
    content, text_truncated = _clip(text, max_content_bytes)
    title, title_truncated = _clip(title, 512)
    reasons = []
    if raw_truncated:
        reasons.append("response_bytes")
    if text_truncated or title_truncated:
        reasons.append("content_bytes")
    return {"url": url, "final_url": final_url, "reference": final_url, "title": title,
            "content_type": content_type.get_content_type(), "content": content,
            "fetched_at": _now(), "untrusted_content": True,
            "truncated": bool(reasons), "limit_reason": reasons,
            "empty_content": not bool(content.strip())}


def web_search(workspace, query, max_results=5, timeout_seconds=20):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ToolError("query 必须是非空文本且不超过 2000 字符")
    _integer(max_results, "max_results", 1, 10)
    deadline = _deadline(timeout_seconds)
    key = load_search_api_key()
    payload = json.dumps({"query": query, "max_results": max_results, "search_depth": "basic",
                          "include_answer": False, "include_raw_content": False,
                          "include_images": False, "auto_parameters": False}).encode("utf-8")
    _, _, data, truncated = _request(SEARCH_URL, deadline=deadline, method="POST", body=payload,
                                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                   redirects=0, allowed_types={"application/json"})
    if truncated:
        raise ToolError("搜索 API 响应超过大小限制")
    try:
        response = json.loads(data)
    except (ValueError, UnicodeError):
        raise ToolError("搜索 API 返回无效 JSON") from None
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise ToolError("搜索 API 响应缺少 results 列表")
    results = []
    skipped = 0
    truncated = len(response["results"]) > max_results
    for item in response["results"][:max_results]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("url", "title", "content")):
            skipped += 1
            continue
        try:
            url, _, _, _ = _parse_url(item["url"])
        except ToolError:
            skipped += 1
            continue
        title, title_cut = _clip(item["title"].replace(key, "[REDACTED]"), 512)
        snippet, snippet_cut = _clip(item["content"].replace(key, "[REDACTED]"), 2000)
        url = url.replace(key, "[REDACTED]")
        truncated = truncated or title_cut or snippet_cut
        results.append({"title": title, "url": url, "reference": url, "snippet": snippet})
    return {"provider": "tavily", "query": query, "results": results, "skipped_results": skipped,
            "fetched_at": _now(), "untrusted_content": True, "truncated": truncated,
            "limit_reason": ["search_results_or_snippets"] if truncated else []}
