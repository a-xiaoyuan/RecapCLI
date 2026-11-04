"""工作区文件读取、文本搜索、目录浏览及访问检查。"""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import os
from pathlib import Path
import re
import subprocess
import time

from ..base.access import local_path
from ..base.paths import SKIP_DIRS, ToolError, checked_target as _checked_target, sensitive_path

MAX_FILE_BYTES = 64 * 1024
MAX_MODEL_READ_LINES = 160
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 50
MAX_TREE_ENTRIES = 300
MAX_LINE_LENGTH = 300


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


def _resolve(workspace, path, *, must_exist=True):
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path 必须是非空字符串")
    candidate = local_path(path)
    if sensitive_path(candidate):
        raise ToolError("禁止访问敏感凭据文件或目录")
    target = _checked_target(workspace, Path(workspace) / candidate)
    if must_exist and not target.exists():
        raise ToolError(f"路径不存在：{path}")
    return target


def _relative(workspace, target):
    root = Path(workspace).resolve()
    return (target.relative_to(root) if target.is_relative_to(root) else target).as_posix()


def file_reference(path, start, end):
    if not start:
        return path
    return f"{path}:{start}" if start == end else f"{path}:{start}-{end}"


def _git_ignored(workspace, root, budget):
    """交给 Git 处理忽略、例外和嵌套规则；非 Git 目录沿用普通遍历。"""
    budget.check_time()
    workspace = root
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "--others", "--ignored", "--exclude-standard",
             "--directory", "-z", "--", str(root.relative_to(workspace))],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=max(0.1, budget.max_seconds - (time.monotonic() - budget.started)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode:
        return set()
    return {Path(workspace) / os.fsdecode(name).rstrip("/") for name in result.stdout.split(b"\0") if name}


def _directory_entries(workspace, directory, budget, ignored=()):
    """只列出允许访问的子项；遍历不跟随链接，避免越界和循环。"""
    entries = []
    try:
        for entry in directory.iterdir():
            budget.visit_entry()
            if entry.name in SKIP_DIRS:
                continue
            if ignored and any(path in ignored for path in (entry, *entry.parents)):
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
    ignored = _git_ignored(Path(workspace).resolve(), root, budget)
    directories = [root]
    while directories:
        children = []
        for entry in _directory_entries(workspace, directories.pop(), budget, ignored):
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


def _read_line_range(target, start, end, *, inspect_more=False):
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
        has_more = bool(file.read(1)) if inspect_more else False
    if not selected:
        if inspect_more and start == 1:
            return "", 0, False
        raise ToolError("行号范围超出文件内容")
    return "".join(parts), last_line, has_more


def read_file(path, workspace, start_line=None, end_line=None, *, max_lines=None, next_start_line=None):
    """读取 UTF-8 原文，最多 64 KiB；max_lines 供模型分页，省略时保持原有读取范围。"""
    target = _resolve(workspace, path)
    if not target.is_file():
        raise ToolError("不是普通文件")
    if start_line is None and next_start_line is not None:
        start_line = next_start_line
    if start_line is None and end_line is None and max_lines is None:
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
    end = end_line
    if max_lines is not None:
        end = min(end_line or start + max_lines - 1, start + max_lines - 1)
    text, actual_end, has_more = _read_line_range(target, start, end, inspect_more=max_lines is not None)
    if actual_end == 0:
        start = 0
    relative = _relative(workspace, target)
    result = {"content": text, "path": relative, "start_line": start, "end_line": actual_end,
              "reference": file_reference(relative, start, actual_end)}
    if max_lines is not None:
        result.update(has_more=has_more, truncated=has_more and (end_line is None or actual_end < end_line))
        if has_more:
            result["next_start_line"] = actual_end + 1
    return result


def read_file_for_model(path, workspace, start_line=None, end_line=None, *, next_start_line=None):
    """模型读取按行分页；原文 API 保留原有行为，不重复返回两份正文。"""
    result = read_file(path, workspace, start_line, end_line, max_lines=MAX_MODEL_READ_LINES,
                       next_start_line=next_start_line)
    start, end = result["start_line"], result["end_line"]
    if start:
        result["content"] = "\n".join(f"{number} | {line}" for number, line in
                                      zip(range(start, end + 1), result["content"].split("\n")))
    result["content_format"] = "line_numbered"
    return result


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
    match_base = Path(workspace).resolve()
    if not root.is_relative_to(match_base):
        match_base = root if root.is_dir() else root.parent
    budget = ScanBudget()
    files = [root] if root.is_file() else _walk_files(workspace, root, budget)
    matches = []
    truncated = False
    try:
        for file in files:
            budget.check_time()
            relative = _relative(workspace, _checked_target(workspace, file))
            candidate = _relative(match_base, file) if "/" in pattern else file.name
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


def search_text(workspace, query, path=".", case_sensitive=False, max_results=MAX_SEARCH_RESULTS,
                regex=False, whole_word=False, context_lines=0):
    """在工作区文本文件中搜索字符串，返回匹配位置。"""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query 必须是非空字符串")
    if not isinstance(case_sensitive, bool):
        raise ToolError("case_sensitive 必须是布尔值")
    if not isinstance(regex, bool) or not isinstance(whole_word, bool):
        raise ToolError("regex 和 whole_word 必须是布尔值")
    if isinstance(context_lines, bool) or not isinstance(context_lines, int) or not 0 <= context_lines <= 10:
        raise ToolError("context_lines 必须是 0 到 10 的整数")
    pattern = query if regex else re.escape(query)
    if whole_word:
        pattern = r"(?<!\w)(?:" + pattern + r")(?!\w)"
    try:
        matcher = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as error:
        raise ToolError(f"正则表达式无效：{error}") from error
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ToolError("max_results 必须是大于 0 的整数")
    max_results = min(max_results, 200)

    root = _resolve(workspace, path)
    budget = ScanBudget()
    files = [root] if root.is_file() else _walk_files(workspace, root, budget)
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
            lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            for number, line in enumerate(lines, 1):
                budget.check_time()
                if matcher.search(line):
                    if len(matches) == max_results:
                        truncated = True
                        break
                    matches.append({
                        "path": _relative(workspace, file), "line": number,
                        "reference": file_reference(_relative(workspace, file), number, number),
                        "text": line[:MAX_LINE_LENGTH],
                    })
                    if context_lines:
                        start, end = max(1, number - context_lines), min(len(lines), number + context_lines)
                        matches[-1]["context"] = "\n".join(f"{index}: {lines[index - 1][:MAX_LINE_LENGTH]}"
                                                           for index in range(start, end + 1))
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
    ignored = set()

    def walk(directory, prefix, level):
        nonlocal count, truncated
        if count >= max_entries:
            truncated = True
            return
        entries = _directory_entries(workspace, directory, budget, ignored)
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
        ignored = _git_ignored(Path(workspace).resolve(), root, budget)
        walk(root, "", 0)
    except ScanLimit:
        pass
    result = {"tree": "\n".join(lines), "truncated": truncated or bool(budget.reason), "entries": count}
    if budget.reason:
        result["limit_reason"] = budget.reason
    return result
