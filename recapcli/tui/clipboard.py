"""复制回答到系统剪贴板。"""

import os

from .widgets import ConversationLine



def _copy_windows_clipboard(text):
    """直接写入 Windows Unicode 剪贴板，兼容不支持 OSC 52 的 IDE 终端。"""
    import ctypes as ct
    from ctypes import wintypes as wt

    user = ct.WinDLL("user32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    user.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                    ct.c_int, ct.c_int, ct.c_int, ct.c_int,
                                    wt.HWND, wt.HMENU, wt.HINSTANCE, ct.c_void_p]
    user.CreateWindowExW.restype = wt.HWND
    user.DestroyWindow.argtypes, user.DestroyWindow.restype = [wt.HWND], wt.BOOL
    user.OpenClipboard.argtypes, user.OpenClipboard.restype = [wt.HWND], wt.BOOL
    user.CloseClipboard.argtypes, user.CloseClipboard.restype = [], wt.BOOL
    user.EmptyClipboard.argtypes, user.EmptyClipboard.restype = [], wt.BOOL
    user.SetClipboardData.argtypes, user.SetClipboardData.restype = [wt.UINT, wt.HANDLE], wt.HANDLE
    kernel.GlobalAlloc.argtypes, kernel.GlobalAlloc.restype = [wt.UINT, ct.c_size_t], wt.HANDLE
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [wt.HANDLE], ct.c_void_p
    kernel.GlobalUnlock.argtypes, kernel.GlobalUnlock.restype = [wt.HANDLE], wt.BOOL
    kernel.GlobalFree.argtypes, kernel.GlobalFree.restype = [wt.HANDLE], wt.HANDLE

    # OpenClipboard 的所有者需要有效窗口；IDE 终端不一定有控制台窗口。
    window = user.CreateWindowExW(0, "STATIC", "RecapCLI clipboard", 0, 0, 0, 0, 0,
                                 None, None, None, None)
    if not window:
        raise ct.WinError(ct.get_last_error())
    memory, opened = None, False
    try:
        data = (text + "\0").encode("utf-16-le")
        memory = kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not memory:
            raise ct.WinError(ct.get_last_error())
        pointer = kernel.GlobalLock(memory)
        if not pointer:
            raise ct.WinError(ct.get_last_error())
        ct.memmove(pointer, data, len(data))
        kernel.GlobalUnlock(memory)
        if not user.OpenClipboard(window):
            raise ct.WinError(ct.get_last_error())
        opened = True
        if not user.EmptyClipboard() or not user.SetClipboardData(13, memory):  # CF_UNICODETEXT
            raise ct.WinError(ct.get_last_error())
        memory = None  # 系统接管内存，成功后不能释放。
    finally:
        if opened:
            user.CloseClipboard()
        if memory:
            kernel.GlobalFree(memory)
        user.DestroyWindow(window)


class ClipboardMixin:
    def copy_to_clipboard(self, text):
        if os.name != "nt":
            super().copy_to_clipboard(text)
            return True
        try:
            _copy_windows_clipboard(text)
        except OSError as error:
            self.notify(f"复制失败：无法写入系统剪贴板（{error}）。", severity="error")
            return False
        self._clipboard = text
        return True

    def action_copy_reply(self, *, all_messages=False):
        rows = [row for row in self.query(ConversationLine)
                if row.kind in {"user", "assistant"} and row.content_text]
        if all_messages:
            text = "\n\n".join(("用户" if row.kind == "user" else "RecapCLI") + "\n" + row.content_text
                               for row in rows)
        else:
            text = next((row.content_text for row in reversed(rows) if row.kind == "assistant"), "")
        if not text:
            self.notify("暂无可复制的对话。")
            return
        if self.copy_to_clipboard(text):
            self.notify("已复制完整对话。" if all_messages else "已复制完整回答。")

    def action_copy(self):
        self.action_copy_reply()
