"""进程管道与原生 PTY；Windows Job / POSIX 进程组负责回收子进程。"""

import os
import signal
import subprocess
import sys


_PIPE_GATE = """import os, subprocess, sys
if os.read(sys.stdin.fileno(), 1) != b'1':
    raise SystemExit(125)
try:
    raise SystemExit(subprocess.call(sys.argv[1:]))
except OSError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(127)
"""
_PTY_GATE = """import subprocess, sys
sys.stdin.readline()
try:
    raise SystemExit(subprocess.call(sys.argv[1:]))
except OSError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(127)
"""


def _windows_job(process):
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
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ct.WinError(ct.get_last_error())
    native = getattr(process, "_handle", None)
    owned = native is None
    native = kernel.OpenProcess(0x0101, False, process.pid) if owned else int(native)
    limits = Extended()
    limits.basic.flags = 0x2000
    ok = native and kernel.SetInformationJobObject(handle, 9, ct.byref(limits), ct.sizeof(limits))
    ok = ok and kernel.AssignProcessToJobObject(handle, native)
    error = ct.get_last_error()
    if owned and native:
        kernel.CloseHandle(native)
    if not ok:
        kernel.CloseHandle(handle)
        raise ct.WinError(error)
    return lambda: kernel.CloseHandle(handle)


class Process:
    def __init__(self, argv, cwd, env, interactive=False):
        self.interactive = interactive
        self.close_job = None
        if interactive:
            if os.name == "nt":
                from winpty import PtyProcess
                self.child = PtyProcess.spawn([sys.executable, "-I", "-u", "-c", _PTY_GATE, *argv],
                                              cwd=str(cwd), env=env)
            else:
                from ptyprocess import PtyProcessUnicode
                self.child = PtyProcessUnicode.spawn(argv, cwd=str(cwd), env=env, echo=False)
        else:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            actual = [sys.executable, "-I", "-u", "-c", _PIPE_GATE, *argv] if os.name == "nt" else argv
            self.child = subprocess.Popen(actual, cwd=cwd, env=env, shell=False, bufsize=0,
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, **options)
        try:
            if os.name == "nt":
                self.close_job = _windows_job(self.child)
                self.write("\r" if interactive else "1")
        except BaseException:
            self.stop()
            raise

    def poll(self):
        if self.interactive:
            if self.child.isalive():
                return None
            code = self.child.exitstatus
            return code if code is not None else -(getattr(self.child, "signalstatus", None) or 1)
        return self.child.poll()

    def read(self):
        if self.interactive:
            return self.child.read(4096).encode("utf-8", "replace")
        return self.child.stdout.read(4096)

    def write(self, text):
        if self.interactive:
            self.child.write(text.replace("\r\n", "\n").replace("\n", "\r") if os.name == "nt" else text)
        else:
            self.child.stdin.write(text.encode("utf-8"))
            self.child.stdin.flush()

    def eof(self):
        if self.interactive:
            if os.name == "nt":
                raise ValueError("Windows PTY 没有通用 EOF；请发送程序的退出命令或停止会话")
            self.child.sendeof()
        else:
            self.child.stdin.close()

    def stop(self):
        if self.close_job:
            self.close_job()
            self.close_job = None
        elif os.name != "nt":
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.interactive:
            if self.child.isalive():
                self.child.terminate(force=True)
        else:
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait()

    def close(self):
        if self.interactive:
            self.child.close(force=True)
        else:
            self.child.stdout.close()
            if not self.child.stdin.closed:
                self.child.stdin.close()
