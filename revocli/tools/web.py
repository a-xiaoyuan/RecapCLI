"""只读网页搜索与获取；返回的内容始终是外部资料。"""

import json
from http.client import HTTPException
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from ..config import load_values
from .filesystem import ToolError

MAX_BYTES = 64 * 1024
MAX_RESULTS = 20


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title_parts = []
        self.hidden = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def _request(request, limit):
    try:
        with urlopen(request, timeout=15) as response:
            data = response.read(limit + 1)
            return data[:limit], len(data) > limit, response.geturl(), response.headers
    except HTTPError as error:
        raise ToolError(f"联网请求失败：HTTP {error.code}") from error
    except (URLError, OSError, HTTPException) as error:
        raise ToolError("联网请求失败，请检查网络或服务配置") from error


def web_search(query, max_results=5, *, workspace):
    if not query.strip():
        raise ToolError("query 不能为空")
    if not 1 <= max_results <= MAX_RESULTS:
        raise ToolError(f"max_results 必须在 1 到 {MAX_RESULTS} 之间")
    values = load_values()
    brave = (values.get("BRAVE_API_KEY") or "").strip()
    tavily = (values.get("TAVILY_API_KEY") or "").strip()
    provider = (values.get("REVO_WEB_PROVIDER") or "").strip().lower() or ("brave" if brave else "tavily" if tavily else "brave")
    if provider not in {"brave", "tavily"}:
        raise ToolError("REVO_WEB_PROVIDER 必须是 brave 或 tavily")
    key = brave if provider == "brave" else tavily
    if not key:
        raise ToolError(f"缺少 {provider.upper()}_API_KEY；请在 RevoCLI 的 .env 或环境变量中设置")
    if provider == "brave":
        url = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": max_results})
        request = Request(url, headers={"Accept": "application/json", "X-Subscription-Token": key})
    else:
        request = Request("https://api.tavily.com/search", data=json.dumps({
            "query": query, "max_results": max_results,
        }).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    raw, truncated, _, _ = _request(request, 1024 * 1024)
    if truncated:
        raise ToolError("搜索服务响应过大")
    try:
        payload = json.loads(raw)
        items = payload.get("web", {}).get("results", []) if provider == "brave" else payload.get("results", [])
        if not isinstance(items, list):
            raise ValueError("invalid results")
        results = [{"title": item.get("title") or "", "url": item.get("url") or "",
                    "snippet": item.get("description" if provider == "brave" else "content") or "",
                    "provider": provider} for item in items[:max_results] if isinstance(item, dict)]
    except (ValueError, TypeError, AttributeError) as error:
        raise ToolError("搜索服务返回了无效响应") from error
    return {"provider": provider, "results": results}


def fetch_url(url, max_bytes=MAX_BYTES, *, workspace):
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        # Accessing port validates malformed ports before opening the URL.
        parsed.port
    except ValueError as error:
        raise ToolError("url 必须是有效的 HTTP(S) 地址，且不能包含凭据") from error
    if not 1 <= max_bytes <= MAX_BYTES:
        raise ToolError(f"max_bytes 必须在 1 到 {MAX_BYTES} 之间")
    request = Request(url, headers={"User-Agent": "RevoCLI/1.0", "Accept": "text/html, text/plain"})
    raw, truncated, final_url, headers = _request(request, max_bytes)
    content_type = headers.get_content_type()
    if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
        raise ToolError(f"不支持的网页内容类型：{content_type}")
    charset = headers.get_content_charset() or "utf-8"
    try:
        decoded = raw.decode(charset, errors="replace")
    except LookupError:
        decoded = raw.decode("utf-8", errors="replace")
    title = ""
    if content_type != "text/plain":
        parser = _TextExtractor()
        parser.feed(decoded)
        title = " ".join("".join(parser.title_parts).split())
        decoded = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines())
        decoded = "\n".join(line for line in decoded.splitlines() if line)
    body = decoded.encode("utf-8")
    if len(body) > max_bytes:
        truncated = True
        decoded = body[:max_bytes].decode("utf-8", errors="ignore")
    return {"url": final_url, "source": final_url, "title": title, "content": decoded, "truncated": truncated}
