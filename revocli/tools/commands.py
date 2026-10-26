"""有时间与输出预算的前台进程执行；工作目录约束不是操作系统沙箱。"""

import codecs
from datetime import datetime
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from ..storage.files import state_directory, write_json
from .filesystem import ToolError, _relative, _resolve

# Windows 的实际命令先等待启动信号，确保加入 Job 后才允许创建子进程。
_WINDOWS_GATE = """import subprocess, sys
if not sys.stdin.buffer.read(1):
    raise SystemExit(125)
try:
    code = subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)
except OSError as error:
    print(str(error), file=sys.stderr)
    code = 127
raise SystemExit(code)
"""
OUTPUT_ENCODINGS = {"utf-8", "utf-8-sig", "gb18030", "gbk", "ascii", "cp1252", "utf-16", "utf-16-le", "utf-16-be"}


def _windows_job(process):
    """关闭 Job 句柄时回收其进程树，包括命令遗留的子进程。

    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """
    import ctypes as ct
    from ctypes import wintypes as wt

    class Basic(ct.Structure):
        _fields_ = [("process_time", ct.c_int64), ("job_time", ct.c_int64), ("flags", wt.DWORD),
                    ("min_working_set", ct.c_size_t), ("max_working_set", ct.c_size_t),
                    ("active_processes", wt.DWORD), ("affinity", ct.c_size_t),
                    ("priority", wt.DWORD), ("scheduling", wt.DWORD)]

    class Extended(ct.Structure):
        _fields_ = [("basic", Basic), ("io", ct.c_uint64 * 6),
                    ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
                    ("peak_process_memory", ct.c_size_t), ("peak_job_memory", ct.c_size_t)]

    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ct.c_void_p, wt.LPCWSTR], wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ct.WinError(ct.get_last_error())
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not kernel.SetInformationJobObject(handle, 9, ct.byref(limits), ct.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
        error = ct.WinError(ct.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    return lambda: kernel.CloseHandle(handle)


def run_command(workspace, argv, cwd=".", timeout_seconds=30, max_output_bytes=16384, encoding="utf-8", *, on_record=None):
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv) or not argv[0].strip()):
        raise ToolError("argv 必须是非空字符串数组，最多 64 项，首项为可执行文件")
    if sum(len(arg.encode("utf-8")) for arg in argv) > 32768:
        raise ToolError("命令参数总长度超过上限")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ToolError("timeout_seconds 必须是有限正数")
    timeout_seconds = min(timeout_seconds, 60)
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
        raise ToolError("cwd 必须是工作区内的目录")
    command_id = uuid.uuid4().hex
    record_path = state_directory(workspace, "commands") / f"{command_id}.json"
    record = {"schema_version": 1, "id": command_id, "argv": argv, "cwd": _relative(workspace, directory),
              "started_at": datetime.now().astimezone().isoformat(), "status": "running"}
    write_json(record_path, record)  # 记录不可写时，不启动命令。
    if on_record is not None:
        on_record("command", command_id)
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() != "API_KEY" and not key.upper().endswith("_API_KEY")}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    output = bytearray()
    exceeded = threading.Event()
    process, reader, close_job = None, None, None
    started = time.monotonic()
    reason = ""

    def capture():
        try:
            while data := process.stdout.read(4096):
                remaining = max_output_bytes - len(output)
                output.extend(data[:remaining])
                if len(data) > remaining:
                    exceeded.set()
        except OSError:
            pass

    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        actual_argv = [sys.executable, "-I", "-u", "-c", _WINDOWS_GATE, *argv] if os.name == "nt" else argv
        process = subprocess.Popen(actual_argv, cwd=directory, env=environment, shell=False, bufsize=0,
                                   stdin=subprocess.PIPE if os.name == "nt" else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
        if os.name == "nt":
            close_job = _windows_job(process)
            process.stdin.write(b"1")
            process.stdin.close()
        reader = threading.Thread(target=capture, daemon=True)
        reader.start()
        while process.poll() is None:
            if exceeded.wait(0.01):
                reason = "命令输出超过上限，已停止"
                break
            if time.monotonic() - started >= timeout_seconds:
                reason = "命令执行超时，已停止"
                break
        record["status"] = "finished"
    except OSError as error:
        reason = f"命令启动失败：{error}"
        record["status"] = "failed"
    except BaseException:
        reason = "命令被中断"
        record["status"] = "interrupted"
        raise
    finally:
        if process is not None:
            if close_job is not None:
                close_job()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if reader is not None:
                reader.join(timeout=2)
            process.stdout.close()
        if exceeded.is_set() and not reason:
            reason = "命令输出超过上限，已停止"
        returncode = process.returncode if process is not None else None
        if returncode and not reason:
            reason = f"命令退出码为 {returncode}"
        decoded = bytes(output).decode(encoding, errors="replace").encode("utf-8", errors="replace").decode("utf-8")
        record.update(returncode=returncode, output=decoded,
                      output_truncated=exceeded.is_set(), timed_out="超时" in reason,
                      duration_seconds=round(time.monotonic() - started, 3), error=reason)
        write_json(record_path, record)
    result = {"ok": not reason, "returncode": returncode, "output": record["output"],
              "output_truncated": record["output_truncated"], "timed_out": record["timed_out"],
              "record_id": command_id, "duration_seconds": record["duration_seconds"],
              "summary": f"命令退出码 {returncode}，执行记录 ID：{command_id}"}
    if reason:
        result["error"] = f"{reason}；执行记录 ID：{command_id}"
    return result
