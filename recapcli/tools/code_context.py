"""按问题挑选仓库结构与语法引用；保留真实行号和索引完整性信息。"""

from collections import defaultdict
from contextlib import contextmanager
import json
from pathlib import Path
import re
import time

from .filesystem import ScanBudget, ScanLimit, ToolError, _relative, _resolve, file_reference
from .retrieval import _connect, _refresh, _read_source, _delete, REFRESH_SECONDS


@contextmanager
def catalog(workspace, path=".", refresh=False):
    workspace = Path(workspace).resolve(strict=True)
    scope = _resolve(workspace, path)
    root = workspace if scope.is_relative_to(workspace) else scope
    connection = _connect(workspace)
    try:
        connection.create_function("in_scope", 1, lambda value: (workspace / value).is_relative_to(scope))
        with connection:
            state = connection.execute("SELECT refreshed_at,details FROM scan_state WHERE id=1").fetchone()
            details = json.loads(state[1]) if state else {}
            cached = (not refresh and state and time.time() - state[0] < REFRESH_SECONDS
                      and details.get("root") == str(root))
            if not cached:
                budget, skipped = _refresh(connection, workspace, root=root)
                details = {"root": str(root), "reason": budget.reason, "skipped": skipped}
                connection.execute("INSERT OR REPLACE INTO scan_state VALUES (1,?,?)", (time.time(), json.dumps(details)))
            yield connection, {"cached": bool(cached), "truncated": bool(details.get("reason")),
                               "limit_reason": details.get("reason", ""), "scope": _relative(workspace, scope)}
    finally:
        connection.close()


def _verified(connection, workspace, rows, metadata):
    checked, budget = {}, ScanBudget()
    for row in rows:
        path, digest = row[0], row[-1]
        if path not in checked:
            try:
                current, text = _read_source(Path(workspace).resolve(), Path(workspace).resolve() / path, budget)
                checked[path] = text if current == digest else None
            except ScanLimit as error:
                metadata.update(truncated=True, limit_reason=str(error))
                break
            except (OSError, ValueError, UnicodeError):
                checked[path] = None
            if checked[path] is None:
                _delete(connection, path)
                connection.execute("DELETE FROM scan_state")
        if checked[path] is not None:
            yield row, checked[path]


def find_references(workspace, name, path=".", kind="call", max_results=50, refresh=False):
    if not isinstance(name, str) or not name.strip() or len(name) > 500:
        raise ToolError("name 必须是 1–500 字符的名称")
    if kind not in {"call", "usage", "import", "any"} or not 1 <= max_results <= 200:
        raise ToolError("kind 为 call/usage/import/any，max_results 为 1–200")
    with catalog(workspace, path, refresh) as (connection, metadata):
        rows = connection.execute("""SELECT refs.path,refs.name,refs.kind,refs.line,refs.caller,files.digest
            FROM refs JOIN files ON files.path=refs.path WHERE refs.name=? AND (?='any' OR refs.kind=?)
            AND in_scope(refs.path) ORDER BY refs.path,refs.line,refs.kind LIMIT ?""",
            (name, kind, kind, max_results + 1)).fetchall()
        matches = []
        for row, text in _verified(connection, workspace, rows[:max_results], metadata):
            file, _, role, line, caller, _ = row
            matches.append({"path": file, "line": line, "kind": role, "caller": caller,
                            "reference": file_reference(file, line, line),
                            "snippet": text.splitlines()[line - 1][:500]})
        return {**metadata, "matches": matches, "truncated": metadata["truncated"] or len(rows) > max_results,
                "resolution": "syntax", "note": "仅按语法名称匹配；同名、别名、重载及动态调用需结合源码或语言工具核实"}


def verification_commands(workspace):
    root = Path(workspace)
    commands = []
    for marker, argv in (("pom.xml", ["mvn", "test"]), ("build.gradle", ["gradle", "test"]),
                         ("build.gradle.kts", ["gradle", "test"]), ("go.mod", ["go", "test", "./..."]),
                         ("Cargo.toml", ["cargo", "test"])):
        if (root / marker).is_file():
            commands.append({"source": marker, "argv": argv})
    manifest = root / "package.json"
    if manifest.is_file() and not manifest.is_symlink() and not manifest.is_junction() and manifest.stat().st_size <= 65536:
        try:
            scripts = json.loads(manifest.read_text(encoding="utf-8-sig")).get("scripts", {})
            for key in ("typecheck", "test", "build"):
                if key in scripts:
                    commands.append({"source": "package.json scripts." + key, "argv": ["npm", "run", key]})
        except (OSError, ValueError, AttributeError):
            pass
    if (root / "pyproject.toml").is_file() and (root / "tests").is_dir():
        commands.append({"source": "pyproject.toml + tests/", "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
                         "note": "候选命令；需核对项目实际测试框架与开发约定"})
    return commands


def repository_map(workspace, query="", path=".", max_files=20, max_bytes=8192, refresh=False):
    if not 1 <= max_files <= 100 or not 512 <= max_bytes <= 65536:
        raise ToolError("max_files 为 1–100，max_bytes 为 512–65536")
    tokens = list(dict.fromkeys(re.findall(r"[\w]+", query.casefold())))[:32]
    with catalog(workspace, path, refresh) as (connection, metadata):
        rows = connection.execute("""SELECT symbols.path,name,kind,start_line,end_line,files.digest
            FROM symbols JOIN files ON files.path=symbols.path WHERE in_scope(symbols.path)
            ORDER BY symbols.path,start_line LIMIT 20001""").fetchall()
        symbols, definitions, digests = defaultdict(list), defaultdict(set), {}
        for file, name, kind, start, end, digest in rows[:20000]:
            symbols[file].append({"name": name, "kind": kind, "start_line": start, "end_line": end})
            definitions[name].add(file)
            digests[file] = digest
        refs = connection.execute("SELECT path,name,kind FROM refs WHERE in_scope(path) AND kind IN ('call','import') LIMIT 40001").fetchall()
        edges = defaultdict(set)
        for file, name, kind in refs[:40000]:
            for target in definitions.get(name, ()):
                if target != file:
                    edges[file].add(target)
        incoming = defaultdict(int)
        for targets in edges.values():
            for target in targets:
                incoming[target] += 1
        scores = {}
        for file, items in symbols.items():
            value = (file + " " + " ".join(item["name"] for item in items)).casefold()
            scores[file] = 1 + incoming[file] + sum(8 for token in tokens if token in value)
        if tokens:
            fts = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            for (file,) in connection.execute("SELECT DISTINCT path FROM lines WHERE lines MATCH ? AND in_scope(path) LIMIT 100", (fts,)):
                if file in scores:
                    scores[file] += 6
        # 将相关文件的一部分权重传给其语法名称关联文件。
        for file, targets in edges.items():
            for target in targets:
                scores[target] = scores.get(target, 1) + scores.get(file, 1) / (4 * len(targets))
        ordered = sorted(scores, key=lambda file: (-scores[file], file))[:max_files]
        candidates = [(file, digests[file]) for file in ordered]
        selected, parts, used = [], [], 0
        for (file, _), _text in _verified(connection, workspace, candidates, metadata):
            source_lines = _text.splitlines()
            items = [{**item, "declaration": source_lines[item["start_line"] - 1].strip()[:180]}
                     for item in symbols[file][:12]]
            block = file + "\n" + "\n".join(f"  {item['kind']} {item['name']} · {item['start_line']}-{item['end_line']} — {item['declaration']}" for item in items)
            linked = sorted(edges.get(file, ()))[:5]
            if linked:
                block += "\n  名称关联候选：" + ", ".join(linked)
            cost = len((block + "\n").encode("utf-8"))
            if used + cost > max_bytes:
                break
            parts.append(block)
            used += cost
            selected.append({"path": file, "symbols": items, "related_candidates": linked})
        return {**metadata, "files": selected, "map": "\n".join(parts), "bytes": used,
                "verification_candidates": verification_commands(Path(workspace).resolve() / path),
                "truncated": metadata["truncated"] or len(selected) < len(symbols) or len(rows) > 20000 or len(refs) > 40000,
                "note": "这是按问题选取的部分仓库地图；名称关联不是编译器解析的调用关系，未列出不代表不存在"}
