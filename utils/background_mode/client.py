"""App-side handle to the Background Mode supervisor and its private display."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from utils.background_mode.common import (
    COMPOSITOR_LOG_FILE,
    PROTOCOL_PREFIX,
    SESSION_WAYLAND,
    SESSION_X11,
    SUPERVISOR_LOG_FILE,
    BackgroundModeUnavailable,
    load_desktop_snapshot,
    save_desktop_snapshot,
    state_dir,
)
from utils.background_mode.support import (
    check_background_mode_support,
    detect_session,
    wayland_socket_path,
)
from utils.logger import Logger

SUPERVISOR_MODULE = "utils.background_mode.supervisor"


def _supervisor_command() -> tuple[list[str], dict[str, str], Optional[str]]:
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        # main.py runs "<app> -m <module>" through runpy in frozen builds.
        return [sys.executable, "-m", SUPERVISOR_MODULE, "--serve"], env, None
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return [sys.executable, "-m", SUPERVISOR_MODULE, "--serve"], env, repo_root


class PrivateDisplay:
    """Handle to a running supervisor and its private display."""

    def __init__(self, proc: subprocess.Popen, info: dict[str, Any], events: "queue.Queue[dict | None]"):
        self._proc = proc
        self._events = events
        self._lock = threading.Lock()
        self._request_seq = 0
        self._replies: dict[int, dict[str, Any]] = {}
        self.info = info
        self.snapshot: dict[str, Any] = dict(info.get("snapshot") or {})
        self.snapshot_source: str = str(info.get("snapshot_source") or "")
        self.session_type: str = str(self.snapshot.get("session_type") or SESSION_X11)
        self.x_display: str = str(info.get("display") or "")
        self.wayland_socket: str = str(info.get("wayland_socket") or "")
        self._dead_reason: Optional[str] = None
        self._dispatch = threading.Thread(target=self._dispatch_loop, name="irp-bg-events", daemon=True)
        self._dispatch.start()

    @property
    def description(self) -> str:
        return self.wayland_socket if self.session_type == SESSION_WAYLAND else self.x_display

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                self._dead_reason = self._dead_reason or "supervisor exited"
                with self._lock:
                    for slot in self._replies.values():
                        slot.setdefault("done", True)
                return
            kind = event.get("event")
            if kind == "reply":
                with self._lock:
                    slot = self._replies.get(int(event.get("id") or 0))
                    if slot is not None:
                        slot.update(event)
                        slot["done"] = True
            elif kind == "log":
                level = str(event.get("level") or "debug")
                message = f"Background Mode: {event.get('message')}"
                if level == "warning":
                    Logger.warning(message)
                elif level == "info":
                    Logger.info(message)
                else:
                    Logger.debug(message)
            elif kind == "fatal":
                self._dead_reason = str(event.get("message") or "supervisor failed")
                Logger.error(f"Background Mode: {self._dead_reason}")

    def _send(self, payload: dict[str, Any]) -> bool:
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            return True
        except Exception:
            return False

    async def _request(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        with self._lock:
            self._request_seq += 1
            seq = self._request_seq
            self._replies[seq] = {}
        if not self._send({**payload, "id": seq}):
            with self._lock:
                self._replies.pop(seq, None)
            return {"ok": False, "error": "supervisor pipe closed"}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                slot = self._replies.get(seq) or {}
                if slot.get("done"):
                    self._replies.pop(seq, None)
                    return slot
            await asyncio.sleep(0.02)
        with self._lock:
            self._replies.pop(seq, None)
        return {"ok": False, "error": "the supervisor did not answer in time"}

    def alive(self) -> bool:
        return self._proc.poll() is None and self._dead_reason is None

    def browser_env(self) -> dict[str, str]:
        """Chromium's environment: the app's own, pointed at the private display instead of the desktop."""
        env = dict(os.environ)
        if self.session_type == SESSION_WAYLAND:
            env["WAYLAND_DISPLAY"] = self.wayland_socket
            env.pop("WAYLAND_SOCKET", None)
            # Chromium only picks its Wayland backend when the session type says so, which
            # is not the case when IntenseRP was started outside a desktop session.
            env["XDG_SESSION_TYPE"] = "wayland"
            if self.x_display:
                env["DISPLAY"] = self.x_display
            else:
                env.pop("DISPLAY", None)
        else:
            env["DISPLAY"] = self.x_display
            env.pop("WAYLAND_DISPLAY", None)
            env.pop("WAYLAND_SOCKET", None)
        # Started outside a desktop session (SSH, TTY, service)? Point Chromium at the
        # user's runtime dir and session bus when they exist, so it reaches the same audio
        # server and settings portal (AudioContext latency and output devices are visible
        # to pages).
        try:
            user_runtime = Path(f"/run/user/{os.getuid()}")
        except Exception:
            user_runtime = None
        if user_runtime is not None and user_runtime.is_dir():
            if not env.get("XDG_RUNTIME_DIR"):
                env["XDG_RUNTIME_DIR"] = str(user_runtime)
            if not env.get("DBUS_SESSION_BUS_ADDRESS") and (user_runtime / "bus").exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={user_runtime / 'bus'}"
        return env

    async def set_clipboard_text(self, text: str, timeout_s: float = 6.0) -> bool:
        """Put text on the private display's clipboard (what Ctrl+V pastes in the hidden browser)."""
        reply = await self._request({"cmd": "clipboard", "text": str(text or "")}, timeout_s)
        return bool(reply.get("ok"))

    def mark_launch(self) -> None:
        """A browser is about to start: the next settle() waits for its new window."""
        self._send({"cmd": "mark_launch"})

    def hint_maximized_window(self, user_data_dir: str) -> None:
        """The next window of this Chromium profile restores maximized (from its saved placement)."""
        # Chromium appends " (<user-data-dir>)" to its X11 WM_CLASS instance name.
        self._send({"cmd": "maximize_hint", "class_contains": f"({user_data_dir})"})

    async def settle(self, timeout_s: float = 4.0) -> dict[str, Any]:
        """Wait until every browser window sits where the mirrored window manager would put it."""
        return await self._request({"cmd": "settle", "timeout": float(timeout_s)}, timeout_s + 2.0)

    def stop(self) -> None:
        self._send({"cmd": "stop"})
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=10)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


_shared_lock = threading.Lock()
_shared_display: Optional[PrivateDisplay] = None
_shared_refs = 0
_atexit_registered = False


def _start_private_display_blocking(config_dir) -> PrivateDisplay:
    ok, reason = check_background_mode_support(config_dir)
    if not ok:
        raise BackgroundModeUnavailable(reason)

    sdir = state_dir(config_dir)
    session = detect_session()
    request = {
        "cmd": "start",
        "session": session,
        "real_display": os.environ.get("DISPLAY") if session == SESSION_X11 else None,
        "real_wayland_socket": wayland_socket_path() if session == SESSION_WAYLAND else None,
        "saved_snapshot": load_desktop_snapshot(config_dir),
        "compositor_log": str(sdir / COMPOSITOR_LOG_FILE),
        # The compiled Xwayland shim is cached here, never in a shared temp path.
        "state_dir": str(sdir) if config_dir else None,
        "parent_pid": os.getpid(),
    }

    cmd, env, cwd = _supervisor_command()
    stderr_log = open(sdir / SUPERVISOR_LOG_FILE, "w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_log,
            cwd=cwd,
            env=env,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    finally:
        stderr_log.close()

    events: "queue.Queue[dict | None]" = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.startswith(PROTOCOL_PREFIX):
                continue
            try:
                events.put(json.loads(line[len(PROTOCOL_PREFIX):]))
            except Exception:
                continue
        events.put(None)

    threading.Thread(target=_reader, name="irp-bg-reader", daemon=True).start()

    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
    except Exception as exc:
        proc.kill()
        raise BackgroundModeUnavailable(f"could not start the private display supervisor: {exc}")

    early_exit = f"the private display supervisor exited early (see {sdir / SUPERVISOR_LOG_FILE})."
    deadline = time.monotonic() + 60.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            raise BackgroundModeUnavailable("the private display did not become ready in time.")
        try:
            event = events.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            if proc.poll() is not None:
                raise BackgroundModeUnavailable(early_exit)
            continue
        if event is None:
            raise BackgroundModeUnavailable(early_exit)
        kind = event.get("event")
        if kind == "ready":
            break
        if kind == "fatal":
            try:
                # Let the supervisor stop the compositor and remove its runtime files.
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
            raise BackgroundModeUnavailable(str(event.get("message") or "the private display failed."))
        if kind == "log":
            Logger.debug(f"Background Mode: {event.get('message')}")

    display = PrivateDisplay(proc, event, events)
    if display.snapshot_source == "live" and display.snapshot and not display.snapshot.get("degraded"):
        try:
            save_desktop_snapshot(config_dir, display.snapshot)
        except Exception:
            pass
    return display


async def acquire_private_display(config_dir) -> PrivateDisplay:
    """Start (or share) the private display. Raises BackgroundModeUnavailable."""
    global _shared_display, _shared_refs, _atexit_registered

    with _shared_lock:
        if _shared_display is not None and _shared_display.alive():
            _shared_refs += 1
            return _shared_display

    loop = asyncio.get_running_loop()
    display = await loop.run_in_executor(None, _start_private_display_blocking, config_dir)

    with _shared_lock:
        if not _atexit_registered:
            atexit.register(shutdown_private_displays)
            _atexit_registered = True
        if _shared_display is not None and _shared_display.alive():
            # Another provider won the race; keep theirs.
            _shared_refs += 1
            winner = _shared_display
        else:
            _shared_display = display
            _shared_refs = 1
            winner = display
    if winner is not display:
        display.stop()
    return winner


def release_private_display(display: Optional[PrivateDisplay]) -> None:
    global _shared_display, _shared_refs
    if display is None:
        return
    stop_now = False
    with _shared_lock:
        if display is _shared_display:
            _shared_refs = max(0, _shared_refs - 1)
            if _shared_refs == 0:
                _shared_display = None
                stop_now = True
        else:
            stop_now = True
    if stop_now:
        display.stop()


def shutdown_private_displays() -> None:
    global _shared_display, _shared_refs
    with _shared_lock:
        display = _shared_display
        _shared_display = None
        _shared_refs = 0
    if display is not None:
        display.stop()
