"""X11 side of Background Mode: read the desktop and mirror it onto the private Xwayland.

Everything here runs in the supervisor process (Xlib through ctypes can exit() its
host process on fatal display errors).
"""

from __future__ import annotations

import base64
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

from utils.background_mode.common import SESSION_X11, SNAPSHOT_VERSION, system_env
from utils.background_mode.ipc import log as _log
from utils.background_mode.sway import SwayIPC, iter_views


class Xlib:
    """Just enough Xlib (via ctypes) to read and write properties and own a selection."""

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        self.ct = ctypes
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        lib = ctypes.CDLL(name)
        c_ulong, c_int, c_void_p, c_char_p, c_long = (
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
        )
        lib.XInitThreads.restype = c_int
        lib.XOpenDisplay.restype = c_void_p
        lib.XOpenDisplay.argtypes = [c_char_p]
        lib.XCloseDisplay.argtypes = [c_void_p]
        lib.XDefaultRootWindow.restype = c_ulong
        lib.XDefaultRootWindow.argtypes = [c_void_p]
        lib.XDisplayWidth.argtypes = [c_void_p, c_int]
        lib.XDisplayHeight.argtypes = [c_void_p, c_int]
        lib.XInternAtom.restype = c_ulong
        lib.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        lib.XGetSelectionOwner.restype = c_ulong
        lib.XGetSelectionOwner.argtypes = [c_void_p, c_ulong]
        lib.XSetSelectionOwner.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong]
        lib.XGetWindowProperty.restype = c_int
        lib.XGetWindowProperty.argtypes = [
            c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
            ctypes.POINTER(c_ulong), ctypes.POINTER(c_int), ctypes.POINTER(c_ulong),
            ctypes.POINTER(c_ulong), ctypes.POINTER(c_void_p),
        ]
        lib.XChangeProperty.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int, c_void_p, c_int]
        lib.XFree.argtypes = [c_void_p]
        lib.XSync.argtypes = [c_void_p, c_int]
        lib.XCreateSimpleWindow.restype = c_ulong
        lib.XCreateSimpleWindow.argtypes = [
            c_void_p, c_ulong, c_int, c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, c_ulong, c_ulong,
        ]

        class XkbState(ctypes.Structure):
            _fields_ = [
                ("group", ctypes.c_ubyte), ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort), ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte), ("base_mods", ctypes.c_ubyte), ("latched_mods", ctypes.c_ubyte),
                ("locked_mods", ctypes.c_ubyte), ("compat_state", ctypes.c_ubyte), ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte), ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte), ("ptr_buttons", ctypes.c_ushort),
            ]

        self.XkbState = XkbState
        lib.XkbGetState.argtypes = [c_void_p, ctypes.c_uint, ctypes.POINTER(XkbState)]
        lib.XkbLockGroup.argtypes = [c_void_p, ctypes.c_uint, ctypes.c_uint]
        self.lib = lib
        lib.XInitThreads()
        # A window can vanish between listing it and reading its properties. The default
        # Xlib error handler would exit() the supervisor for that, so ignore protocol errors.
        handler_type = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
        self._error_handler = handler_type(lambda _d, _e: 0)
        lib.XSetErrorHandler(self._error_handler)

    def open(self, name: str):
        handle = self.lib.XOpenDisplay(name.encode())
        return handle or None

    def close(self, handle) -> None:
        if handle:
            self.lib.XCloseDisplay(handle)

    def atom(self, handle, name: str) -> int:
        return int(self.lib.XInternAtom(handle, name.encode(), 0))

    def root(self, handle) -> int:
        return int(self.lib.XDefaultRootWindow(handle))

    def get_property(self, handle, window: int, name: str):
        ct = self.ct
        actual_type, fmt, nitems, after, data = (
            ct.c_ulong(), ct.c_int(), ct.c_ulong(), ct.c_ulong(), ct.c_void_p(),
        )
        status = self.lib.XGetWindowProperty(
            handle, window, self.atom(handle, name), 0, 1 << 22, 0, 0,
            ct.byref(actual_type), ct.byref(fmt), ct.byref(nitems), ct.byref(after), ct.byref(data),
        )
        if status != 0 or not data.value:
            return None
        count = int(nitems.value)
        try:
            if fmt.value == 8:
                return ct.string_at(data.value, count)
            if fmt.value == 16:
                return list((ct.c_short * count).from_address(data.value))
            # Format 32 is returned as C longs.
            return [int(v) for v in (ct.c_long * count).from_address(data.value)]
        finally:
            self.lib.XFree(data)

    XKB_USE_CORE_KBD = 0x0100

    def keyboard_group(self, handle) -> Optional[int]:
        """The active keyboard layout (XKB group) for users with several layouts."""
        state = self.XkbState()
        if self.lib.XkbGetState(handle, self.XKB_USE_CORE_KBD, self.ct.byref(state)) != 0:
            return None
        return int(state.group)

    def set_property(self, handle, window: int, name: str, type_atom: int, fmt: int, values) -> None:
        ct = self.ct
        if fmt == 8:
            raw = bytes(values)
            self.lib.XChangeProperty(handle, window, self.atom(handle, name), type_atom, 8, 0, raw, len(raw))
        else:
            arr = (ct.c_long * len(values))(*values)
            self.lib.XChangeProperty(handle, window, self.atom(handle, name), type_atom, 32, 0, arr, len(values))


XA_ATOM = 4
XA_CARDINAL = 6
XA_STRING = 31


_XRANDR_OUTPUT_RE = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")


_XRANDR_MODE_RE = re.compile(r"^\s+\S+\s+\(0x[0-9a-fA-F]+\)\s+([\d.]+)MHz(.*)\*current")
_XRANDR_TIMING_RE = re.compile(
    r"^\s+([hv]):\s+(?:width|height)\s+(\d+)\s+start\s+(\d+)\s+end\s+(\d+)\s+total\s+(\d+)"
)
_MODE_FLAG_BITS = {"+HSync": 1, "-HSync": 2, "+VSync": 4, "-VSync": 8, "Interlace": 16, "DoubleScan": 32}


def query_outputs(display: str) -> list[dict[str, Any]]:
    """
    Active outputs with their current X11 mode line and exact refresh rate.

    The exact rate matters: Chromium paces requestAnimationFrame from the X11 mode line,
    and pages can measure it.
    """
    xrandr = shutil.which("xrandr")
    if not xrandr:
        return []
    try:
        text = subprocess.run(
            [xrandr, "--verbose", "--current"],
            env={**system_env(), "DISPLAY": display},
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return []
    outputs: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    mode: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        match = _XRANDR_OUTPUT_RE.match(line)
        if match:
            current = {
                "name": match.group(1),
                "primary": bool(match.group(2)),
                "width": int(match.group(3)),
                "height": int(match.group(4)),
                "x": int(match.group(5)),
                "y": int(match.group(6)),
                "refresh": None,
                "modeline": None,
            }
            outputs.append(current)
            mode = None
            continue
        if line and not line[0].isspace():
            current = None
            mode = None
            continue
        if current is None:
            continue
        mode_match = _XRANDR_MODE_RE.match(line)
        if mode_match:
            flags = 0
            for token in mode_match.group(2).split():
                flags |= _MODE_FLAG_BITS.get(token, 0)
            mode = {"clock_khz": int(round(float(mode_match.group(1)) * 1000)), "flags": flags}
            continue
        if mode is not None:
            timing = _XRANDR_TIMING_RE.match(line)
            if timing:
                mode[timing.group(1)] = [int(timing.group(i)) for i in range(2, 6)]
                if "h" in mode and "v" in mode and current["refresh"] is None:
                    htotal, vtotal = mode["h"][3], mode["v"][3]
                    if htotal > 0 and vtotal > 0:
                        current["refresh"] = mode["clock_khz"] * 1000.0 / (htotal * vtotal)
                        current["modeline"] = mode
                    mode = None
    for output in outputs:
        if not output["refresh"]:
            output["refresh"] = 60.0
    return outputs



def capture_x11_snapshot(x: Xlib, display: str) -> dict[str, Any]:
    handle = x.open(display)
    if not handle:
        raise RuntimeError(f"cannot open the desktop display {display}")
    try:
        root = x.root(handle)
        snapshot: dict[str, Any] = {
            "version": SNAPSHOT_VERSION,
            "captured_at": time.time(),
            "session_type": SESSION_X11,
            "screen": [int(x.lib.XDisplayWidth(handle, 0)), int(x.lib.XDisplayHeight(handle, 0))],
        }
        outputs = query_outputs(display)
        if not outputs:
            # No active monitor right now (screen switched off, laptop lid closed, xrandr
            # missing). Mirroring this would not match the desktop pages normally see.
            snapshot["degraded"] = True
            outputs = [{
                "name": "default", "primary": True, "x": 0, "y": 0,
                "width": snapshot["screen"][0], "height": snapshot["screen"][1], "refresh": 60.0,
                "modeline": None,
            }]
        snapshot["outputs"] = outputs

        resources = x.get_property(handle, root, "RESOURCE_MANAGER")
        snapshot["resource_manager"] = resources.decode("utf-8", "replace") if resources else ""

        owner = int(x.lib.XGetSelectionOwner(handle, x.atom(handle, "_XSETTINGS_S0")))
        blob = x.get_property(handle, owner, "_XSETTINGS_SETTINGS") if owner else None
        snapshot["xsettings_b64"] = base64.b64encode(blob).decode() if blob else ""

        workarea = x.get_property(handle, root, "_NET_WORKAREA")
        current = x.get_property(handle, root, "_NET_CURRENT_DESKTOP")
        index = int(current[0]) if current else 0
        if workarea and len(workarea) >= 4 * (index + 1):
            snapshot["workarea"] = [int(v) for v in workarea[4 * index:4 * index + 4]]
        elif workarea and len(workarea) >= 4:
            snapshot["workarea"] = [int(v) for v in workarea[:4]]
        else:
            snapshot["workarea"] = [0, 0, snapshot["screen"][0], snapshot["screen"][1]]

        rules = x.get_property(handle, root, "_XKB_RULES_NAMES")
        parts = rules.split(b"\0") if rules else []
        keys = ("rules", "model", "layout", "variant", "options")
        snapshot["xkb"] = {k: (parts[i].decode("utf-8", "replace") if i < len(parts) else "") for i, k in enumerate(keys)}
        snapshot["xkb"]["group"] = x.keyboard_group(handle) or 0

        wm_name = ""
        check = x.get_property(handle, root, "_NET_SUPPORTING_WM_CHECK")
        if check:
            name = x.get_property(handle, int(check[0]), "_NET_WM_NAME")
            wm_name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
        snapshot["wm_name"] = wm_name
        return snapshot
    finally:
        x.close(handle)


class Placer(threading.Thread):
    """
    Plays the user's window manager for Chromium windows on the private display.

    Sway centres floating windows and re-centres them on every resize request. An X11
    window manager like xfwm4 instead keeps the position the client asked for (Chromium
    puts it in WM_NORMAL_HINTS) and only pushes the window back inside the work area.
    Calibrated against xfwm4 with saved positions, oversize windows, off-screen requests
    and maximized restores.
    """

    def __init__(self, x: Xlib, x_display: str, swaysock: str, workarea: list[int]):
        super().__init__(name="irp-bg-placer", daemon=True)
        self.x = x
        self.x_display = x_display
        self.swaysock = swaysock
        self.workarea = workarea
        self.stop_event = threading.Event()
        self.ready = threading.Event()  # Set once connected; the supervisor waits for it.
        self.cond = threading.Condition()
        self.passes = 0
        self.last_moves = 0
        self.failed: Optional[str] = None
        # Pending "this profile's first window restores maximized" hints, consumed once.
        self.hints_lock = threading.Lock()
        self.maximize_hints: list[str] = []
        # Per-window maximized decision, made when the window is first seen.
        self.sticky_maximized: dict[int, bool] = {}

    def add_maximize_hint(self, class_contains: str) -> None:
        if class_contains:
            with self.hints_lock:
                self.maximize_hints.append(class_contains)

    def _initially_maximized(self, handle, window: int, request: dict[str, Any]) -> bool:
        if request.get("maximized"):
            return True
        wm_class = self.x.get_property(handle, window, "WM_CLASS") if window else None
        text = wm_class.decode("utf-8", "replace") if isinstance(wm_class, bytes) else ""
        with self.hints_lock:
            for index, needle in enumerate(self.maximize_hints):
                if needle in text:
                    del self.maximize_hints[index]
                    return True
        return False

    def _request_for(self, handle, window: int) -> dict[str, Any]:
        hints = self.x.get_property(handle, window, "WM_NORMAL_HINTS") if window else None
        request: dict[str, Any] = {"has_position": False, "x": 0, "y": 0, "maximized": False}
        if hints and len(hints) >= 3:
            flags = int(hints[0])
            # USPosition = 1, PPosition = 4
            if flags & (1 | 4):
                request.update(has_position=True, x=int(hints[1]), y=int(hints[2]))
        state = self.x.get_property(handle, window, "_NET_WM_STATE") if window else None
        if state:
            atoms = {int(a) for a in state}
            vert = self.x.atom(handle, "_NET_WM_STATE_MAXIMIZED_VERT")
            horz = self.x.atom(handle, "_NET_WM_STATE_MAXIMIZED_HORZ")
            request["maximized"] = vert in atoms and horz in atoms
        return request

    def _compute_target(self, request: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
        wx, wy, ww, wh = self.workarea
        if request.get("maximized"):
            return wx, wy, ww, wh
        if request.get("has_position"):
            rx, ry = int(request["x"]), int(request["y"])
        else:
            rx, ry = wx + 10, wy + 10
        x = max(wx, min(rx, wx + ww - width))
        y = max(wy, min(ry, wy + wh - height))
        return x, y, width, height

    def _pass(self, ipc: SwayIPC, handle) -> int:
        moves = 0
        seen: set[int] = set()
        for view in iter_views(ipc.tree()):
            if view.get("shell") != "xwayland":
                continue
            rect = view.get("rect") or {}
            width, height = int(rect.get("width") or 0), int(rect.get("height") or 0)
            if width <= 0 or height <= 0:
                continue
            window = int(view.get("window") or 0)
            con_id = int(view["id"])
            seen.add(con_id)
            request = self._request_for(handle, window)
            if con_id not in self.sticky_maximized:
                # Chromium restores a maximized window via _NET_WM_STATE, which sway clears
                # for floating windows. xfwm4 keeps such a window at the work area size even
                # when Playwright later resizes it, so remember the decision per window.
                self.sticky_maximized[con_id] = self._initially_maximized(handle, window, request)
            request["maximized"] = self.sticky_maximized[con_id]
            x, y, w, h = self._compute_target(request, width, height)
            commands = []
            if (w, h) != (width, height):
                commands.append(f"resize set width {w} px height {h} px")
            if (x, y) != (int(rect.get("x") or 0), int(rect.get("y") or 0)):
                commands.append(f"move absolute position {x} {y}")
            if commands:
                ipc.command(f"[con_id={con_id}] " + ", ".join(commands))
                moves += 1
        for gone in set(self.sticky_maximized) - seen:
            del self.sticky_maximized[gone]
        return moves

    def run(self) -> None:
        handle = self.x.open(self.x_display)
        ipc: Optional[SwayIPC] = None
        try:
            if not handle:
                raise RuntimeError("could not open the private X display")
            ipc = SwayIPC(self.swaysock)
            self.ready.set()
            while not self.stop_event.is_set():
                moves = self._pass(ipc, handle)
                with self.cond:
                    self.passes += 1
                    self.last_moves = moves
                    self.cond.notify_all()
                self.stop_event.wait(0.05 if moves else 0.15)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.failed = str(exc)
                _log(f"window placement stopped: {exc}", "warning")
        finally:
            with self.cond:
                self.cond.notify_all()
            if ipc is not None:
                ipc.close()
            self.x.close(handle)

    def settle(self, timeout_s: float) -> dict[str, Any]:
        """Wait for two consecutive passes that moved nothing (the second one confirms)."""
        deadline = time.monotonic() + max(0.5, timeout_s)
        with self.cond:
            start = self.passes
            clean = 0
            seen = start
            while time.monotonic() < deadline and self.failed is None:
                self.cond.wait(timeout=0.25)
                if self.passes != seen:
                    seen = self.passes
                    clean = clean + 1 if self.last_moves == 0 else 0
                    if clean >= 2 and seen - start >= 2:
                        return {"ok": True}
        return {"ok": False, "error": self.failed or "windows did not settle"}


class ClipboardOwner(threading.Thread):
    """
    Owns the CLIPBOARD selection on the private display and serves text to Chromium.

    In visible mode a provider driver may paste through the desktop clipboard. In
    Background Mode the browser has its own X server and clipboard, so the supervisor
    plays the desktop clipboard there. No browser permission grant is needed, and the
    user's real clipboard is never touched.
    """

    SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY = 29, 30, 31

    def __init__(self, x: Xlib, x_display: str):
        super().__init__(name="irp-bg-clipboard", daemon=True)
        import ctypes

        self.ct = ctypes
        self.x = x
        self.x_display = x_display
        self.commands: "queue.Queue[tuple[str, Any, threading.Event, dict]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.ready = threading.Event()  # Set once it owns its window; the supervisor waits for it.
        self.wake_r, self.wake_w = os.pipe()
        lib = x.lib
        lib.XPending.argtypes = [ctypes.c_void_p]
        lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
        lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        lib.XExtendedMaxRequestSize.restype = ctypes.c_long
        lib.XExtendedMaxRequestSize.argtypes = [ctypes.c_void_p]
        lib.XMaxRequestSize.restype = ctypes.c_long
        lib.XMaxRequestSize.argtypes = [ctypes.c_void_p]

        c_int, c_ulong, c_void_p = ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p

        class SelectionRequest(ctypes.Structure):
            _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                        ("owner", c_ulong), ("requestor", c_ulong), ("selection", c_ulong), ("target", c_ulong),
                        ("property", c_ulong), ("time", c_ulong)]

        class SelectionNotify(ctypes.Structure):
            _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                        ("requestor", c_ulong), ("selection", c_ulong), ("target", c_ulong), ("property", c_ulong),
                        ("time", c_ulong)]

        class Event(ctypes.Union):
            _fields_ = [("type", c_int), ("request", SelectionRequest), ("notify", SelectionNotify),
                        ("pad", ctypes.c_long * 24)]

        self.Event = Event
        self.text: Optional[bytes] = None

    def set_text(self, text: str, timeout_s: float = 5.0) -> dict[str, Any]:
        done, result = threading.Event(), {}
        self.commands.put(("set", text, done, result))
        os.write(self.wake_w, b"x")
        done.wait(timeout_s)
        return result or {"ok": False, "error": "clipboard owner did not answer"}

    def _handle_request(self, handle, owner: int, ev) -> None:
        x, lib = self.x, self.x.lib
        req = ev.request
        reply = self.Event()
        reply.notify.type = self.SELECTION_NOTIFY
        reply.notify.requestor = req.requestor
        reply.notify.selection = req.selection
        reply.notify.target = req.target
        reply.notify.time = req.time
        prop = req.property or req.target
        reply.notify.property = 0
        if self.text is not None and req.owner == owner:
            utf8 = x.atom(handle, "UTF8_STRING")
            text_types = {
                x.atom(handle, "UTF8_STRING"): (utf8, self.text),
                x.atom(handle, "text/plain;charset=utf-8"): (x.atom(handle, "text/plain;charset=utf-8"), self.text),
                x.atom(handle, "STRING"): (XA_STRING, self.text.decode("utf-8", "replace").encode("latin-1", "replace")),
                x.atom(handle, "TEXT"): (XA_STRING, self.text.decode("utf-8", "replace").encode("latin-1", "replace")),
                x.atom(handle, "text/plain"): (x.atom(handle, "text/plain"), self.text.decode("utf-8", "replace").encode("latin-1", "replace")),
            }
            if req.target == x.atom(handle, "TARGETS"):
                atoms = [x.atom(handle, "TARGETS"), x.atom(handle, "TIMESTAMP")] + list(text_types)
                arr = (self.ct.c_long * len(atoms))(*atoms)
                lib.XChangeProperty(handle, req.requestor, prop, XA_ATOM, 32, 0, arr, len(atoms))
                reply.notify.property = prop
            elif req.target == x.atom(handle, "TIMESTAMP"):
                arr = (self.ct.c_long * 1)(0)
                lib.XChangeProperty(handle, req.requestor, prop, x.atom(handle, "INTEGER"), 32, 0, arr, 1)
                reply.notify.property = prop
            elif req.target in text_types:
                type_atom, payload = text_types[req.target]
                lib.XChangeProperty(handle, req.requestor, prop, type_atom, 8, 0, payload, len(payload))
                reply.notify.property = prop
        lib.XSendEvent(handle, req.requestor, 0, 0, self.ct.byref(reply))
        lib.XFlush(handle)

    def run(self) -> None:
        import select

        x, lib = self.x, self.x.lib
        handle = x.open(self.x_display)
        if not handle:
            return
        owner = int(lib.XCreateSimpleWindow(handle, x.root(handle), -10, -10, 1, 1, 0, 0, 0))
        clipboard = x.atom(handle, "CLIPBOARD")
        max_bytes = int(lib.XExtendedMaxRequestSize(handle) or lib.XMaxRequestSize(handle)) * 4 - 4096
        fd = int(lib.XConnectionNumber(handle))
        ev = self.Event()
        self.ready.set()
        try:
            while not self.stop_event.is_set():
                while not self.commands.empty():
                    cmd, value, done, result = self.commands.get_nowait()
                    if cmd == "set":
                        data = str(value or "").encode("utf-8")
                        if len(data) > max_bytes:
                            result.update(ok=False, error="text is too large for a single X11 transfer")
                        else:
                            self.text = data
                            lib.XSetSelectionOwner(handle, clipboard, owner, 0)
                            lib.XFlush(handle)
                            result.update(ok=int(lib.XGetSelectionOwner(handle, clipboard)) == owner)
                    done.set()
                while lib.XPending(handle):
                    lib.XNextEvent(handle, self.ct.byref(ev))
                    if ev.type == self.SELECTION_REQUEST:
                        self._handle_request(handle, owner, ev)
                    elif ev.type == self.SELECTION_CLEAR:
                        self.text = None
                ready, _, _ = select.select([fd, self.wake_r], [], [], 0.5)
                if self.wake_r in ready:
                    os.read(self.wake_r, 64)
        finally:
            x.close(handle)


def apply_x11_mirror(x: Xlib, handle, x_display: str, snapshot: dict[str, Any]) -> None:
    """Copy the desktop's X resources, work area, XSETTINGS and keyboard layout to the private X server.

    ``handle`` must stay open while the display lives: it owns the XSETTINGS selection.
    """
    root = x.root(handle)

    resources = (snapshot.get("resource_manager") or "").encode("utf-8")
    if resources:
        x.set_property(handle, root, "RESOURCE_MANAGER", XA_STRING, 8, resources)

    workarea = snapshot.get("workarea")
    if workarea and len(workarea) == 4:
        x.set_property(handle, root, "_NET_WORKAREA", XA_CARDINAL, 32, [int(v) for v in workarea])
        x.set_property(handle, root, "_NET_CURRENT_DESKTOP", XA_CARDINAL, 32, [0])

    blob = base64.b64decode(snapshot.get("xsettings_b64") or "")
    if blob:
        # Own the XSETTINGS selection with a byte-for-byte copy of the desktop's settings,
        # so GTK (and Chromium's font/theme defaults) see the same values. This connection
        # stays open for the lifetime of the display, which keeps the owner window alive.
        owner = int(x.lib.XCreateSimpleWindow(handle, root, -10, -10, 1, 1, 0, 0, 0))
        settings_atom = x.atom(handle, "_XSETTINGS_SETTINGS")
        x.set_property(handle, owner, "_XSETTINGS_SETTINGS", settings_atom, 8, blob)
        x.lib.XSetSelectionOwner(handle, x.atom(handle, "_XSETTINGS_S0"), owner, 0)
    x.lib.XSync(handle, 0)

    xkb = snapshot.get("xkb") or {}
    layout = str(xkb.get("layout") or "")
    if layout:
        setxkbmap = shutil.which("setxkbmap")
        needs_layout = (layout, str(xkb.get("variant") or ""), str(xkb.get("options") or "")) != ("us", "", "")
        if not setxkbmap:
            if needs_layout:
                raise RuntimeError(
                    "setxkbmap is not installed, so your keyboard layout cannot be mirrored "
                    "(install x11-xkb-utils)"
                )
        else:
            cmd = [
                setxkbmap, "-display", x_display,
                "-rules", str(xkb.get("rules") or "evdev"),
                "-model", str(xkb.get("model") or "pc105"),
                "-layout", layout,
            ]
            if xkb.get("variant"):
                cmd += ["-variant", str(xkb["variant"])]
            cmd += ["-option", ""]
            if xkb.get("options"):
                cmd += ["-option", str(xkb["options"])]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=system_env())
            if result.returncode != 0:
                raise RuntimeError(f"could not apply the keyboard layout: {result.stderr.strip()}")
            applied = x.get_property(handle, root, "_XKB_RULES_NAMES") or b""
            applied_parts = applied.split(b"\0")
            if len(applied_parts) < 3 or applied_parts[2].decode("utf-8", "replace") != layout:
                raise RuntimeError("the keyboard layout did not apply to the private display")
        group = int(xkb.get("group") or 0)
        if group:
            # Several layouts: start on the one that is active on the desktop.
            x.lib.XkbLockGroup(handle, x.XKB_USE_CORE_KBD, group)
            x.lib.XSync(handle, 0)
            if x.keyboard_group(handle) != group:
                raise RuntimeError("the active keyboard layout did not apply to the private display")

    outputs = snapshot.get("outputs") or []
    if len(outputs) > 1:
        primary = next((i for i, o in enumerate(outputs, start=1) if o.get("primary")), None)
        xrandr = shutil.which("xrandr")
        if primary and xrandr:
            result = subprocess.run(
                [xrandr, "--output", f"HEADLESS-{primary}", "--primary"],
                env={**system_env(), "DISPLAY": x_display},
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                # Chromium opens new windows on the primary monitor.
                raise RuntimeError("the primary monitor could not be set on the private display")
