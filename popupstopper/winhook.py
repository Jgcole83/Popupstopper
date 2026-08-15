"""Live detection of new top-level windows via SetWinEventHook.

Windows announces every window that appears through the accessibility event
stream. Hooking it out-of-context gives us system-wide visibility with no
injection and no polling, and tells us about a popup at the moment it is
shown rather than after the fact.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable

from . import winapi as w
from .config import IGNORED_WINDOW_CLASSES

log = logging.getLogger(__name__)

DIALOG_CLASS = "#32770"

_EVENT_NAMES = {
    w.EVENT_SYSTEM_FOREGROUND: "foreground",
    w.EVENT_SYSTEM_DIALOGSTART: "dialog",
    w.EVENT_OBJECT_SHOW: "show",
}


@dataclass
class WindowEvent:
    hwnd: int
    pid: int
    title: str
    window_class: str
    exe_path: str
    exe_name: str
    event_name: str
    is_popup_like: bool
    is_topmost: bool
    is_fullscreen_ish: bool
    owner: int
    rect: tuple[int, int, int, int]
    ts: float = field(default_factory=time.time)
    body: str = ""


class WindowWatcher:
    """Runs a Win32 message loop on its own thread and reports new windows."""

    def __init__(self, on_window: Callable[[WindowEvent], None]) -> None:
        self._on_window = on_window
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._running = threading.Event()
        self._own_pid = os.getpid()
        # hwnd -> last reported timestamp, so a window that fires several
        # events in a row is only reported once.
        self._recent: dict[int, float] = {}
        self._recent_lock = threading.Lock()
        self._callback_ref = None  # keep the ctypes trampoline alive

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="window-watcher", daemon=True)
        self._thread.start()
        self._running.wait(timeout=5)

    def stop(self) -> None:
        if self._thread_id:
            w.user32.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3)
        self._running.clear()

    @property
    def is_running(self) -> bool:
        return self._running.is_set() and bool(self._thread and self._thread.is_alive())

    # -- hook thread -------------------------------------------------------

    def _run(self) -> None:
        self._thread_id = w.kernel32.GetCurrentThreadId()
        callback = w.WinEventProcType(self._handle_event)
        self._callback_ref = callback
        flags = w.WINEVENT_OUTOFCONTEXT | w.WINEVENT_SKIPOWNPROCESS

        hooks = [
            w.user32.SetWinEventHook(
                w.EVENT_SYSTEM_FOREGROUND, w.EVENT_SYSTEM_DIALOGSTART, None, callback, 0, 0, flags
            ),
            w.user32.SetWinEventHook(
                w.EVENT_OBJECT_SHOW, w.EVENT_OBJECT_SHOW, None, callback, 0, 0, flags
            ),
        ]
        hooks = [h for h in hooks if h]
        if not hooks:
            log.error("SetWinEventHook failed (error %s)", ctypes.get_last_error())
            return

        log.info("Window watcher active (%d hooks)", len(hooks))
        self._running.set()
        try:
            msg = wintypes.MSG()
            while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hook in hooks:
                w.user32.UnhookWinEvent(hook)
            self._running.clear()
            log.info("Window watcher stopped")

    def _handle_event(
        self,
        _hook: int,
        event: int,
        hwnd: int,
        id_object: int,
        id_child: int,
        _thread: int,
        _time_ms: int,
    ) -> None:
        # A hook callback must never raise: an exception here would be swallowed
        # by ctypes and could destabilise the message loop.
        try:
            if id_object != w.OBJID_WINDOW or id_child != w.CHILDID_SELF or not hwnd:
                return
            info = self._inspect(event, hwnd)
            if info is not None:
                self._on_window(info)
        except Exception:  # noqa: BLE001 - defensive boundary
            log.exception("Error handling window event")

    # -- filtering ---------------------------------------------------------

    def _inspect(self, event: int, hwnd: int) -> WindowEvent | None:
        if not w.user32.IsWindow(hwnd) or not w.user32.IsWindowVisible(hwnd):
            return None
        if not w.is_top_level(hwnd):
            return None

        window_class = w.get_class_name(hwnd)
        if window_class in IGNORED_WINDOW_CLASSES:
            return None

        style, ex_style = w.get_window_styles(hwnd)
        if style & w.WS_CHILD:
            return None
        if ex_style & w.WS_EX_TOOLWINDOW:
            return None

        left, top, right, bottom = w.get_window_rect(hwnd)
        width, height = right - left, bottom - top
        if width <= 1 or height <= 1:
            return None

        pid = w.get_window_pid(hwnd)
        if pid == self._own_pid or pid <= 0:
            return None

        title = w.get_window_text(hwnd)
        is_dialog = window_class == DIALOG_CLASS
        if not title and not is_dialog:
            return None

        if not self._should_report(hwnd):
            return None

        exe_path = w.get_process_path(pid)
        owner = w.get_owner(hwnd)
        is_topmost = bool(ex_style & w.WS_EX_TOPMOST)

        popup_like = bool(
            is_dialog
            or event == w.EVENT_SYSTEM_DIALOGSTART
            or (style & w.WS_POPUP and not style & w.WS_CHILD and (owner or is_topmost or title))
            or (ex_style & w.WS_EX_DLGMODALFRAME)
        )

        screen_w = w.user32.GetSystemMetrics(0)
        screen_h = w.user32.GetSystemMetrics(1)
        fullscreen_ish = bool(screen_w and screen_h and width >= screen_w * 0.98 and height >= screen_h * 0.9)

        body = ""
        if is_dialog or popup_like:
            body = "\n".join(w.get_child_texts(hwnd))

        return WindowEvent(
            hwnd=hwnd,
            pid=pid,
            title=title,
            window_class=window_class,
            exe_path=exe_path,
            exe_name=os.path.basename(exe_path).lower() if exe_path else "",
            event_name=_EVENT_NAMES.get(event, f"event_{event:#x}"),
            is_popup_like=popup_like,
            is_topmost=is_topmost,
            is_fullscreen_ish=fullscreen_ish,
            owner=owner,
            rect=(left, top, right, bottom),
            body=body,
        )

    def _should_report(self, hwnd: int, window_seconds: float = 3.0) -> bool:
        """Collapse the burst of events a single window fires when it opens."""
        now = time.time()
        with self._recent_lock:
            last = self._recent.get(hwnd)
            if last is not None and now - last < window_seconds:
                return False
            self._recent[hwnd] = now
            if len(self._recent) > 512:
                cutoff = now - 60
                for key in [k for k, v in self._recent.items() if v < cutoff]:
                    del self._recent[key]
        return True
