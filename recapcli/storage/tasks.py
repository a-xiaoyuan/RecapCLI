"""E0 任务事实：独立于成功会话保存，不从模型自述推断验收结果。"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from .files import data_root, state_directory


SCHEMA_VERSION = 1
TERMINAL_STATES = {"returned", "failed", "cancelled", "blocked", "incomplete", "unknown"}


def _now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _process_identity(pid):
    """返回进程出生标识；None 表示已退出，unknown 表示无法确认。"""
    if os.name == "nt":
        import ctypes as ct
        from ctypes import wintypes as wt

        kernel = ct.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
        kernel.GetProcessTimes.argtypes = [wt.HANDLE, *([ct.POINTER(wt.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ct.get_last_error() == 87 else "unknown"
        try:
            code = wt.DWORD()
            if not kernel.GetExitCodeProcess(handle, ct.byref(code)):
                return "unknown"
            if code.value != 259:
                return None
            times = [wt.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ct.byref(value) for value in times)):
                return "unknown"
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        if Path("/proc").is_dir():
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19]
        os.kill(pid, 0)
        return "unknown"
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        return "unknown"


def project_version(workspace):
    """Git 版本和有界的工作区指纹；无法完整采集时明确标记。"""
    def git(*arguments):
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "maintenance.auto=false",
             "-C", str(workspace), *arguments], capture_output=True,
            stdin=subprocess.DEVNULL, timeout=3, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout

    try:
        head = git("rev-parse", "HEAD").decode().strip()
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
        diff = git("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--", ".")
        untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--", ".")
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None, "fingerprint": None, "complete": False}
    digest = hashlib.sha256(status + diff)
    complete = True
    size = 0
    started = time.monotonic()
    for index, raw_path in enumerate(untracked.split(b"\0")):
        if not raw_path:
            continue
        path = workspace / os.fsdecode(raw_path)
        # 不采集凭据内容，也不跟随链接。省略必须反映在 complete 中。
        if (index >= 200 or size >= 8 * 1024 * 1024 or time.monotonic() - started > 2
                or path.is_symlink() or path.is_junction()
                or not path.resolve().is_relative_to(workspace)
                or path.name == ".env" or path.name.startswith(".env.")):
            complete = False
            continue
        try:
            with path.open("rb") as file:
                content = file.read(min(1024 * 1024, 8 * 1024 * 1024 - size) + 1)
            size += len(content)
            if len(content) > 1024 * 1024 or size > 8 * 1024 * 1024:
                complete = False
            digest.update(raw_path + b"\0" + content)
        except OSError:
            complete = False
    return {"head": head, "dirty": bool(status), "fingerprint": digest.hexdigest(), "complete": complete}


class TaskStore:
    def __init__(self, workspace, *, create=True):
        self.workspace = Path(workspace).resolve()
        directory = data_root(self.workspace) / "tasks"
        if create:
            directory = state_directory(self.workspace, "tasks")
        self.path = directory / "tasks.sqlite3"
        for candidate in (directory.parent, directory, self.path):
            if (candidate.is_symlink() or candidate.is_junction()
                    or not candidate.resolve().is_relative_to(self.workspace)):
                raise ValueError("任务存储不能通过链接访问或位于工作区之外")
        if create:
            self._initialize()

    @contextmanager
    def _connection(self, *, readonly=False):
        mode = "ro" if readonly else "rwc"
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + f"?mode={mode}", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise OSError(f"任务存储无法读写（{self.path.name}）：{error}") from error
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self):
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f"不支持的任务存储版本：{version}")
            if version == SCHEMA_VERSION:
                self._check_workspace(connection)
                return
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (workspace TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, prompt TEXT NOT NULL,
                    workspace TEXT NOT NULL, mode TEXT NOT NULL, model TEXT NOT NULL,
                    metadata TEXT NOT NULL, owner_pid INTEGER NOT NULL, owner_identity TEXT,
                    started_at TEXT NOT NULL, ended_at TEXT, duration_seconds REAL,
                    status TEXT NOT NULL DEFAULT 'running', stop_reason TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'unverified', answer TEXT,
                    model_requests INTEGER NOT NULL DEFAULT 0,
                    usage TEXT, plan TEXT
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES task_runs(id),
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, sequence);
            """)
            row = connection.execute("SELECT workspace FROM metadata").fetchone()
            if row is None:
                connection.execute("INSERT INTO metadata VALUES (?)", (str(self.workspace),))
            elif row[0] != str(self.workspace):
                raise ValueError("任务存储不属于当前工作区，移动项目后需要明确迁移")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _check_workspace(self, connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("不支持的任务存储版本")
        row = connection.execute("SELECT workspace FROM metadata").fetchone()
        if row is None or row[0] != str(self.workspace):
            raise ValueError("任务存储不属于当前工作区")

    def start(self, session_id, prompt, model, mode, metadata):
        task_id = uuid.uuid4().hex
        with self._connection() as connection:
            self._check_workspace(connection)
            connection.execute("""INSERT INTO task_runs
                (id, session_id, prompt, workspace, mode, model, metadata,
                 owner_pid, owner_identity, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, session_id, prompt, str(self.workspace), mode, model,
                 json.dumps(metadata, ensure_ascii=False), os.getpid(), _process_identity(os.getpid()), _now()))
        return task_id

    def append(self, task_id, kind, data):
        with self._connection() as connection:
            self._check_workspace(connection)
            row = connection.execute("SELECT status FROM task_runs WHERE id=?", (task_id,)).fetchone()
            if row is None or row[0] != "running":
                raise ValueError("任务不存在或已经结束，不能追加执行事件")
            connection.execute("INSERT INTO task_events(task_id, created_at, kind, data) VALUES (?, ?, ?, ?)",
                               (task_id, _now(), kind, json.dumps(data, ensure_ascii=False)))
            if kind == "model_request":
                connection.execute("UPDATE task_runs SET model_requests=model_requests+1 WHERE id=?", (task_id,))

    def finish(self, task_id, status, reason, *, answer=None, duration=None):
        if status not in TERMINAL_STATES:
            raise ValueError("任务终态不正确")
        with self._connection() as connection:
            self._check_workspace(connection)
            rows = connection.execute("SELECT kind, data FROM task_events WHERE task_id=? ORDER BY sequence",
                                      (task_id,)).fetchall()
            requests = 0
            usages = []
            plan = None
            for row in rows:
                data = json.loads(row["data"])
                if row["kind"] == "model_request":
                    requests += 1
                elif row["kind"] == "model_response":
                    usages.append(data.get("usage"))
                elif row["kind"] == "plan":
                    plan = data
            # 缺少任意一次 usage 就保留未知，不能把未知当零。
            usage = None
            if requests and len(usages) == requests and all(isinstance(item, dict) for item in usages):
                keys = ("prompt_tokens", "completion_tokens", "total_tokens")
                usage = {key: sum(item[key] for item in usages)
                         if all(isinstance(item.get(key), int) for item in usages) else None for key in keys}
            changed = connection.execute("""UPDATE task_runs SET status=?, stop_reason=?, ended_at=?,
                duration_seconds=?, answer=?, model_requests=?, usage=?, plan=?
                WHERE id=? AND status='running'""",
                (status, reason, _now(), duration, answer, requests,
                 json.dumps(usage), json.dumps(plan, ensure_ascii=False), task_id)).rowcount
            if not changed:
                raise ValueError("任务不存在或已经结束")

    def recover_interrupted(self):
        """只回收可确认已退出的任务所属进程，不重放任何动作。"""
        if not self.path.exists():
            return []
        with self._connection(readonly=True) as connection:
            self._check_workspace(connection)
            rows = connection.execute("SELECT id, owner_pid, owner_identity FROM task_runs WHERE status='running'").fetchall()
        recovered = []
        for row in rows:
            identity = _process_identity(row["owner_pid"])
            reused = (identity not in (None, "unknown") and row["owner_identity"] not in (None, "unknown")
                      and identity != row["owner_identity"])
            if identity is None or reused:
                try:
                    self.append(row["id"], "recovery", {"reason": "所属进程已退出，未闭合动作的结果需要人工核对"})
                    self.finish(row["id"], "unknown", "进程中断后恢复：执行结果未知，未自动重试或回滚")
                except ValueError:
                    # 另一查看进程可能已完成同一条对账；保留其终态。
                    if self.get(row["id"])["status"] != "running":
                        continue
                    raise
                recovered.append(row["id"])
        return recovered

    def verify(self, task_id, status, command_id, note):
        """根据已记录命令登记人工验收；此入口不注册为模型工具。"""
        if status not in {"passed", "failed"} or not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise ValueError("验收状态须为 passed/failed，并提供 1 到 2000 字的验收范围和依据")
        task = self.get(task_id)
        if task["status"] == "running":
            raise ValueError("任务仍在执行，暂不能登记验收")
        results = [event["data"]["result"] for event in task["events"]
                   if event["kind"] == "tool_result" and event["data"]["name"] == "run_command"
                   and event["data"]["result"].get("record_id") == command_id]
        if not results:
            raise ValueError("验收必须引用该任务已保存的命令结果 ID")
        result = results[-1]
        if status == "passed" and (not result.get("ok") or result.get("returncode") != 0):
            raise ValueError("命令未正常退出，不能登记为通过")
        evidence = {"source": "user", "status": status, "command_id": command_id, "note": note.strip()}
        with self._connection() as connection:
            self._check_workspace(connection)
            connection.execute("INSERT INTO task_events(task_id, created_at, kind, data) VALUES (?, ?, 'verification', ?)",
                               (task_id, _now(), json.dumps(evidence, ensure_ascii=False)))
            connection.execute("UPDATE task_runs SET verification_status=? WHERE id=?", (status, task_id))

    def list_tasks(self, limit=20):
        if not self.path.exists():
            return []
        with self._connection(readonly=True) as connection:
            self._check_workspace(connection)
            rows = connection.execute("""SELECT id, session_id, prompt, mode, model, started_at,
                status, verification_status, stop_reason FROM task_runs ORDER BY started_at DESC, rowid DESC LIMIT ?""",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, task_id):
        if not isinstance(task_id, str) or len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError("任务 ID 必须是完整的 32 位十六进制字符串")
        if not self.path.exists():
            raise ValueError(f"任务不存在：{task_id}")
        with self._connection(readonly=True) as connection:
            self._check_workspace(connection)
            row = connection.execute("SELECT * FROM task_runs WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在：{task_id}")
            events = connection.execute("SELECT sequence, created_at, kind, data FROM task_events WHERE task_id=? ORDER BY sequence",
                                        (task_id,)).fetchall()
        task = dict(row)
        for key in ("metadata", "usage", "plan"):
            task[key] = json.loads(task[key]) if task[key] is not None else None
        task["events"] = [{**dict(event), "data": json.loads(event["data"])} for event in events]
        return task
