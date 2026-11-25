"""
Windows side of Background Mode: keep the browser on the user's own desktop, but invisible.

There is no private display on Windows. Instead the same headed Chromium runs on the real
desktop (same monitors, GPU, fonts and DPI, so the fingerprint is identical by
construction), and a concealer makes each of its top-level windows invisible from the
moment it is created:

* layered with alpha 0 (nothing is drawn, DWM still composes it, so rendering, vsync and
  page visibility stay exactly as in normal mode),
* click-through (WS_EX_TRANSPARENT), out of the taskbar and Alt+Tab (WS_EX_TOOLWINDOW),
  not activatable by clicks (WS_EX_NOACTIVATE),
* and if it ever becomes the foreground window, the foreground goes straight back to the
  window the user had, so real keystrokes never reach the hidden browser.

Out-of-context WinEvent hooks deliver window creation and foreground changes to a
message-loop thread. Chromium shows its first window before any out-of-process hook can
act, so that window can appear for an instant when the browser starts. Starting it
off-screen would avoid that, but Chromium applies --window-position to every window it
opens (popups would then open in the wrong place), and a window created off-screen gets
no frame insets (the page would see a different inner size), so the flash is the price.

Hidden runs cannot be told apart from visible ones because nothing about the browser or
the desktop changes, only how DWM draws the window; calibration still checks it. Each
hidden start also confirms on the composed screen that the window really is invisible
(some GPU paths could ignore window transparency), and a window that cannot be hidden, or
a foreground that cannot be given back, stops the hidden browser.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import threading
import time
from typing import Any, Callable, Optional

from utils.background_mode.common import SESSION_WINDOWS, SNAPSHOT_VERSION

if sys.platform == "win32":
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None

    WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUT(ctypes.Structure):
        # INPUT with its union reduced to the mouse member, which is the largest one.
        _fields_ = [("type", wt.DWORD), ("mi", _MOUSEINPUT)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, ctypes.c_ubyte, wt.DWORD]
    user32.GetLayeredWindowAttributes.argtypes = [
        wt.HWND, ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wt.DWORD)
    ]
    user32.GetAncestor.restype = wt.HWND
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetShellWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.SetWinEventHook.restype = wt.HANDLE
    user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WinEventProc, wt.DWORD, wt.DWORD, wt.DWORD]
    user32.UnhookWinEvent.argtypes = [wt.HANDLE]
    user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.MsgWaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
    user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.GetDC.restype = wt.HDC
    user32.GetDC.argtypes = [wt.HWND]
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int,
                             ctypes.c_int, wt.DWORD]
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    if dwmapi is not None:
        dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.GetCurrentThreadId.restype = wt.DWORD

GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80
WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x40000, 0x08000000
CONCEAL_BITS = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
# What any concealer (also another provider's) leaves on a window it hides.
CONCEALED_MARK = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
LWA_ALPHA = 0x2
DWMWA_CLOAKED = 14
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW = 0x8000, 0x8002
OBJID_WINDOW = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# The foreground goes back within this long, or the hidden browser is stopped.
FOREGROUND_GRACE_S = 1.5
# A browser window that stays visible this long stops the hidden browser.
CONCEAL_GRACE_S = 1.0
# On-screen check: a colour no desktop shows over a whole window, painted on the blank page.
MARKER_RGB = (255, 0, 254)


# ---------------------------------------------------------------------------- snapshot


def capture_windows_snapshot() -> dict[str, Any]:
    """Monitors (layout, refresh, colour depth, scale) for the calibration signature."""

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD),
                    ("szDevice", ctypes.c_wchar * 32)]

    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD),
                    ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD), ("dmFields", wt.DWORD),
                    ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long), ("dmDisplayOrientation", wt.DWORD),
                    ("dmDisplayFixedOutput", wt.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                    ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                    ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD),
                    ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
                    ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD), ("dmICMIntent", wt.DWORD),
                    ("dmMediaType", wt.DWORD), ("dmDitherType", wt.DWORD), ("dmReserved1", wt.DWORD),
                    ("dmReserved2", wt.DWORD), ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD)]

    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:
        shcore = None
    outputs: list[dict[str, Any]] = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)

    def on_monitor(hmon, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        mode = DEVMODEW()
        mode.dmSize = ctypes.sizeof(mode)
        user32.EnumDisplaySettingsW(info.szDevice, -1, ctypes.byref(mode))  # ENUM_CURRENT_SETTINGS
        dpi_x, dpi_y = wt.UINT(96), wt.UINT(96)
        if shcore is not None:
            shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))  # MDT_EFFECTIVE_DPI
        m, w = info.rcMonitor, info.rcWork
        outputs.append({
            "name": info.szDevice,
            "primary": bool(info.dwFlags & 1),
            "x": m.left, "y": m.top, "width": m.right - m.left, "height": m.bottom - m.top,
            "work": [w.left, w.top, w.right - w.left, w.bottom - w.top],
            "refresh": float(mode.dmDisplayFrequency or 60),
            "bpp": int(mode.dmBitsPerPel or 0),
            "scale": round(dpi_x.value / 96.0, 4),
        })
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(on_monitor), 0)
    outputs.sort(key=lambda o: (o["y"], o["x"]))
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "captured_at": time.time(),
        "session_type": SESSION_WINDOWS,
        "outputs": outputs,
        "desktop": "windows",
    }
    if not outputs:
        snapshot["degraded"] = True
    return snapshot


# ---------------------------------------------------------------------------- concealer


def process_identity(pid: int) -> tuple[int, str]:
    """(creation time, executable path) of a process; (0, "") if it cannot be opened."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0, ""
    try:
        times = [wt.FILETIME() for _ in range(4)]
        created = 0
        if kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        image = buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
        return created, image
    finally:
        kernel32.CloseHandle(handle)


def browser_matcher(executable_path: str, driver_pid: Optional[int]) -> Callable[[int], bool]:
    """
    'Is this process one of our hidden browser's?': the browser executable, started (directly
    or through one level) by this driver's Node process, so parallel providers that run
    visibly are never touched.
    """
    want = os.path.normcase(os.path.abspath(executable_path))
    # Keyed by creation time too: Windows reuses process IDs quickly.
    cache: dict[tuple[int, int], bool] = {}

    def parent_pid(pid: int) -> Optional[int]:
        try:
            import psutil

            return int(psutil.Process(pid).ppid())
        except Exception:
            return None

    def is_ours(pid: int) -> bool:
        created, image = process_identity(pid)
        if not image:
            return False  # Not decided: asked again on the next event or sweep.
        key = (pid, created)
        known = cache.get(key)
        if known is not None:
            return known
        ok = os.path.normcase(os.path.abspath(image)) == want
        if ok and driver_pid:
            parent = parent_pid(pid)
            grandparent = parent_pid(parent) if parent and parent != driver_pid else None
            if parent is None or (parent != driver_pid and grandparent is None):
                return False  # Lookup failed; not cached.
            ok = parent == driver_pid or grandparent == driver_pid
        if len(cache) > 4096:
            cache.clear()
        cache[key] = bool(ok)
        return bool(ok)

    return is_ours


def window_pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def ex_style(hwnd) -> int:
    return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))


def is_concealed(hwnd) -> bool:
    """True if the window carries the concealing styles and is fully transparent."""
    ex = ex_style(hwnd)
    if (ex & CONCEAL_BITS) != CONCEAL_BITS or ex & WS_EX_APPWINDOW:
        return False
    alpha, flags = ctypes.c_ubyte(255), wt.DWORD(0)
    if not user32.GetLayeredWindowAttributes(hwnd, None, ctypes.byref(alpha), ctypes.byref(flags)):
        return False
    return bool(flags.value & LWA_ALPHA) and alpha.value == 0


def window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_cloaked(hwnd) -> bool:
    if dwmapi is None:
        return False
    value = wt.DWORD(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) != 0:
        return False
    return bool(value.value)


def top_level_windows() -> list[int]:
    """All top-level windows, topmost first."""
    found: list[int] = []

    def cb(hwnd, _lparam):
        found.append(int(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


class Concealer(threading.Thread):
    """Message-loop thread that conceals matching windows and guards the foreground."""

    def __init__(self, is_target_pid: Callable[[int], bool], user_foreground: int = 0):
        super().__init__(daemon=True, name="irp-bg-concealer")
        self.is_target_pid = is_target_pid
        self.concealed: set[int] = set()
        self.last_user_foreground = int(user_foreground or user32.GetForegroundWindow() or 0)
        self.stats = {
            "styled": 0,
            "foreground_returns": 0,
            "foreground_returns_after_input": 0,
            "foreground_failures": 0,
        }
        self.ready = threading.Event()
        self.stop_flag = False
        self.error: Optional[str] = None
        self._visible_since: dict[int, float] = {}
        self._foreground_stuck_since: Optional[float] = None
        self._proc = WinEventProc(self._on_event)

    def is_target_window(self, hwnd) -> bool:
        if not hwnd or user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return False
        return int(hwnd) in self.concealed or self.is_target_pid(window_pid(hwnd))

    def conceal(self, hwnd) -> bool:
        ex = ex_style(hwnd)
        want = (ex | CONCEAL_BITS) & ~WS_EX_APPWINDOW
        if ex != want:
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
        if not user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA) and ex & WS_EX_LAYERED:
            # A window drawn with UpdateLayeredWindow only accepts this once it is re-layered.
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want & ~WS_EX_LAYERED)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
            user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
        if not is_concealed(hwnd):
            return False
        key = int(hwnd)
        if key not in self.concealed:
            self.concealed.add(key)
            self.stats["styled"] += 1
        return True

    def visible_unconcealed(self) -> list[int]:
        """Our windows that are visible on screen without being concealed."""
        return [
            hwnd
            for hwnd in top_level_windows()
            if user32.IsWindowVisible(hwnd) and self.is_target_window(hwnd) and not is_concealed(hwnd)
        ]

    def sweep(self) -> None:
        now = time.monotonic()
        self.concealed = {hwnd for hwnd in self.concealed if user32.IsWindow(hwnd)}
        visible = set()
        for hwnd in top_level_windows():
            # Hidden windows are concealed when they show (EVENT_OBJECT_SHOW).
            if not user32.IsWindowVisible(hwnd) or not self.is_target_window(hwnd):
                continue
            if self.conceal(hwnd):
                continue
            visible.add(hwnd)
            since = self._visible_since.setdefault(hwnd, now)
            if now - since > CONCEAL_GRACE_S:
                self.error = "a browser window could not be hidden"
        self._visible_since = {h: t for h, t in self._visible_since.items() if h in visible}

        foreground = user32.GetForegroundWindow()
        if foreground and self.is_target_window(foreground):
            if self.give_foreground_back():
                self._foreground_stuck_since = None
                return
            self._foreground_stuck_since = self._foreground_stuck_since or now
            if now - self._foreground_stuck_since > FOREGROUND_GRACE_S:
                self.error = "the keyboard focus could not be given back to your window"
        else:
            self._foreground_stuck_since = None

    def _is_user_window(self, hwnd) -> bool:
        """A window the foreground can go back to: a normal app window that is not ours."""
        if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return False
        ex = ex_style(hwnd)
        if ex & WS_EX_TOOLWINDOW or (ex & CONCEALED_MARK) == CONCEALED_MARK:
            return False
        return not self.is_target_window(hwnd) and not is_cloaked(hwnd)

    def _foreground_target(self) -> int:
        target = self.last_user_foreground
        if target and user32.IsWindow(target) and (ex_style(target) & CONCEALED_MARK) != CONCEALED_MARK:
            return target
        # The user's window is gone: like Windows itself, activate the next window down.
        for hwnd in top_level_windows():
            if self._is_user_window(hwnd):
                self.last_user_foreground = hwnd
                return hwnd
        return 0

    @staticmethod
    def _lift_foreground_lock() -> None:
        """
        Windows only lets the process that sent the last input change the foreground. Nudge
        the pointer by one pixel and put it straight back: mouse input goes to the window
        under the pointer, never to the click-through browser (a key press would reach the
        hidden page).
        """
        where = wt.POINT()
        if not user32.GetCursorPos(ctypes.byref(where)):
            return
        event = _INPUT(type=0)  # INPUT_MOUSE
        event.mi.dx, event.mi.dwFlags = 1, 0x0001  # MOUSEEVENTF_MOVE
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
        user32.SetCursorPos(where.x, where.y)

    def _foreground_is_ours(self) -> bool:
        current = user32.GetForegroundWindow()
        return bool(current) and self.is_target_window(current)

    def give_foreground_back(self) -> bool:
        """Hand the foreground back; True once it is no longer on a hidden browser window."""
        # With no app window left, the desktop takes it, as when the last window closes.
        target = self._foreground_target() or int(user32.GetShellWindow() or 0)
        if not target:
            return False
        current = user32.GetForegroundWindow()
        our_thread = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(current, None) if current else 0
        attached = bool(fg_thread) and fg_thread != our_thread and user32.AttachThreadInput(our_thread, fg_thread, True)
        try:
            user32.SetForegroundWindow(target)
            if not self._foreground_is_ours():
                self.stats["foreground_returns"] += 1
                return True
            self._lift_foreground_lock()
            user32.SetForegroundWindow(target)
        finally:
            if attached:
                user32.AttachThreadInput(our_thread, fg_thread, False)
        if not self._foreground_is_ours():
            self.stats["foreground_returns_after_input"] += 1
            return True
        self.stats["foreground_failures"] += 1
        return False

    def _on_event(self, _hook, event, hwnd, id_object, _id_child, _thread, _time):
        try:
            if event == EVENT_SYSTEM_FOREGROUND:
                if hwnd and self.is_target_window(hwnd):
                    self.conceal(hwnd)
                    if int(user32.GetForegroundWindow() or 0) == int(hwnd):  # (Not if the user moved on.)
                        self.give_foreground_back()
                elif hwnd and (ex_style(hwnd) & CONCEALED_MARK) != CONCEALED_MARK:
                    # (Never another provider's hidden window.)
                    self.last_user_foreground = int(hwnd)
            elif id_object == OBJID_WINDOW and self.is_target_window(hwnd):
                self.conceal(hwnd)
        except Exception:
            pass

    def run(self) -> None:
        hooks = [
            user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._proc, 0, 0, 0),
            user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, None, self._proc, 0, 0, 0),
        ]
        try:
            if not all(hooks):
                self.error = f"window event hooks could not be set (error {ctypes.get_last_error()})"
                return
            self.ready.set()
            msg = wt.MSG()
            last_sweep = 0.0
            while not self.stop_flag:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                now = time.monotonic()
                if now - last_sweep > 0.25:
                    try:
                        self.sweep()
                    except Exception as exc:
                        self.error = self.error or f"the window concealer failed ({exc})"
                    last_sweep = now
                user32.MsgWaitForMultipleObjects(0, None, False, 20, 0x04FF)  # QS_ALLINPUT
        finally:
            for hook in hooks:
                if hook:
                    user32.UnhookWinEvent(hook)
            self.ready.set()


def _with_dpi_awareness(fn: Callable[[], Any]) -> Any:
    """Run fn with physical-pixel coordinates (per-monitor DPI awareness) on this thread."""
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous = None
    if setter is not None:
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p]
        previous = setter(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        return fn()
    finally:
        if setter is not None and previous:
            setter(ctypes.c_void_p(previous))


def marker_share_on_screen(hwnd: int) -> Optional[float]:
    """
    Share of the window that shows MARKER_RGB in the composed desktop image, or None if
    that cannot be measured (capture failed, window mostly off screen).
    """

    def capture() -> Optional[float]:
        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        # Only the part on the virtual screen can be captured.
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # SM_X/YVIRTUALSCREEN
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)  # SM_CX/CYVIRTUALSCREEN
        left, top = max(rect.left, vx), max(rect.top, vy)
        width, height = min(rect.right, vx + vw) - left, min(rect.bottom, vy + vh) - top
        full = (rect.right - rect.left) * (rect.bottom - rect.top)
        if width <= 0 or height <= 0 or full <= 0 or width * height < full / 2:
            return None
        screen = user32.GetDC(None)
        if not screen:
            return None
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        old = gdi32.SelectObject(memory, bitmap) if memory and bitmap else None
        try:
            if old is None:
                return None
            # CAPTUREBLT includes layered windows, i.e. what is actually on screen.
            if not gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top, 0x00CC0020 | 0x40000000):
                return None
            header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                       biPlanes=1, biBitCount=32, biCompression=0)
            pixels = (ctypes.c_ubyte * (width * height * 4))()
            gdi32.SelectObject(memory, old)
            old = None
            if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
                return None
        finally:
            if old is not None:
                gdi32.SelectObject(memory, old)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory:
                gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)
        data = bytes(pixels)
        r, g, b = MARKER_RGB
        hits = total = 0
        for y in range(0, height, max(1, height // 64)):  # A 64x64 grid is plenty.
            row = y * width * 4
            for x in range(0, width, max(1, width // 64)):
                i = row + x * 4  # BGRA
                total += 1
                if abs(data[i + 2] - r) < 16 and abs(data[i + 1] - g) < 16 and abs(data[i] - b) < 16:
                    hits += 1
        return hits / total if total else 0.0

    return _with_dpi_awareness(capture)


# ---------------------------------------------------------------------------- app-side adapter

MARKER_JS = """async (rgb) => {
    let el = document.getElementById('__irp_bg_marker');
    if (!rgb) {
        if (el) el.remove();
        return true;
    }
    if (!el) {
        el = document.createElement('div');
        el.id = '__irp_bg_marker';
        document.documentElement.appendChild(el);
    }
    el.style.cssText = `position:fixed;inset:0;z-index:2147483647;background:rgb(${rgb.join(',')})`;
    // True once four frames were drawn with the marker, false if frames are not ticking.
    return await new Promise((resolve) => {
        let n = 0;
        const step = () => { if (++n >= 4) resolve(true); else requestAnimationFrame(step); };
        requestAnimationFrame(step);
        setTimeout(() => resolve(false), 1500);
    });
}"""


class WindowsDisplay:
    """
    The Windows counterpart of client.PrivateDisplay: same interface, no private display.

    The browser stays on the user's desktop (default environment); its windows are
    concealed from creation by a Concealer thread.
    """

    session_type = SESSION_WINDOWS
    snapshot_source = "live"
    description = "your desktop, with its windows concealed"
    private_clipboard = False  # Same desktop: the normal clipboard path is already correct.

    def __init__(self, snapshot: dict[str, Any], executable_path: str, driver_pid: Optional[int]):
        self.snapshot = snapshot
        self.info = {"snapshot": snapshot}
        self.concealer = Concealer(browser_matcher(executable_path, driver_pid))
        self._styled_at_launch = 0

    def start(self) -> None:
        self.concealer.start()
        if not self.concealer.ready.wait(5) or self.concealer.error:
            error = self.concealer.error or "the window concealer did not start"
            self.stop()
            raise RuntimeError(error)

    def alive(self) -> bool:
        return self.concealer.is_alive() and self.concealer.error is None

    @property
    def dead_reason(self) -> Optional[str]:
        if self.concealer.error:
            return self.concealer.error
        return None if self.concealer.is_alive() else "the window concealer stopped"

    def browser_env(self) -> None:
        return None

    def mark_launch(self) -> None:
        self._styled_at_launch = self.concealer.stats["styled"]

    def hint_maximized_window(self, user_data_dir: str) -> None:
        return None

    async def settle(self, timeout_s: float = 4.0) -> dict[str, Any]:
        """Wait until the new browser window exists and is concealed."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.alive():
                return {"ok": False, "error": self.dead_reason}
            if self.concealer.stats["styled"] > self._styled_at_launch:
                # Every visible browser window must be concealed by now; windows that only just
                # appeared get the concealer's usual grace period.
                grace = time.monotonic() + CONCEAL_GRACE_S
                while True:
                    await asyncio.sleep(0.2)
                    visible = self.concealer.visible_unconcealed()
                    if not visible:
                        return {"ok": True}
                    if time.monotonic() > grace or not self.alive():
                        names = ", ".join(sorted({window_class(hwnd) for hwnd in visible}))
                        return {"ok": False, "error": f"a browser window stayed visible ({names})"}
            await asyncio.sleep(0.05)
        return {"ok": False, "error": "the browser window was not seen"}

    async def verify_on_screen(self, page: Any) -> Optional[str]:
        """
        Check the composed screen, not just the window styles: paint the still blank page
        in a marker colour, raise the window to the top without activating it, and look for
        the colour where the window is. None if nothing shows.
        """
        windows = [hwnd for hwnd in top_level_windows() if hwnd in self.concealer.concealed and user32.IsWindowVisible(hwnd)]
        if not windows:
            return "the browser window was not found"
        loop = asyncio.get_running_loop()
        shares: list[Optional[float]] = []
        try:
            painted = await asyncio.wait_for(page.evaluate(MARKER_JS, list(MARKER_RGB)), 5.0)
            if not painted:
                return "could not check that the browser window is hidden (the page is not drawing frames)"
            for hwnd in windows:
                # Above the user's windows (it is click-through and, if all is well, invisible),
                # so nothing can cover what is being checked.
                flags = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # HWND_TOPMOST
                await asyncio.sleep(0.15)
                try:
                    shares.append(await asyncio.wait_for(loop.run_in_executor(None, marker_share_on_screen, hwnd), 5.0))
                finally:
                    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
        finally:
            try:
                await asyncio.wait_for(page.evaluate(MARKER_JS, None), 5.0)
            except Exception:
                pass
        if any(share is None for share in shares):
            return "could not check that the browser window is hidden (the screen could not be captured)"
        worst = max(share for share in shares if share is not None)
        if worst > 0.2:
            return f"the browser window is visible on screen ({worst:.0%} of it), so this GPU or driver ignores window transparency"
        return None

    async def set_clipboard_text(self, text: str, timeout_s: float = 6.0) -> bool:
        return False

    def stop(self) -> None:
        # Match nothing from now on, so a visible relaunch can never be concealed even if
        # the thread takes a moment to unhook.
        self.concealer.is_target_pid = lambda _pid: False
        self.concealer.stop_flag = True
        self.concealer.join(2)
        self.concealer.concealed = set()


def driver_process_id(playwright: Any) -> Optional[int]:
    """PID of Patchright's Node driver (its children are this driver's browsers)."""
    try:
        return int(playwright._impl_obj._connection._transport._proc.pid)
    except Exception:
        return None


def open_windows_display(playwright: Any) -> WindowsDisplay:
    snapshot = capture_windows_snapshot()
    if snapshot.get("degraded"):
        raise RuntimeError("no monitor was found")
    driver_pid = driver_process_id(playwright)
    if driver_pid is None:
        # Without it, the browser executable alone would also match other providers'
        # visible browsers (for example one waiting for a sign-in).
        raise RuntimeError("the browser driver process was not found")
    display = WindowsDisplay(snapshot, playwright.chromium.executable_path, driver_pid)
    display.start()
    return display
