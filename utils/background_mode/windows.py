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
the desktop changes, only how DWM draws the window; calibration still checks it.
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

    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, ctypes.c_byte, wt.DWORD]
    user32.GetAncestor.restype = wt.HWND
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.SetWinEventHook.restype = wt.HANDLE
    user32.IsWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x80000, 0x20, 0x80
WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x40000, 0x08000000
CONCEAL_BITS = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW = 0x8000, 0x8002
OBJID_WINDOW = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


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


def process_image(pid: int) -> str:
    """Executable path of a process (a native call: this runs on every new window)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def browser_matcher(executable_path: str, driver_pid: Optional[int]) -> Callable[[int], bool]:
    """
    'Is this process one of our hidden browser's?': the browser executable, started (directly
    or through one level) by this driver's Node process, so parallel providers that run
    visibly are never touched.
    """
    want = os.path.normcase(os.path.abspath(executable_path))
    cache: dict[int, bool] = {}

    def parent_pid(pid: int) -> int:
        try:
            import psutil

            return int(psutil.Process(pid).ppid())
        except Exception:
            return 0

    def is_ours(pid: int) -> bool:
        if pid not in cache:
            image = process_image(pid)
            ok = bool(image) and os.path.normcase(os.path.abspath(image)) == want
            if ok and driver_pid:
                parent = parent_pid(pid)
                ok = parent == driver_pid or (parent and parent_pid(parent) == driver_pid)
            cache[pid] = bool(ok)
        return cache[pid]

    return is_ours


def window_pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


class Concealer(threading.Thread):
    """Message-loop thread that conceals matching windows and guards the foreground."""

    def __init__(self, is_target_pid: Callable[[int], bool], user_foreground: int = 0):
        super().__init__(daemon=True, name="irp-bg-concealer")
        self.is_target_pid = is_target_pid
        self.concealed: set[int] = set()
        self.last_user_foreground = int(user_foreground or user32.GetForegroundWindow() or 0)
        self.stats = {"styled": 0, "foreground_returns": 0, "foreground_failures": 0}
        self.ready = threading.Event()
        self.stop_flag = False
        self._proc = WinEventProc(self._on_event)

    def _is_target_window(self, hwnd) -> bool:
        if not hwnd or user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return False
        return self.is_target_pid(window_pid(hwnd))

    def conceal(self, hwnd) -> None:
        key = int(hwnd)
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        want = (ex | CONCEAL_BITS) & ~WS_EX_APPWINDOW
        if ex != want:
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
        user32.SetLayeredWindowAttributes(hwnd, 0, 0, 2)  # LWA_ALPHA: fully transparent
        if key not in self.concealed:
            self.concealed.add(key)
            self.stats["styled"] += 1

    def sweep(self) -> None:
        found = []

        def cb(hwnd, _l):
            if self._is_target_window(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        for hwnd in found:
            self.conceal(hwnd)

    def give_foreground_back(self) -> bool:
        target = self.last_user_foreground
        if not target or not user32.IsWindow(target):
            return False
        current = user32.GetForegroundWindow()
        our_thread = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(current, None) if current else 0
        attached = bool(fg_thread) and fg_thread != our_thread and user32.AttachThreadInput(our_thread, fg_thread, True)
        try:
            user32.SetForegroundWindow(target)
            if int(user32.GetForegroundWindow() or 0) != int(target):
                # Foreground lock: an input event from this process lifts it. F24 is on no
                # common keyboard and bound by no common app (unlike Alt, which opens menus).
                user32.keybd_event(0x87, 0, 0, 0)
                user32.keybd_event(0x87, 0, 2, 0)
                user32.SetForegroundWindow(target)
        finally:
            if attached:
                user32.AttachThreadInput(our_thread, fg_thread, False)
        return int(user32.GetForegroundWindow() or 0) == int(target)

    def _on_event(self, _hook, event, hwnd, id_object, _id_child, _thread, _time):
        try:
            if event == EVENT_SYSTEM_FOREGROUND:
                if hwnd and (int(hwnd) in self.concealed or self._is_target_window(hwnd)):
                    self.conceal(hwnd)
                    if self.give_foreground_back():
                        self.stats["foreground_returns"] += 1
                    else:
                        self.stats["foreground_failures"] += 1
                elif hwnd:
                    self.last_user_foreground = int(hwnd)
            elif id_object == OBJID_WINDOW and self._is_target_window(hwnd):
                self.conceal(hwnd)
        except Exception:
            pass

    def run(self) -> None:
        hooks = [
            user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._proc, 0, 0, 0),
            user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, None, self._proc, 0, 0, 0),
        ]
        self.ready.set()
        msg = wt.MSG()
        last_sweep = 0.0
        while not self.stop_flag:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            now = time.monotonic()
            if now - last_sweep > 0.25:
                self.sweep()
                last_sweep = now
            user32.MsgWaitForMultipleObjects(0, None, False, 20, 0x04FF)  # QS_ALLINPUT
        for hook in hooks:
            if hook:
                user32.UnhookWinEvent(hook)


# ---------------------------------------------------------------------------- app-side adapter


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

    def start(self) -> None:
        self.concealer.start()
        if not self.concealer.ready.wait(5):
            raise RuntimeError("the window concealer did not start")

    def alive(self) -> bool:
        return self.concealer.is_alive()

    def browser_env(self) -> None:
        return None

    def mark_launch(self) -> None:
        self._styled_at_launch = self.concealer.stats["styled"]

    def hint_maximized_window(self, user_data_dir: str) -> None:
        return None

    async def settle(self, timeout_s: float = 4.0) -> dict[str, Any]:
        """Wait until the new browser window exists and is concealed."""
        start = getattr(self, "_styled_at_launch", 0)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.concealer.stats["styled"] > start:
                await asyncio.sleep(0.2)
                return {"ok": True}
            await asyncio.sleep(0.05)
        return {"ok": False, "error": "the browser window was not seen"}

    async def set_clipboard_text(self, text: str, timeout_s: float = 6.0) -> bool:
        return False

    def stop(self) -> None:
        # Match nothing from now on, so a visible relaunch can never be concealed even if
        # the thread takes a moment to unhook.
        self.concealer.is_target_pid = lambda _pid: False
        self.concealer.stop_flag = True
        self.concealer.join(2)


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
