"""工作区内的持久代码索引；查询时先按文件内容刷新，再复核引用。"""

import ast
import hashlib
from pathlib import Path
import sqlite3

from ..storage.files import state_directory
from .filesystem import (MAX_FILE_BYTES, MAX_SEARCH_FILE_BYTES, ScanBudget, ScanLimit,
                               ToolError, _checked_target, _relative, _walk_files, file_reference)

SCHEMA_VERSION = 1
MAX_RESULTS = 200


def _read_source(workspace, path, budget=None):
    # 遍历时和再次校验时都拒绝链接，且重新检查解析后的路径。
    relative = path.relative_to(workspace)
    cursor = workspace
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
        if version not in (0, SCHEMA_VERSION):
            raise ToolError("不支持的代码索引版本")
        connection.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, digest TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS symbols (path TEXT, name TEXT, kind TEXT, start_line INTEGER, end_line INTEGER)")
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lines USING fts5(path UNINDEXED, line UNINDEXED, text)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return connection
    except BaseException:
        connection.close()
        raise


def _delete(connection, path):
    connection.execute("DELETE FROM files WHERE path = ?", (path,))
    connection.execute("DELETE FROM symbols WHERE path = ?", (path,))
    connection.execute("DELETE FROM lines WHERE path = ?", (path,))


def _index(connection, path, digest, text, budget):
    _delete(connection, path)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    connection.execute("INSERT INTO files VALUES (?, ?)", (path, digest))
    for number, line in enumerate(lines, 1):
        if number % 128 == 0:
            budget.check_time()
        # read_file 必须能读取所返回的完整行。
        if len(line.encode("utf-8")) <= MAX_FILE_BYTES:
            connection.execute("INSERT INTO lines(path, line, text) VALUES (?, ?, ?)", (path, number, line))
    if path.lower().endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return
        for node in ast.walk(tree):
            budget.check_time()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = ("class" if isinstance(node, ast.ClassDef) else
                        "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
                start, end = node.lineno, node.end_lineno
                if end is not None and len("\n".join(lines[start - 1:end]).encode("utf-8")) <= MAX_FILE_BYTES:
                    connection.execute("INSERT INTO symbols VALUES (?, ?, ?, ?, ?)",
                                       (path, node.name, kind, start, end))


def _refresh(connection, workspace):
    budget = ScanBudget()
    seen = set()
    skipped = 0
    try:
        for path in _walk_files(workspace, workspace, budget):
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
            if old is None or old[0] != digest:
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
            if path not in seen:
                _delete(connection, path)
    return budget, skipped


def _search(workspace, *, name=None, kind=None, query=None, max_results=50):
    workspace = Path(workspace).resolve(strict=True)
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ToolError("max_results 必须是大于 0 的整数")
    max_results = min(max_results, MAX_RESULTS)
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
        raise ToolError("query 必须是 1 到 500 字符的非空字符串")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ToolError("name 必须是非空字符串")
    if kind is not None and kind not in ("class", "function", "async_function"):
        raise ToolError("kind 必须是 class、function 或 async_function")
    if query is None and name is None and kind is None:
        raise ToolError("至少提供 name 或 kind")
    try:
        connection = _connect(workspace)
        try:
            with connection:
                budget, skipped = _refresh(connection, workspace)
                if query is not None:
                    # 将用户输入作为 FTS 短语，避免将其解释成 FTS 操作符。
                    phrase = '"' + query.replace('"', '""') + '"'
                    rows = connection.execute("""SELECT lines.path, lines.line, lines.line, lines.text, files.digest
                        FROM lines JOIN files ON files.path = lines.path WHERE lines MATCH ?
                        ORDER BY lines.path, CAST(lines.line AS INTEGER) LIMIT ?""", (phrase, max_results + 1)).fetchall()
                else:
                    rows = connection.execute("""SELECT symbols.path, start_line, end_line, name, kind, files.digest
                        FROM symbols JOIN files ON files.path = symbols.path
                        WHERE (? IS NULL OR name = ?) AND (? IS NULL OR kind = ?)
                        ORDER BY symbols.path, start_line LIMIT ?""", (name, name, kind, kind, max_results + 1)).fetchall()
                matches, changed = [], False
                verified = {}
                for row in rows[:max_results]:
                    path, start, end = row[:3]
                    try:
                        if path not in verified:
                            budget.check_time()
                            verified[path], _ = _read_source(workspace, workspace / path, budget)
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
                        match["snippet"] = row[3]
                    else:
                        match.update(name=row[3], kind=row[4])
                    matches.append(match)
                result = {"matches": matches, "truncated": len(rows) > max_results or bool(budget.reason) or changed,
                          "skipped_files": skipped, "scanned_files": budget.files}
                if budget.reason:
                    result["limit_reason"] = budget.reason
                if changed:
                    result["stale_files"] = True
                return result
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ToolError(f"代码索引不可用：{error}") from error


def search_symbols(workspace, name=None, kind=None, max_results=50):
    return _search(workspace, name=name, kind=kind, max_results=max_results)


def search_code_index(workspace, query, max_results=50):
    return _search(workspace, query=query, max_results=max_results)
