"""工作区文件读取、文本搜索、目录浏览及访问检查。"""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
import time

from ..config import ENV_FILE

MAX_FILE_BYTES = 64 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 50
MAX_TREE_ENTRIES = 300
MAX_LINE_LENGTH = 300
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".archer", ".idea", ".vscode",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


class ToolError(ValueError):
    """工具参数或执行环境不满足要求。"""


class ScanLimit(ToolError):
    """本地扫描达到预算；搜索和目录浏览可以返回已有的部分结果。"""


@dataclass
class ScanBudget:
    max_entries: int = 5000
    max_files: int = 1000
    max_bytes: int = 16 * 1024 * 1024
    max_seconds: float = 5.0
    entries: int = field(default=0, init=False)
    files: int = field(default=0, init=False)
    bytes_read: int = field(default=0, init=False)
    reason: str = field(default="", init=False)
    started: float = field(default_factory=time.monotonic, init=False)

    def stop(self, reason):
        self.reason = reason
        raise ScanLimit(reason)

    def check_time(self):
        if time.monotonic() - self.started >= self.max_seconds:
            self.stop("达到本地扫描时间上限")

    def visit_entry(self):
        self.check_time()
        if self.entries >= self.max_entries:
            self.stop("达到目录扫描条目上限")
        self.entries += 1

    def visit_file(self):
        self.check_time()
        if self.files >= self.max_files:
            self.stop("达到扫描文件数量上限")
        self.files += 1

    def consume_bytes(self, size):
        self.check_time()
        if self.bytes_read + size > self.max_bytes:
            self.stop("达到文件扫描字节上限")
        self.bytes_read += size


def _checked_target(workspace, target):
    """入口路径和遍历得到的子路径使用同一份访问检查。"""
    target = target.resolve()
    if not target.is_relative_to(Path(workspace).resolve()):
        raise ToolError("禁止访问工作区之外的路径")
    if target == ENV_FILE.resolve():
        raise ToolError("禁止访问 Archer 的本地凭据配置")
    return target


def _resolve(workspace, path, *, must_exist=True):
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path 必须是非空字符串")
    candidate = Path(path)
    if candidate.is_absolute():
        raise ToolError("path 必须是相对工作区的路径")
    target = _checked_target(workspace, Path(workspace) / candidate)
    if must_exist and not target.exists():
        raise ToolError(f"路径不存在：{path}")
    return target


def _relative(workspace, target):
    return str(target.relative_to(Path(workspace).resolve())).replace("\\", "/")


def file_reference(path, start, end):
    if not start:
        return path
    return f"{path}:{start}" if start == end else f"{path}:{start}-{end}"


def _directory_entries(workspace, directory, budget):
    """只列出允许访问的子项；遍历不跟随链接，避免越界和循环。"""
    entries = []
    try:
        for entry in directory.iterdir():
            budget.visit_entry()
            if entry.name in SKIP_DIRS:
                continue
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                target = _checked_target(workspace, entry)
                if target.is_dir() or target.is_file():
                    entries.append(target)
            except (OSError, ValueError):
                continue
    except ScanLimit:
        pass  # 仍然返回预算内已经检查过的子项。
    except OSError:
        pass
    return sorted(entries, key=lambda item: (item.is_file(), item.name.lower()))


def _walk_files(workspace, root, budget):
    directories = [root]
    while directories:
        children = []
        for entry in _directory_entries(workspace, directories.pop(), budget):
            if entry.is_dir():
                children.append(entry)
            else:
                yield entry
        if budget.reason:
            return
        directories.extend(reversed(children))


def _validate_line_number(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolError(f"{name} 必须是大于 0 的整数")
    return value


def _read_line_range(target, start, end):
    budget = ScanBudget()
    parts = []
    size = 0
    number = 1
    at_line_start = True
    selected = False
    last_line = start
    with target.open("r", encoding="utf-8-sig", newline=None) as file:
        while end is None or number <= end:
            # 长行分块读取，跳过或拒绝它时也不把整行装入内存。
            chunk = file.readline(MAX_FILE_BYTES + 1)
            if not chunk:
                break
            budget.consume_bytes(len(chunk.encode("utf-8")))
            line_ended = chunk.endswith("\n")
            if number >= start:
                last_line = number
                piece = chunk.removesuffix("\n")
                if at_line_start and selected:
                    piece = "\n" + piece
                selected = True
                size += len(piece.encode("utf-8"))
                if size > MAX_FILE_BYTES:
                    raise ToolError("所选内容超过 64 KiB，请缩小行号范围")
                parts.append(piece)
            at_line_start = line_ended
            if line_ended:
                number += 1
    if not selected:
        raise ToolError("行号范围超出文件内容")
    return "".join(parts), last_line


def read_file(path, workspace, start_line=None, end_line=None):
    """读取 UTF-8 文本；全文或所选行的返回内容最多 64 KiB。"""
    target = _resolve(workspace, path)
    if not target.is_file():
        raise ToolError("不是普通文件")
    if start_line is None and end_line is None:
        with target.open("rb") as file:
            content = file.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ToolError("文件超过 64 KiB，请使用行号缩小范围或选择较小的文件")
        text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        start = 1 if text else 0
        end = text.count("\n") + int(bool(text) and not text.endswith("\n"))
        relative = _relative(workspace, target)
        return {"content": text, "path": relative, "start_line": start, "end_line": end,
                "reference": file_reference(relative, start, end)}
    if start_line is not None:
        start_line = _validate_line_number(start_line, "start_line")
    if end_line is not None:
        end_line = _validate_line_number(end_line, "end_line")
    start = start_line or 1
    if end_line is not None and end_line < start:
        raise ToolError("end_line 不能小于 start_line")
    text, actual_end = _read_line_range(target, start, end_line)
    relative = _relative(workspace, target)
    return {"content": text, "path": relative, "start_line": start, "end_line": actual_end,
            "reference": file_reference(relative, start, actual_end)}


def find_files(workspace, pattern, path=".", max_results=50):
    """按名称/相对路径的子串或 glob 查找文件，默认忽略大小写。"""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolError("pattern 必须是非空字符串")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ToolError("max_results 必须是大于 0 的整数")
    max_results = min(max_results, 200)
    pattern = pattern.replace("\\", "/").casefold()
    glob = any(character in pattern for character in "*?[")
    root = _resolve(workspace, path)
    budget = ScanBudget()
    files = [root] if root.is_file() else _walk_files(workspace, root, budget)
    matches = []
    truncated = False
    try:
        for file in files:
            budget.check_time()
            relative = _relative(workspace, _checked_target(workspace, file))
            candidate = relative if "/" in pattern else file.name
            candidate = candidate.casefold()
            if not (fnmatchcase(candidate, pattern) if glob else pattern in candidate):
                continue
            if len(matches) == max_results:
                truncated = True
                break
            matches.append(relative)
    except ScanLimit:
        pass
    result = {"files": matches, "truncated": truncated or bool(budget.reason)}
    if budget.reason:
        result["limit_reason"] = budget.reason
    return result


def search_text(workspace, query, path=".", case_sensitive=False, max_results=MAX_SEARCH_RESULTS):
    """在工作区文本文件中搜索字符串，返回匹配位置。"""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query 必须是非空字符串")
    if not isinstance(case_sensitive, bool):
        raise ToolError("case_sensitive 必须是布尔值")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ToolError("max_results 必须是大于 0 的整数")
    max_results = min(max_results, 200)

    root = _resolve(workspace, path)
    budget = ScanBudget()
    files = [root] if root.is_file() else _walk_files(workspace, root, budget)
    needle = query if case_sensitive else query.lower()
    matches = []
    skipped = 0
    truncated = False
    try:
        for file in files:
            budget.visit_file()
            try:
                target = _checked_target(workspace, file)
                if target.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    skipped += 1
                    continue
                with target.open("rb") as stream:
                    # 防止检查大小之后文件增长，或一次读取超过剩余总预算。
                    data = stream.read(min(MAX_SEARCH_FILE_BYTES + 1, budget.max_bytes - budget.bytes_read + 1))
                budget.consume_bytes(len(data))
                if len(data) > MAX_SEARCH_FILE_BYTES or b"\x00" in data[:1024]:
                    skipped += 1
                    continue
                text = data.decode("utf-8-sig", errors="replace")
            except ScanLimit:
                raise
            except (OSError, ValueError):
                skipped += 1
                continue
            for number, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
                budget.check_time()
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    if len(matches) == max_results:
                        truncated = True
                        break
                    matches.append({
                        "path": _relative(workspace, file), "line": number,
                        "reference": file_reference(_relative(workspace, file), number, number),
                        "text": line[:MAX_LINE_LENGTH],
                    })
            if truncated:
                break
    except ScanLimit:
        pass
    result = {"matches": matches, "truncated": truncated or bool(budget.reason), "skipped_files": skipped,
              "scanned_files": budget.files, "scanned_bytes": budget.bytes_read}
    if budget.reason:
        result["limit_reason"] = budget.reason
    return result


def list_directory(workspace, path=".", max_depth=2, max_entries=MAX_TREE_ENTRIES):
    """返回目录树文本；深度和条目数都有上限。"""
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        raise ToolError("max_depth 必须是整数")
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ToolError("max_entries 必须是大于 0 的整数")
    max_depth = min(max(0, max_depth), 5)
    max_entries = min(max_entries, 500)

    root = _resolve(workspace, path)
    if not root.is_dir():
        raise ToolError("不是目录")
    lines = ["." if root == Path(workspace).resolve() else _relative(workspace, root) + "/"]
    budget = ScanBudget()
    count = 0
    truncated = False

    def walk(directory, prefix, level):
        nonlocal count, truncated
        if count >= max_entries:
            truncated = True
            return
        entries = _directory_entries(workspace, directory, budget)
        for index, entry in enumerate(entries):
            budget.check_time()
            if count >= max_entries:
                truncated = True
                return
            count += 1
            last = index == len(entries) - 1
            branch = "└── " if last else "├── "
            is_dir = entry.is_dir()
            lines.append(prefix + branch + entry.name + ("/" if is_dir else ""))
            if is_dir and level < max_depth and not budget.reason:
                walk(entry, prefix + ("    " if last else "│   "), level + 1)
            if truncated:
                return

    try:
        walk(root, "", 0)
    except ScanLimit:
        pass
    result = {"tree": "\n".join(lines), "truncated": truncated or bool(budget.reason), "entries": count}
    if budget.reason:
        result["limit_reason"] = budget.reason
    return result
