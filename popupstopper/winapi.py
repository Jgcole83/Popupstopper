"""Thin ctypes bindings for the Win32 calls Popup Stopper needs.

Kept dependency-free on purpose: everything here is available from the
standard library, so the app needs no compiler and no native packages.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# -- constants -------------------------------------------------------------

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_DIALOGSTART = 0x0010
EVENT_SYSTEM_DIALOGEND = 0x0011
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_CREATE = 0x8000

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

OBJID_WINDOW = 0
CHILDID_SELF = 0

GA_ROOT = 2
GW_OWNER = 4

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_DLGFRAME = 0x00400000
WS_SYSMENU = 0x00080000

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_DLGMODALFRAME = 0x00000001

WM_CLOSE = 0x0010
WM_QUIT = 0x0012
SMTO_ABORTIFHUNG = 0x0002

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# -- prototypes ------------------------------------------------------------

WinEventProcType = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # hWinEventHook
    wintypes.DWORD,  # event
    wintypes.HWND,  # hwnd
    wintypes.LONG,  # idObject
    wintypes.LONG,  # idChild
    wintypes.DWORD,  # dwEventThread
    wintypes.DWORD,  # dwmsEventTime
)

EnumChildProcType = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HMODULE,
    WinEventProcType,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumChildProcType, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND

# GetWindowLongPtrW only exists in 64-bit user32; fall back for 32-bit Python.
_get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_get_window_long.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# -- helpers ---------------------------------------------------------------


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_styles(hwnd: int) -> tuple[int, int]:
    return (
        _get_window_long(hwnd, GWL_STYLE) & 0xFFFFFFFF,
        _get_window_long(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF,
    )


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_process_path(pid: int) -> str:
    """Full image path for a PID, or "" when it cannot be opened."""
    if pid <= 0:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_top_level(hwnd: int) -> bool:
    return bool(hwnd) and user32.GetAncestor(hwnd, GA_ROOT) == hwnd


def get_owner(hwnd: int) -> int:
    return int(user32.GetWindow(hwnd, GW_OWNER) or 0)


def get_child_texts(hwnd: int, limit: int = 12) -> list[str]:
    """Read the static/label text inside a dialog, which is its actual message."""
    texts: list[str] = []

    def callback(child: int, _lparam: int) -> bool:
        if len(texts) >= limit:
            return False
        cls = get_class_name(child)
        if cls.lower() in ("static", "edit", "richedit20w", "directuihwnd", "syslink"):
            text = get_window_text(child).strip()
            if text and text not in texts:
                texts.append(text)
        return True

    try:
        user32.EnumChildWindows(hwnd, EnumChildProcType(callback), 0)
    except OSError:
        pass
    return texts


def close_window(hwnd: int) -> bool:
    if not user32.IsWindow(hwnd):
        return False
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def is_admin() -> bool:
    try:
        return bool(ctypes.WinDLL("shell32", use_last_error=True).IsUserAnAdmin())
    except OSError:
        return False
