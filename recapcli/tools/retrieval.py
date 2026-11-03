"""工作区内的持久代码索引；查询时先按文件内容刷新，再复核引用。"""

import hashlib
import json
from pathlib import Path
import sqlite3
import time

from ..storage.files import state_directory
from ..core.access import access_root
from .symbols import extract_symbols, extract_references, LANGUAGES, KINDS
from .filesystem import (MAX_FILE_BYTES, MAX_SEARCH_FILE_BYTES, ScanBudget, ScanLimit,
                               ToolError, _checked_target, _relative, _resolve, _walk_files, file_reference)

SCHEMA_VERSION = 3
MAX_RESULTS = 200
REFRESH_SECONDS = 10


def _read_source(workspace, path, budget=None):
    # 遍历时和再次校验时都拒绝链接，且重新检查解析后的路径。
    root = access_root(workspace, path)
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink() or cursor.is_junction():
            raise ToolError("索引文件路径不能包含链接")
    if _checked_target(workspace, path) != path or not path.is_file():
        raise ToolError("文件不可索引")
    if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
        raise ToolError("文件超过索引大小上限")
    with path.open("rb") as stream:
        data = stream.read(MAX_SEARCH_FILE_BYTES + 1)
    if budget is not None:
        budget.consume_bytes(len(data))
    if len(data) > MAX_SEARCH_FILE_BYTES or b"\x00" in data:
        raise ToolError("文件过大或不是文本")
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(data).hexdigest(), text


def _connect(workspace):
    directory = state_directory(workspace, "retrieval")
    path = directory / "index.sqlite3"
    for candidate in (path, *(path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        if candidate.is_symlink() or candidate.is_junction() or not candidate.resolve().is_relative_to(workspace):
            raise ToolError("索引文件不能是链接或位于工作区之外")
    connection = sqlite3.connect(path, timeout=5)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise ToolError("不支持的代码索引版本")
        connection.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, digest TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS symbols (path TEXT, name TEXT, kind TEXT, start_line INTEGER, end_line INTEGER)")
        connection.execute('CREATE TABLE IF NOT EXISTS refs (path TEXT, name TEXT, kind TEXT, line INTEGER, caller TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS refs_name ON refs(name,kind)')
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lines USING fts5(path UNINDEXED, line UNINDEXED, text)")
        connection.execute("CREATE TABLE IF NOT EXISTS scan_state (id INTEGER PRIMARY KEY, refreshed_at REAL, details TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS chunks (path TEXT, start_line INTEGER, end_line INTEGER, text TEXT, digest TEXT, PRIMARY KEY(path,start_line))")
        connection.execute("CREATE TABLE IF NOT EXISTS vectors (path TEXT, start_line INTEGER, model TEXT, digest TEXT, vector TEXT, PRIMARY KEY(path,start_line,model))")
        if version in (1, 2):
            for table in ("files", "symbols", "refs", "lines", "scan_state", "chunks", "vectors"):
                connection.execute(f"DELETE FROM {table}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return connection
    except BaseException:
        connection.close()
        raise


def _delete(connection, path):
    connection.execute("DELETE FROM refs WHERE path = ?", (path,))
    connection.execute("DELETE FROM files WHERE path = ?", (path,))
    connection.execute("DELETE FROM symbols WHERE path = ?", (path,))
    connection.execute("DELETE FROM lines WHERE path = ?", (path,))
    connection.execute("DELETE FROM chunks WHERE path = ?", (path,))
    connection.execute("DELETE FROM vectors WHERE path = ?", (path,))


def _index(connection, path, digest, text, budget):
    _delete(connection, path)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    connection.execute("INSERT INTO files VALUES (?, ?)", (path, digest))
    for start in range(0, len(lines), 40):
        snippet = "\n".join(lines[start:start + 40])
        if snippet.strip() and len(snippet.encode("utf-8")) <= 8192:
            connection.execute("INSERT INTO chunks VALUES (?,?,?,?,?)",
                               (path, start + 1, min(start + 40, len(lines)), snippet, digest))
    for number, line in enumerate(lines, 1):
        if number % 128 == 0:
            budget.check_time()
        # read_file 必须能读取所返回的完整行。
        if len(line.encode("utf-8")) <= MAX_FILE_BYTES:
            connection.execute("INSERT INTO lines(path, line, text) VALUES (?, ?, ?)", (path, number, line))
    for name, kind, start, end in extract_symbols(path, text, budget.check_time):
        connection.execute("INSERT INTO symbols VALUES (?, ?, ?, ?, ?)", (path, name, kind, start, end))
    connection.executemany("INSERT INTO refs VALUES (?, ?, ?, ?, ?)",
                           ((path, name, kind, line, caller) for name, kind, line, caller in extract_references(path, text, budget.check_time)))


def _refresh(connection, workspace, root=None):
    root = root or workspace
    budget = ScanBudget()
    seen = set()
    skipped = 0
    try:
        files = [root] if root.is_file() else _walk_files(workspace, root, budget)
        for path in files:
            budget.visit_file()
            relative = _relative(workspace, path)
            try:
                digest, text = _read_source(workspace, path, budget)
            except ScanLimit:
                raise
            except (OSError, ValueError, UnicodeError):
                skipped += 1
                _delete(connection, relative)
                continue
            old = connection.execute("SELECT digest FROM files WHERE path = ?", (relative,)).fetchone()
            has_chunks = connection.execute("SELECT 1 FROM chunks WHERE path=? LIMIT 1", (relative,)).fetchone()
            if old is None or old[0] != digest or (text.strip() and not has_chunks):
                # 建索引中途耗尽预算时回滚该文件，保留此前完整的缓存。
                connection.execute("SAVEPOINT index_file")
                try:
                    _index(connection, relative, digest, text, budget)
                except BaseException:
                    connection.execute("ROLLBACK TO index_file")
                    raise
                finally:
                    connection.execute("RELEASE index_file")
            seen.add(relative)
    except ScanLimit:
        pass
    # 只有完整扫描才能以“未出现”判定文件已删除；部分扫描保留未检查的缓存。
    if not budget.reason:
        for (path,) in connection.execute("SELECT path FROM files").fetchall():
            if (workspace / path).is_relative_to(root) and path not in seen:
                _delete(connection, path)
    return budget, skipped


def _validate_search_params(*, name, kind, query, max_results, refresh, mode, timeout_seconds, scope):
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ToolError("max_results 必须是大于 0 的整数")
    max_results = min(max_results, MAX_RESULTS)
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
        raise ToolError("query 必须是 1 到 500 字符的非空字符串")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ToolError("name 必须是非空字符串")
    if kind is not None and kind not in KINDS:
        raise ToolError("不支持的符号类型：" + str(kind))
    if query is None and name is None and kind is None and scope == ".":
        raise ToolError("至少提供 name 或 kind")
    if not isinstance(refresh, bool):
        raise ToolError("refresh 必须是布尔值")
    if mode not in {"keyword", "hybrid"}:
        raise ToolError("mode 必须是 keyword 或 hybrid")
    if not 0 < timeout_seconds <= 30:
        raise ToolError("timeout_seconds 必须大于 0 且不超过 30")
    return max_results


def _search(workspace, *, name=None, kind=None, query=None, path=".", max_results=50, refresh=False,
            mode="keyword", timeout_seconds=20):
    workspace = Path(workspace).resolve(strict=True)
    scope_target = _resolve(workspace, path)
    scope = _relative(workspace, scope_target)
    scan_root = workspace if scope_target.is_relative_to(workspace) else scope_target
    max_results = _validate_search_params(
        name=name, kind=kind, query=query, max_results=max_results, refresh=refresh,
        mode=mode, timeout_seconds=timeout_seconds, scope=scope)
    try:
        connection = _connect(workspace)
        connection.create_function("in_scope", 1, lambda path: (workspace / path).is_relative_to(scope_target))
        try:
            with connection:
                ranking, retrieval = {}, {}
                def scan():
                    budget, skipped = (_refresh(connection, workspace) if scan_root == workspace
                                       else _refresh(connection, workspace, root=scan_root))
                    details = {"reason": budget.reason, "skipped": skipped, "root": str(scan_root)}
                    connection.execute("INSERT OR REPLACE INTO scan_state VALUES (1, ?, ?)",
                                       (time.time(), json.dumps(details)))
                    return budget, details

                def lookup():
                    nonlocal ranking, retrieval
                    if query is not None:
                        if mode == "hybrid":
                            from .hybrid import rank_rows
                            started = time.monotonic()
                            rows, ranking, retrieval = rank_rows(connection, query, max_results + 1, timeout_seconds)
                            # 扫描与网络各有时限，Embedding 等待不消耗本地扫描计时。
                            budget.started += time.monotonic() - started
                            return rows
                        phrase = '"' + query.replace('"', '""') + '"'
                        return connection.execute("""SELECT lines.path, lines.line, lines.line, lines.text, files.digest
                            FROM lines JOIN files ON files.path = lines.path WHERE lines MATCH ? AND in_scope(lines.path)
                            ORDER BY bm25(lines), lines.path, CAST(lines.line AS INTEGER) LIMIT ?""",
                            (phrase, max_results + 1)).fetchall()
                    return connection.execute("""SELECT symbols.path, start_line, end_line, name, kind, files.digest
                            FROM symbols JOIN files ON files.path = symbols.path
                            WHERE (? IS NULL OR name = ?) AND (? IS NULL OR kind = ?)
                            AND in_scope(symbols.path)
                            ORDER BY symbols.path, start_line LIMIT ?""",
                            (name, name, kind, kind, max_results + 1)).fetchall()

                state = connection.execute("SELECT refreshed_at, details FROM scan_state WHERE id=1").fetchone()
                age = max(0, time.time() - state[0]) if state else REFRESH_SECONDS
                cached = (not refresh and state is not None and age < REFRESH_SECONDS
                          and json.loads(state[1]).get("root", str(workspace)) == str(scan_root))
                if cached:
                    budget, details = ScanBudget(), json.loads(state[1])
                else:
                    budget, details = scan()
                rows = lookup()
                if cached and not rows:
                    # 空查询先刷新一次，避免新文件/新关键词被旧索引掩盖。
                    budget, details = scan()
                    rows, cached = lookup(), False
                matches, changed = [], False
                verified = {}
                source_text = {}
                for row in rows[:max_results]:
                    path, start, end = row[:3]
                    try:
                        if path not in verified:
                            budget.check_time()
                            verified[path], source_text[path] = _read_source(workspace, workspace / path, budget)
                        current = verified[path]
                        if current != row[-1]:
                            raise ToolError("文件已变化")
                    except ScanLimit:
                        break
                    except (OSError, ValueError, UnicodeError):
                        changed = True
                        _delete(connection, path)
                        continue
                    match = {"path": path, "start_line": int(start), "end_line": int(end),
                             "reference": file_reference(path, int(start), int(end))}
                    if query is not None:
                        match["snippet"] = ("\n".join(source_text[path].splitlines()[int(start) - 1:int(end)])
                                            if mode == "hybrid" else row[3])
                        match.update(ranking.get((path, int(start), int(end)), {}))
                    else:
                        match.update(name=row[3], kind=row[4])
                        match["language"] = LANGUAGES.get(Path(path).suffix.lower(), "text")
                    matches.append(match)
                reason = budget.reason or details["reason"]
                result = {"matches": matches, "truncated": len(rows) > max_results or bool(reason) or changed,
                          "skipped_files": details["skipped"], "scanned_files": budget.files,
                          "cached": cached, "index_age_seconds": round(age, 2) if cached else 0}
                if query is not None:
                    result.update(retrieval, retrieval_mode=mode)
                    if retrieval.get("pending_embedding_chunks") or retrieval.get("candidate_truncated"):
                        result["truncated"] = True
                if reason:
                    result["limit_reason"] = reason
                if cached:
                    result["cache_note"] = "候选已核验；索引复用最近 10 秒扫描，若需完整覆盖当前工作区请传 refresh=true。"
                if changed:
                    result["stale_files"] = True
                return result
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ToolError(f"代码索引不可用：{error}") from error


def search_symbols(workspace, name=None, kind=None, max_results=50, refresh=False, path="."):
    return _search(workspace, name=name, kind=kind, path=path, max_results=max_results, refresh=refresh)


def search_code_index(workspace, query, max_results=50, refresh=False, mode="hybrid", timeout_seconds=20, path="."):
    return _search(workspace, query=query, max_results=max_results, refresh=refresh, mode=mode,
                   timeout_seconds=timeout_seconds, path=path)
