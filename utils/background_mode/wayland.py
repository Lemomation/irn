"""Wayland side of Background Mode: read the desktop compositor and mirror it onto the private one.

On a Wayland desktop Chromium runs as a native Wayland client, so pages see what the
compositor tells Chromium: output modes, refresh rate and scale, and which input devices
the seat has (no pointer means ``(pointer: none)``, a classic bot tell). This module:

* reads those from the user's compositor (wl_output, xdg-output, wl_seat, the exact
  keymap the compositor hands to clients), and
* recreates them on the private sway: matching outputs (through sway's config), a
  virtual pointer and a virtual keyboard carrying the same keymap, and a clipboard
  served through the wlr data-control protocol.

It speaks the Wayland wire protocol directly (stdlib only), so it adds no dependencies.
"""

from __future__ import annotations

import mmap
import os
import select
import socket
import struct
import threading
import time
from typing import Any, Callable, Optional

from utils.background_mode.common import SESSION_WAYLAND, SNAPSHOT_VERSION
from utils.background_mode.ipc import log as _log

SEAT_POINTER = 1
SEAT_KEYBOARD = 2
SEAT_TOUCH = 4

KEYMAP_FORMAT_XKB_V1 = 1

# interface -> opcode -> (event name, signature). Signature letters follow the wire
# format: i int, u uint, f fixed, s string, o object, n new_id, a array, h fd.
_EVENTS: dict[str, dict[int, tuple[str, str]]] = {
    "wl_display": {0: ("error", "ous"), 1: ("delete_id", "u")},
    "wl_registry": {0: ("global", "usu"), 1: ("global_remove", "u")},
    "wl_callback": {0: ("done", "u")},
    "wl_output": {
        0: ("geometry", "iiiiissi"),
        1: ("mode", "uiii"),
        2: ("done", ""),
        3: ("scale", "i"),
        4: ("name", "s"),
        5: ("description", "s"),
    },
    "zxdg_output_v1": {
        0: ("logical_position", "ii"),
        1: ("logical_size", "ii"),
        2: ("done", ""),
        3: ("name", "s"),
        4: ("description", "s"),
    },
    "wl_seat": {0: ("capabilities", "u"), 1: ("name", "s")},
    "wl_keyboard": {
        0: ("keymap", "uhu"),
        1: ("enter", "uoa"),
        2: ("leave", "uo"),
        3: ("key", "uuuu"),
        4: ("modifiers", "uuuuu"),
        5: ("repeat_info", "ii"),
    },
    "zwlr_data_control_device_v1": {
        0: ("data_offer", "n"),
        1: ("selection", "o"),
        2: ("finished", ""),
        3: ("primary_selection", "o"),
    },
    "zwlr_data_control_offer_v1": {0: ("offer", "s")},
    "zwlr_data_control_source_v1": {0: ("send", "sh"), 1: ("cancelled", "")},
}

# Objects the server creates through a new_id event argument.
_SERVER_CREATED = {("zwlr_data_control_device_v1", "data_offer"): "zwlr_data_control_offer_v1"}

_TRANSFORMS = ["normal", "90", "180", "270", "flipped", "flipped-90", "flipped-180", "flipped-270"]


class WaylandError(RuntimeError):
    pass


def _u32(value: int) -> bytes:
    return struct.pack("<I", int(value) & 0xFFFFFFFF)


def _string(text: str) -> bytes:
    raw = text.encode("utf-8") + b"\0"
    return _u32(len(raw)) + raw + b"\0" * ((4 - len(raw) % 4) % 4)


class WlConnection:
    """A tiny synchronous Wayland client connection."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.objects: dict[int, str] = {1: "wl_display"}
        self.handlers: dict[int, Callable[[str, list[Any]], None]] = {}
        self.next_id = 2
        self.buffer = b""
        self.fds: list[int] = []
        self.globals: dict[int, tuple[str, int]] = {}
        self.registry = self.new_object("wl_registry", self._on_registry)
        self.send(1, 1, _u32(self.registry))  # wl_display.get_registry
        self.roundtrip()

    def close(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        try:
            self.sock.close()
        except Exception:
            pass

    def new_object(self, interface: str, handler: Optional[Callable[[str, list[Any]], None]] = None) -> int:
        oid = self.next_id
        self.next_id += 1
        self.objects[oid] = interface
        if handler is not None:
            self.handlers[oid] = handler
        return oid

    def send(self, oid: int, opcode: int, payload: bytes = b"", fds: tuple[int, ...] = ()) -> None:
        data = struct.pack("<IHH", oid, opcode, 8 + len(payload)) + payload
        if fds:
            socket.send_fds(self.sock, [data], list(fds))
        else:
            self.sock.sendall(data)

    def _on_registry(self, event: str, args: list[Any]) -> None:
        if event == "global":
            self.globals[int(args[0])] = (str(args[1]), int(args[2]))
        elif event == "global_remove":
            self.globals.pop(int(args[0]), None)

    def find(self, interface: str) -> list[tuple[int, int]]:
        return [(name, version) for name, (iface, version) in sorted(self.globals.items()) if iface == interface]

    def bind(self, global_name: int, interface: str, version: int, handler=None) -> int:
        oid = self.new_object(interface, handler)
        self.send(self.registry, 0, _u32(global_name) + _string(interface) + _u32(version) + _u32(oid))
        return oid

    def bind_first(self, interface: str, max_version: int, handler=None) -> tuple[Optional[int], int]:
        found = self.find(interface)
        if not found:
            return None, 0
        name, version = found[0]
        version = min(version, max_version)
        return self.bind(name, interface, version, handler), version

    def roundtrip(self, timeout_s: float = 5.0) -> None:
        done = [False]
        callback = self.new_object("wl_callback", lambda _event, _args: done.__setitem__(0, True))
        self.send(1, 0, _u32(callback))  # wl_display.sync
        deadline = time.monotonic() + timeout_s
        while not done[0]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaylandError("the compositor did not answer")
            self.dispatch(remaining)

    def dispatch(self, timeout_s: float) -> bool:
        ready, _, _ = select.select([self.sock], [], [], max(0.0, timeout_s))
        if not ready:
            return False
        data, fds, _flags, _addr = socket.recv_fds(self.sock, 65536, 28)
        if not data and not fds:
            raise WaylandError("the compositor closed the connection")
        self.fds.extend(fds)
        self.buffer += data
        while len(self.buffer) >= 8:
            oid, opcode, size = struct.unpack_from("<IHH", self.buffer)
            if size < 8:
                raise WaylandError("malformed message from the compositor")
            if len(self.buffer) < size:
                break
            payload, self.buffer = self.buffer[8:size], self.buffer[size:]
            self._handle(oid, opcode, payload)
        return True

    def _decode(self, interface: str, event: str, signature: str, payload: bytes) -> list[Any]:
        args: list[Any] = []
        pos = 0
        for code in signature:
            if code == "i":
                args.append(struct.unpack_from("<i", payload, pos)[0])
                pos += 4
            elif code in "uo":
                args.append(struct.unpack_from("<I", payload, pos)[0])
                pos += 4
            elif code == "n":
                new_id = struct.unpack_from("<I", payload, pos)[0]
                pos += 4
                created = _SERVER_CREATED.get((interface, event))
                if created:
                    self.objects[new_id] = created
                args.append(new_id)
            elif code == "f":
                args.append(struct.unpack_from("<i", payload, pos)[0] / 256.0)
                pos += 4
            elif code in "sa":
                length = struct.unpack_from("<I", payload, pos)[0]
                pos += 4
                raw = payload[pos:pos + length]
                pos += length + (4 - length % 4) % 4
                if code == "s":
                    args.append(raw[:-1].decode("utf-8", "replace") if length else None)
                else:
                    args.append(raw)
            elif code == "h":
                args.append(self.fds.pop(0) if self.fds else -1)
        return args

    def _handle(self, oid: int, opcode: int, payload: bytes) -> None:
        interface = self.objects.get(oid)
        spec = _EVENTS.get(interface or "", {}).get(opcode)
        if spec is None:
            return  # Not modelled; none of those carry file descriptors to us.
        event, signature = spec
        args = self._decode(interface or "", event, signature, payload)
        if interface == "wl_display":
            if event == "error":
                raise WaylandError(f"protocol error on object {args[0]}: {args[2]}")
            if event == "delete_id":
                self.objects.pop(int(args[0]), None)
                self.handlers.pop(int(args[0]), None)
            return
        handler = self.handlers.get(oid)
        if handler is not None:
            handler(event, args)
        elif "h" in signature:
            for value, code in zip(args, signature):
                if code == "h" and value >= 0:
                    os.close(value)


def _read_keymap(fd: int, size: int) -> str:
    try:
        with mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ) as mapped:
            return mapped[:size].rstrip(b"\0").decode("utf-8", "replace")
    finally:
        os.close(fd)


def capture_wayland_snapshot(socket_path: str) -> dict[str, Any]:
    """Read outputs, seat capabilities and the keymap from the user's compositor."""
    conn = WlConnection(socket_path)
    try:
        outputs: dict[int, dict[str, Any]] = {}

        def output_handler(oid: int):
            def handle(event: str, args: list[Any]) -> None:
                out = outputs[oid]
                if event == "geometry":
                    out.update(make=args[5], model=args[6], transform=int(args[7]), physical_mm=[args[2], args[3]])
                elif event == "mode" and int(args[0]) & 1:
                    out.update(mode_w=int(args[1]), mode_h=int(args[2]), refresh_mhz=int(args[3]))
                elif event == "scale":
                    out["scale_int"] = int(args[0])
                elif event in ("name", "description"):
                    out[event] = args[0]
            return handle

        for name, version in conn.find("wl_output"):
            oid = conn.new_object("wl_output")
            outputs[oid] = {}
            conn.handlers[oid] = output_handler(oid)
            conn.send(conn.registry, 0, _u32(name) + _string("wl_output") + _u32(min(version, 4)) + _u32(oid))

        manager, _ = conn.bind_first("zxdg_output_manager_v1", 3)
        if manager is not None:
            for output_oid in list(outputs):
                def xdg_handler(event: str, args: list[Any], out=outputs[output_oid]) -> None:
                    if event == "logical_position":
                        out["logical_x"], out["logical_y"] = int(args[0]), int(args[1])
                    elif event == "logical_size":
                        out["logical_w"], out["logical_h"] = int(args[0]), int(args[1])
                xdg = conn.new_object("zxdg_output_v1", xdg_handler)
                conn.send(manager, 1, _u32(xdg) + _u32(output_oid))  # get_xdg_output

        seat: dict[str, Any] = {"capabilities": 0, "name": ""}

        def seat_handler(event: str, args: list[Any]) -> None:
            if event == "capabilities":
                seat["capabilities"] = int(args[0])
            elif event == "name":
                seat["name"] = args[0] or ""

        seat_id, _ = conn.bind_first("wl_seat", 5, seat_handler)
        conn.roundtrip()
        conn.roundtrip()

        keyboard: dict[str, Any] = {}
        if seat_id is not None and seat["capabilities"] & SEAT_KEYBOARD:
            def keyboard_handler(event: str, args: list[Any]) -> None:
                if event == "keymap":
                    fmt, fd, size = int(args[0]), int(args[1]), int(args[2])
                    if fmt == KEYMAP_FORMAT_XKB_V1 and fd >= 0 and size > 0:
                        keyboard["keymap"] = _read_keymap(fd, size)
                    elif fd >= 0:
                        os.close(fd)
                elif event == "repeat_info":
                    keyboard["repeat"] = [int(args[0]), int(args[1])]
            kbd = conn.new_object("wl_keyboard", keyboard_handler)
            conn.send(seat_id, 1, _u32(kbd))  # get_keyboard
            conn.roundtrip()
            conn.roundtrip()

        snapshot_outputs = []
        for out in outputs.values():
            if not out.get("mode_w"):
                continue
            transform = int(out.get("transform") or 0)
            mode_w, mode_h = int(out["mode_w"]), int(out["mode_h"])
            scale_int = int(out.get("scale_int") or 1)
            rotated_w, rotated_h = (mode_h, mode_w) if transform % 2 else (mode_w, mode_h)
            logical_w = int(out.get("logical_w") or round(rotated_w / scale_int))
            logical_h = int(out.get("logical_h") or round(rotated_h / scale_int))
            snapshot_outputs.append({
                "name": out.get("name") or "",
                "description": out.get("description") or "",
                "mode_w": mode_w,
                "mode_h": mode_h,
                "refresh_mhz": int(out.get("refresh_mhz") or 60000),
                "refresh": int(out.get("refresh_mhz") or 60000) / 1000.0,
                "scale": round(rotated_w / logical_w, 6) if logical_w else float(scale_int),
                "scale_int": scale_int,
                "transform": transform,
                "x": int(out.get("logical_x") or 0),
                "y": int(out.get("logical_y") or 0),
                "width": logical_w,
                "height": logical_h,
            })
        snapshot_outputs.sort(key=lambda o: (o["y"], o["x"]))

        snapshot: dict[str, Any] = {
            "version": SNAPSHOT_VERSION,
            "captured_at": time.time(),
            "session_type": SESSION_WAYLAND,
            "outputs": snapshot_outputs,
            "seat_capabilities": int(seat["capabilities"]),
            "keymap": keyboard.get("keymap") or "",
            "repeat": keyboard.get("repeat") or [],
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP") or "",
            "globals": sorted({iface for iface, _version in conn.globals.values()}),
        }
        if not snapshot_outputs:
            snapshot["degraded"] = True
        return snapshot
    finally:
        conn.close()


def sway_output_lines(snapshot: dict[str, Any]) -> list[str]:
    """sway config lines that reproduce the desktop's outputs (mode, exact refresh, scale, layout)."""
    lines = []
    for index, out in enumerate(snapshot.get("outputs") or [], start=1):
        transform = _TRANSFORMS[int(out.get("transform") or 0) % 8]
        # sway truncates refresh * 1000 to an int, so nudge the decimal to land on the exact mHz.
        refresh = int(out.get("refresh_mhz") or 60000) / 1000.0 + 0.0004
        lines.append(
            f"output HEADLESS-{index} mode --custom {int(out['mode_w'])}x{int(out['mode_h'])}@{refresh:.4f}Hz "
            f"position {int(out['x'])} {int(out['y'])} scale {float(out.get('scale') or 1):g} transform {transform}"
        )
    return lines


class PrivateSeat(threading.Thread):
    """
    Input devices and clipboard on the private compositor.

    Creates a virtual pointer and a virtual keyboard (with the desktop's keymap) so the
    private seat has the same capabilities as the user's, and owns the clipboard through
    wlr-data-control. The connection must stay open: the devices vanish with it.
    """

    CLIPBOARD_MIME_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")

    def __init__(self, socket_path: str, snapshot: dict[str, Any]):
        super().__init__(name="irp-bg-wayland-seat", daemon=True)
        self.conn = WlConnection(socket_path)
        self.stop_event = threading.Event()
        self.wake_r, self.wake_w = os.pipe()
        self.commands: list[tuple[str, Any, threading.Event, dict]] = []
        self.commands_lock = threading.Lock()
        self.text: Optional[bytes] = None
        self.source: Optional[int] = None
        self.offers: set[int] = set()
        self.seat_caps = [0]

        wanted = int(snapshot.get("seat_capabilities") or 0)
        if wanted & SEAT_TOUCH:
            raise RuntimeError("your desktop has a touchscreen, which the private display cannot reproduce")

        def seat_handler(event: str, args: list[Any]) -> None:
            if event == "capabilities":
                self.seat_caps[0] = int(args[0])

        self.seat, _ = self.conn.bind_first("wl_seat", 5, seat_handler)
        if self.seat is None:
            raise RuntimeError("the private compositor has no seat")

        if wanted & SEAT_POINTER:
            manager, _ = self.conn.bind_first("zwlr_virtual_pointer_manager_v1", 1)
            if manager is None:
                raise RuntimeError("the private compositor cannot create a virtual pointer")
            pointer = self.conn.new_object("zwlr_virtual_pointer_v1")
            self.conn.send(manager, 0, _u32(self.seat) + _u32(pointer))

        if wanted & SEAT_KEYBOARD:
            keymap = str(snapshot.get("keymap") or "")
            if not keymap:
                raise RuntimeError("your desktop's keymap could not be read")
            manager, _ = self.conn.bind_first("zwp_virtual_keyboard_manager_v1", 1)
            if manager is None:
                raise RuntimeError("the private compositor cannot create a virtual keyboard")
            keyboard = self.conn.new_object("zwp_virtual_keyboard_v1")
            self.conn.send(manager, 0, _u32(self.seat) + _u32(keyboard))
            data = keymap.encode("utf-8") + b"\0"
            fd = os.memfd_create("irp-keymap", os.MFD_CLOEXEC)
            try:
                os.write(fd, data)
                self.conn.send(keyboard, 0, _u32(KEYMAP_FORMAT_XKB_V1) + _u32(len(data)), fds=(fd,))
            finally:
                os.close(fd)

        self.device: Optional[int] = None
        self.data_control, _ = self.conn.bind_first("zwlr_data_control_manager_v1", 2)
        if self.data_control is not None:
            self.device = self.conn.new_object("zwlr_data_control_device_v1", self._on_device)
            self.conn.send(self.data_control, 1, _u32(self.device) + _u32(self.seat))

        self.conn.roundtrip()
        self.conn.roundtrip()
        got = self.seat_caps[0] & (SEAT_POINTER | SEAT_KEYBOARD)
        if got != wanted & (SEAT_POINTER | SEAT_KEYBOARD):
            raise RuntimeError(f"the private seat has input capabilities {got}, the desktop has {wanted}")

    def _on_device(self, event: str, args: list[Any]) -> None:
        if event == "data_offer":
            self.offers.add(int(args[0]))
        elif event == "selection":
            current = int(args[0] or 0)
            for offer in list(self.offers):
                if offer != current:
                    self.conn.send(offer, 1)  # zwlr_data_control_offer_v1.destroy
                    self.offers.discard(offer)

    def _on_source(self, source: int):
        def handle(event: str, args: list[Any]) -> None:
            if event == "send":
                mime, fd = str(args[0] or ""), int(args[1])
                if fd < 0:
                    return
                try:
                    data = self.text or b""
                    if mime in ("STRING", "TEXT", "text/plain"):
                        data = data.decode("utf-8", "replace").encode("latin-1", "replace")
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                except OSError:
                    pass
                finally:
                    os.close(fd)
            elif event == "cancelled":
                self.conn.send(source, 1)  # destroy
                if self.source == source:
                    self.source = None
        return handle

    def set_text(self, text: str, timeout_s: float = 5.0) -> dict[str, Any]:
        done, result = threading.Event(), {}
        with self.commands_lock:
            self.commands.append(("set", text, done, result))
        os.write(self.wake_w, b"x")
        done.wait(timeout_s)
        return result or {"ok": False, "error": "the clipboard did not answer"}

    def _set_text_now(self, text: str) -> dict[str, Any]:
        if self.data_control is None or self.device is None:
            return {"ok": False, "error": "the private compositor has no data-control clipboard"}
        self.text = str(text or "").encode("utf-8")
        source = self.conn.new_object("zwlr_data_control_source_v1")
        self.conn.handlers[source] = self._on_source(source)
        self.conn.send(self.data_control, 0, _u32(source))  # create_data_source
        for mime in self.CLIPBOARD_MIME_TYPES:
            self.conn.send(source, 0, _string(mime))  # offer
        self.conn.send(self.device, 0, _u32(source))  # set_selection
        self.source = source
        self.conn.roundtrip()
        return {"ok": True}

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                ready, _, _ = select.select([self.conn.sock, self.wake_r], [], [], 0.5)
                if self.conn.sock in ready:
                    self.conn.dispatch(0)
                if self.wake_r in ready:
                    os.read(self.wake_r, 64)
                    with self.commands_lock:
                        pending, self.commands = self.commands, []
                    for cmd, value, done, result in pending:
                        try:
                            if cmd == "set":
                                result.update(self._set_text_now(value))
                        except Exception as exc:
                            result.update(ok=False, error=str(exc))
                        done.set()
        except Exception as exc:
            if not self.stop_event.is_set():
                _log(f"the private seat stopped: {exc}", "warning")
        finally:
            self.conn.close()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass
        self.join(timeout=2)
