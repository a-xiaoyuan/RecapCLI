"""前台命令、后台任务与可继续输入的 PTY 会话，共用权限及命令记录。"""

import atexit
import codecs
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from ..storage.files import state_directory, write_json
from .filesystem import ToolError, _relative, _resolve
from .processes import Process

OUTPUT_ENCODINGS = {"utf-8", "utf-8-sig", "gb18030", "gbk", "ascii", "cp1252", "utf-16", "utf-16-le", "utf-16-be"}
_commands = {}
_lock = threading.RLock()


def subprocess_environment(overrides=None):
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES", "OS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "NO_COLOR", "VIRTUAL_ENV",
        "JAVA_HOME", "JDK_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_HOME",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({str(key): str(value) for key, value in (overrides or {}).items()})
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return environment


class Command:
    def __init__(self, workspace, argv, directory, timeout, encoding, interactive, capture_bytes, on_record):
        self.workspace = workspace
        self.id = uuid.uuid4().hex
        self.path = state_directory(workspace, "commands") / f"{self.id}.json"
        self.started = time.monotonic()
        self.ended = None
        self.timeout, self.encoding, self.capture_bytes = timeout, encoding, capture_bytes
        self.lock, self.input_lock = threading.RLock(), threading.Lock()
        self.done, self.stopping = threading.Event(), threading.Event()
        self.output, self.total = bytearray(), 0
        self.error, self.returncode = "", None
        self.status, self.process = "running", None
        self.record = {"schema_version": 1, "id": self.id, "argv": argv,
                       "cwd": _relative(workspace, directory), "status": "running", "owner_pid": os.getpid(),
                       "interactive": interactive, "timeout_seconds": timeout,
                       "started_at": datetime.now().astimezone().isoformat()}
        write_json(self.path, self.record)
        if on_record is not None:
            on_record("command", self.id)
        try:
            self.process = Process(argv, directory, subprocess_environment(), interactive)
        except (OSError, ValueError) as error:
            self.error, self.status = f"命令启动失败：{error}", "failed"
            self.ended = time.monotonic()
            self.done.set()
            self.persist()
            raise ToolError(self.error) from error
        self.reader = threading.Thread(target=self.capture, daemon=True, name="recapcli-output")
        self.reader.start()

    def capture(self):
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        try:
            while data := self.process.read():
                data = decoder.decode(data).encode("utf-8")
                with self.lock:
                    self.total += len(data)
                    self.output.extend(data)
                    if len(self.output) > self.capture_bytes:
                        del self.output[:-self.capture_bytes]
        except (OSError, EOFError):
            pass

    def watch(self):
        try:
            while self.process.poll() is None:
                if self.stopping.wait(0.03):
                    self.error, self.status = "命令已停止", "stopped"
                    break
                if time.monotonic() - self.started >= self.timeout:
                    self.error, self.status = "命令执行超时，已停止", "timed_out"
                    break
            self.returncode = self.process.poll()
            self.process.stop()  # 即使父进程成功退出，也回收遗留子进程。
            if self.returncode is None:
                self.returncode = self.process.poll()
            self.reader.join(timeout=2)
            self.process.close()
            if self.status == "running":
                self.status = "finished"
        finally:
            self.ended = time.monotonic()
            try:
                self.persist()
            except (OSError, ValueError) as error:
                self.error, self.status = f"命令记录保存失败：{error}", "failed"
            finally:
                self.done.set()

    def result(self, offset=0, max_output_bytes=16384):
        with self.lock:
            earliest = self.total - len(self.output)
            start = max(offset, earliest)
            data = bytes(self.output[max(0, start - earliest):max(0, start - earliest) + max_output_bytes])
            return {"ok": not self.error, "success": self.status == "finished" and self.returncode == 0,
                    "status": self.status, "returncode": self.returncode,
                    "output": re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", data.decode("utf-8", errors="replace")),
                    "output_truncated": offset < earliest or start + len(data) < self.total,
                    "next_offset": start + len(data), "earliest_offset": earliest,
                    "total_output_bytes": self.total, "timed_out": self.status == "timed_out",
                    "record_id": self.id, "duration_seconds": round((self.ended or time.monotonic()) - self.started, 3),
                    "error": self.error, "summary": f"命令 {self.id} · {self.status}"}

    def persist(self):
        result = self.result(max_output_bytes=self.capture_bytes)
        with self.lock:
            result["output"] = bytes(self.output).decode("utf-8", "replace")
        write_json(self.path, {**self.record, **result})


def _key(workspace, record_id):
    if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{32}", record_id):
        raise ToolError("record_id 必须是完整命令 ID")
    return str(Path(workspace).resolve()), record_id


def _live(workspace, record_id):
    with _lock:
        command = _commands.get(_key(workspace, record_id))
    if command is None:
        raise ToolError("该命令不属于本次运行，不能继续输入或停止；可查询已保存的记录")
    return command


def run_command(workspace, argv, cwd=".", timeout_seconds=300, max_output_bytes=16384, encoding="utf-8",
                stdin_text="", background=False, interactive=False, *, on_record=None, check=None):
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv) or not argv[0].strip()):
        raise ToolError("argv 必须是非空字符串数组，最多 64 项，首项为可执行文件")
    if sum(len(arg.encode("utf-8")) for arg in argv) > 32768:
        raise ToolError("命令参数总长度超过上限")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400):
        raise ToolError("timeout_seconds 必须大于 0 且不超过 86400")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or not 1 <= max_output_bytes <= 65536:
        raise ToolError("max_output_bytes 必须在 1 到 65536 之间")
    try:
        encoding = codecs.lookup(encoding).name
        if encoding not in OUTPUT_ENCODINGS:
            raise LookupError(encoding)
    except (LookupError, TypeError) as error:
        raise ToolError("不支持的输出编码") from error
    directory = _resolve(workspace, cwd)
    if not directory.is_dir():
        raise ToolError("cwd 必须是已授权目录")
    if not isinstance(stdin_text, str) or not isinstance(background, bool) or not isinstance(interactive, bool):
        raise ToolError("stdin_text 为文本，background 和 interactive 为布尔值")
    workspace = Path(workspace).resolve()
    command = Command(workspace, argv, directory, timeout_seconds,
                      "utf-8" if interactive else encoding, interactive, 1024 * 1024, on_record)
    with _lock:
        # 已完成命令从内存释放，历史查询继续读取磁盘记录。
        for key in [key for key, value in _commands.items() if value.done.is_set()]:
            del _commands[key]
        _commands[_key(workspace, command.id)] = command
    monitor = threading.Thread(target=command.watch, daemon=True, name="recapcli-command")
    monitor.start()

    def feed():
        try:
            with command.input_lock:
                if stdin_text:
                    command.process.write(stdin_text)
                if not interactive:
                    command.process.eof()
        except (OSError, EOFError):
            pass

    threading.Thread(target=feed, daemon=True, name="recapcli-stdin").start()
    try:
        if background or interactive:
            command.done.wait(0.1)
        else:
            while not command.done.wait(0.05):
                if check is not None:
                    check()
    except BaseException:
        command.stopping.set()
        command.done.wait(3)
        raise
    result = command.result(max_output_bytes=max_output_bytes)
    if result["status"] == "running":
        result["summary"] += "；用 command_status 查询，interactive 会话可用 command_input 继续输入"
    return result


def command_status(workspace, record_id, offset=0, max_output_bytes=16384, wait_seconds=0, *, check=None):
    key = _key(workspace, record_id)
    if offset < 0 or not 1 <= max_output_bytes <= 65536 or not 0 <= wait_seconds <= 30:
        raise ToolError("offset 非负，输出上限 1–65536 字节，等待时间 0–30 秒")
    with _lock:
        command = _commands.get(key)
    if command is not None:
        deadline = time.monotonic() + wait_seconds
        while not command.done.wait(min(0.05, max(0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                break
            if check is not None:
                check()
        return command.result(offset, max_output_bytes)
    path = state_directory(workspace, "commands") / f"{record_id}.json"
    if path.is_symlink() or path.is_junction():
        raise ToolError("命令记录不能是链接")
    record = json.loads(path.read_text(encoding="utf-8"))
    data = record.get("output", "").encode("utf-8")
    earliest = record.get("earliest_offset", 0)
    start = max(offset, earliest)
    segment = data[max(0, start - earliest):max(0, start - earliest) + max_output_bytes]
    if record["status"] == "running":
        record.update(ok=False, success=False, status="unknown", error="所属运行已结束，无法重新连接该命令；未自动重放")
    return {**record, "output": segment.decode("utf-8", "replace"), "next_offset": start + len(segment),
            "output_truncated": offset < earliest or start + len(segment) < earliest + len(data)}


def command_input(workspace, record_id, text, eof=False):
    command = _live(workspace, record_id)
    if not command.record["interactive"] or command.done.is_set():
        raise ToolError("只有本次运行中尚未退出的 interactive 会话可以继续输入")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 65536:
        raise ToolError("输入文本最多 64 KiB")
    with command.input_lock:
        command.process.write(text)
        if eof:
            command.process.eof()
    return {"ok": True, "record_id": record_id, "summary": "输入已发送；用 command_status 查询实际处理结果"}


def stop_command(workspace, record_id):
    command = _live(workspace, record_id)
    command.stopping.set()
    stopped = command.done.wait(3)
    return {**command.result(), "ok": stopped, "summary": f"命令 {record_id} {'已停止' if stopped else '正在停止'}"}


def list_commands(workspace):
    root = str(Path(workspace).resolve())
    with _lock:
        return [{**value.record, **value.result(max_output_bytes=512)}
                for key, value in _commands.items() if key[0] == root]


def close_commands(workspace=None):
    with _lock:
        commands = [value for key, value in _commands.items()
                    if workspace is None or key[0] == str(Path(workspace).resolve())]
    for command in commands:
        command.stopping.set()
    for command in commands:
        command.done.wait(3)


atexit.register(close_commands)
