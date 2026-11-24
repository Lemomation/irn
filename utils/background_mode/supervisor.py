"""
Background Mode supervisor process.

Started by the app as ``python -m utils.background_mode.supervisor --serve`` (or
``<app> -m utils.background_mode.supervisor --serve`` in frozen builds). It reads the
user's desktop, starts a private headless sway that renders on the real GPU, mirrors the
desktop into it, and then serves small requests (window settling, clipboard) over a
JSON-lines pipe until the pipe closes.

Running this out of process keeps Xlib, which may exit() on fatal display errors, away
from the app, and ties the compositor's lifetime to the app: when the pipe closes the
supervisor stops sway, and sway's own watchdog checks the supervisor and app PIDs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from utils.background_mode.common import (
    SAFE_PATH_RE,
    SESSION_WAYLAND,
    SESSION_X11,
    SHIM_DIR_NAME,
    system_env,
)
from utils.background_mode.ipc import emit as _emit
from utils.background_mode.ipc import log as _log
from utils.background_mode.sway import SwayIPC, iter_views
from utils.background_mode.xcvt import build_xcvt_shim, exact_mode_spec, pick_output_refresh


class Supervisor:
    def __init__(self) -> None:
        self.session = SESSION_X11
        self.runtime_dir: Optional[Path] = None
        self.sway: Optional[subprocess.Popen] = None
        self.swaysock = ""
        self.x_display = ""
        self.wayland_socket = ""
        self.refresh_plan: list[dict[str, Any]] = []
        # X11 mirror
        self.x = None
        self.private_handle = None
        self.placer = None
        self.x_clipboard = None
        self.xcvt_specs: list[str] = []
        self.xcvt_shim: Optional[Path] = None
        self.shim_dir: Optional[Path] = None
        self.xwayland_wrapper: Optional[Path] = None
        # Wayland mirror
        self.seat = None
        # Windows that existed when the app announced a browser launch.
        self.known_views: Optional[set[int]] = None

    # -- desktop snapshot --------------------------------------------------

    def _capture_live(self, request: dict[str, Any]) -> Optional[dict[str, Any]]:
        session = request.get("session")
        try:
            if session == SESSION_X11 and request.get("real_display"):
                from utils.background_mode.x11 import Xlib, capture_x11_snapshot

                self.x = self.x or Xlib()
                return capture_x11_snapshot(self.x, str(request["real_display"]))
            if session == SESSION_WAYLAND and request.get("real_wayland_socket"):
                from utils.background_mode.wayland import capture_wayland_snapshot

                return capture_wayland_snapshot(str(request["real_wayland_socket"]))
        except Exception as exc:
            _log(f"could not read your desktop ({exc}); trying the saved copy", "warning")
        return None

    def _choose_snapshot(self, request: dict[str, Any]) -> tuple[dict[str, Any], str]:
        saved = request.get("saved_snapshot") if isinstance(request.get("saved_snapshot"), dict) else None
        if saved is not None and saved.get("degraded"):
            saved = None
        live = self._capture_live(request)
        if live is not None and live.get("degraded"):
            if saved is not None and saved.get("session_type") == live.get("session_type"):
                _log("no monitor is active right now (screen off or lid closed); mirroring the saved desktop snapshot", "info")
                return saved, "saved"
            # Never run hidden on guessed screen values: that would change the fingerprint.
            raise RuntimeError(
                "no monitor is active and there is no saved desktop snapshot yet, so your screen "
                "cannot be mirrored. Start a provider once with your monitor on."
            )
        if live is not None:
            return live, "live"
        if saved is not None:
            return saved, "saved"
        raise RuntimeError("there is no desktop snapshot to mirror")

    # -- compositor ----------------------------------------------------------

    def _make_runtime_dir(self) -> Path:
        base = os.environ.get("XDG_RUNTIME_DIR") or ""
        if not base or not os.path.isdir(base) or not SAFE_PATH_RE.match(base):
            base = tempfile.gettempdir()
        path = Path(tempfile.mkdtemp(prefix="intenserp-bg-", dir=base))
        if not SAFE_PATH_RE.match(str(path)):
            path = Path(tempfile.mkdtemp(prefix="intenserp-bg-", dir="/tmp"))
        os.chmod(path, 0o700)
        return path

    def _x11_output_lines(self, snapshot: dict[str, Any]) -> list[str]:
        lines = []
        self.refresh_plan = []
        self.xcvt_specs = []
        for index, output in enumerate(snapshot.get("outputs") or [], start=1):
            width, height = int(output["width"]), int(output["height"])
            target = float(output.get("refresh") or 60.0)
            spec = exact_mode_spec(output) if self.xcvt_shim else None
            if spec is not None:
                # The Xwayland shim hands out the desktop's own mode line for this size/rate.
                mhz, expected, method = int(round(target * 1000)), target, "exact"
                self.xcvt_specs.append(spec.format(mhz=mhz))
            else:
                mhz, expected = pick_output_refresh(width, height, target)
                method = "nearest"
            self.refresh_plan.append(
                {"output": f"HEADLESS-{index}", "target": target, "mhz": mhz, "expected": expected, "method": method}
            )
            # sway truncates refresh * 1000 to an int, so nudge the decimal to land on mhz.
            lines.append(
                f"output HEADLESS-{index} mode --custom {width}x{height}"
                f"@{mhz / 1000.0 + 0.0004:.4f}Hz position {int(output['x'])} {int(output['y'])} scale 1"
            )
        return lines

    def _write_sway_config(self, snapshot: dict[str, Any], parent_pid: int) -> Path:
        assert self.runtime_dir is not None
        swaymsg = shutil.which("swaymsg") or "swaymsg"
        ready = self.runtime_dir / "ready"
        session_script = self.runtime_dir / "session.sh"
        session_script.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n%s\\n%s\\n%s\\n' \"$WAYLAND_DISPLAY\" \"$DISPLAY\" \"$SWAYSOCK\" \"$XDG_RUNTIME_DIR\" > '{ready}.tmp' "
            f"&& mv '{ready}.tmp' '{ready}'\n"
            # Watchdog: if the supervisor or the app disappears, take the compositor down.
            f"while [ -S \"$SWAYSOCK\" ] && kill -0 {int(os.getpid())} 2>/dev/null && kill -0 {int(parent_pid)} 2>/dev/null; do sleep 2; done\n"
            f"'{swaymsg}' exit >/dev/null 2>&1\n",
            encoding="utf-8",
        )
        if self.session == SESSION_X11:
            xwayland = "xwayland force"
        else:
            # Native Wayland Chromium does not need X, but the desktop has an X server
            # too (DISPLAY is set), so offer one lazily.
            xwayland = "xwayland enable" if shutil.which("Xwayland") else "xwayland disable"
        lines = [
            "# Generated by IntenseRP for Background Mode. Rewritten on every start.",
            xwayland,
            "default_border none",
            "default_floating_border none",
            "floating_maximum_size -1 x -1",
            "focus_follows_mouse no",
            "mouse_warping none",
            "focus_on_window_activation focus",
            'for_window [shell="xwayland"] floating enable',
            'for_window [shell="xdg_shell"] floating enable',
        ]
        if self.session == SESSION_X11:
            lines += self._x11_output_lines(snapshot)
        else:
            from utils.background_mode.wayland import sway_output_lines

            lines += sway_output_lines(snapshot)
        lines.append(f"exec /bin/sh {session_script}")
        config = self.runtime_dir / "sway.conf"
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config

    def _write_xwayland_wrapper(self) -> Optional[Path]:
        """Wrapper that preloads the mode-line shim into Xwayland only (never into sway or Chromium)."""
        xwayland = shutil.which("Xwayland")
        if not (self.xcvt_shim and self.xcvt_specs and xwayland and self.shim_dir):
            return None
        wrapper = self.shim_dir / f"xwayland-{os.getpid()}.sh"
        modes = ";".join(self.xcvt_specs)
        if not (SAFE_PATH_RE.match(str(wrapper)) and SAFE_PATH_RE.match(str(self.xcvt_shim))
                and SAFE_PATH_RE.match(xwayland) and re.match(r"^[0-9,;]+$", modes)):
            return None
        wrapper.write_text(
            "#!/bin/sh\n"
            f"IRP_XCVT_MODES='{modes}'; export IRP_XCVT_MODES\n"
            f"LD_PRELOAD='{self.xcvt_shim}'${{LD_PRELOAD:+:$LD_PRELOAD}}; export LD_PRELOAD\n"
            f"exec '{xwayland}' \"$@\"\n",
            encoding="utf-8",
        )
        os.chmod(wrapper, 0o700)
        self.xwayland_wrapper = wrapper
        return wrapper

    def _start_sway(self, snapshot: dict[str, Any], compositor_log: str, parent_pid: int) -> None:
        sway = shutil.which("sway")
        if not sway:
            raise RuntimeError("sway is not installed")
        self.runtime_dir = self._make_runtime_dir()
        config = self._write_sway_config(snapshot, parent_pid)
        outputs = max(1, len(snapshot.get("outputs") or []))
        wrapper = self._write_xwayland_wrapper() if self.session == SESSION_X11 else None

        env = {
            k: v
            for k, v in system_env().items()
            if k not in {"DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "SWAYSOCK", "I3SOCK"}
        }
        env.update(
            {
                "WLR_BACKENDS": "headless",
                "WLR_RENDERER": "gles2",
                "WLR_LIBINPUT_NO_DEVICES": "1",
                "WLR_HEADLESS_OUTPUTS": str(outputs),
            }
        )
        if wrapper is not None:
            env["WLR_XWAYLAND"] = str(wrapper)
        runtime = env.get("XDG_RUNTIME_DIR") or ""
        if not runtime or not os.path.isdir(runtime):
            env["XDG_RUNTIME_DIR"] = str(self.runtime_dir)

        try:
            log_handle = open(compositor_log, "w", encoding="utf-8", errors="replace")
        except Exception:
            log_handle = open(os.devnull, "w")
        try:
            self.sway = subprocess.Popen(
                [sway, "-c", str(config)],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()

        ready = self.runtime_dir / "ready"
        deadline = time.monotonic() + 25.0
        while not ready.exists():
            if self.sway.poll() is not None:
                raise RuntimeError(_compositor_exit_reason(compositor_log))
            if time.monotonic() > deadline:
                raise RuntimeError(f"the compositor did not start in time (see {compositor_log})")
            time.sleep(0.05)
        fields = (ready.read_text(encoding="utf-8").splitlines() + ["", "", "", ""])[:4]
        wayland_display, x_display, swaysock, runtime_dir = fields
        if not swaysock or not wayland_display:
            raise RuntimeError("the compositor did not report its sockets")
        self.swaysock = swaysock
        self.x_display = x_display
        self.wayland_socket = (
            wayland_display if wayland_display.startswith("/") else str(Path(runtime_dir or env["XDG_RUNTIME_DIR"]) / wayland_display)
        )

        if self.session == SESSION_X11:
            if not x_display:
                raise RuntimeError("the compositor started without Xwayland")
            deadline = time.monotonic() + 15.0
            while True:
                handle = self.x.open(x_display)
                if handle:
                    self.private_handle = handle
                    break
                if self.sway.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Xwayland did not accept connections")
                time.sleep(0.05)

    # -- session mirrors -----------------------------------------------------

    # Largest refresh difference accepted when the exact mode line is unavailable. Pages can
    # measure requestAnimationFrame; one CVT step near 60 Hz is about 0.1-0.2 Hz.
    REFRESH_TOLERANCE_HZ = 0.05

    def _report_x11_refresh(self) -> bool:
        """Log the X11 refresh Chromium will pace with. False if the exact mode line did not apply."""
        from utils.background_mode.x11 import query_outputs

        achieved = query_outputs(self.x_display)
        exact_ok = True
        for plan in self.refresh_plan:
            got = next((o for o in achieved if o.get("name") == plan["output"]), None)
            plan["achieved"] = got.get("refresh") if got else None
            if plan["achieved"] is None:
                continue
            if plan.get("method") == "exact" and abs(plan["achieved"] - plan["target"]) > 0.0005:
                exact_ok = False
            _log(
                f"{plan['output']}: desktop refresh {plan['target']:.4f} Hz, private display "
                f"{plan['achieved']:.4f} Hz (difference {abs(plan['achieved'] - plan['target']):.4f} Hz)",
                "info",
            )
        return exact_ok

    def _start_x11(self, snapshot: dict[str, Any], request: dict[str, Any]) -> None:
        from utils.background_mode.x11 import ClipboardOwner, Placer, Xlib, apply_x11_mirror

        self.x = self.x or Xlib()
        state_dir = request.get("state_dir")
        if state_dir:
            self.shim_dir = Path(str(state_dir)) / SHIM_DIR_NAME
            self.xcvt_shim = build_xcvt_shim(self.shim_dir)
        compositor_log = str(request.get("compositor_log") or os.devnull)
        parent_pid = int(request.get("parent_pid") or os.getppid())
        try:
            self._start_sway(snapshot, compositor_log, parent_pid)
            exact_ok = self._report_x11_refresh()
        except Exception as exc:
            if self.xwayland_wrapper is None:
                raise
            _log(f"Xwayland did not start with the exact mode line ({exc}); using the nearest CVT mode", "warning")
            exact_ok = False
        if not exact_ok:
            # Without the shim the plain CVT mode can be ~0.2 Hz off; the nearest-mode
            # search gets within one CVT step, so restart with that instead.
            _log("the exact mode line did not take effect; restarting with the nearest CVT mode", "warning")
            self._teardown_compositor()
            self.xcvt_shim = None
            self._start_sway(snapshot, compositor_log, parent_pid)
            self._report_x11_refresh()
        for plan in self.refresh_plan:
            achieved = plan.get("achieved")
            if achieved is None or abs(float(achieved) - float(plan["target"])) > self.REFRESH_TOLERANCE_HZ:
                raise RuntimeError(
                    f"the private display cannot match your refresh rate ({plan['target']:.3f} Hz; "
                    f"got {achieved if achieved is not None else 'unknown'})"
                )
        apply_x11_mirror(self.x, self.private_handle, self.x_display, snapshot)
        self.placer = Placer(self.x, self.x_display, self.swaysock, list(snapshot.get("workarea") or [0, 0, 0, 0]))
        self.placer.start()
        self.x_clipboard = ClipboardOwner(self.x, self.x_display)
        self.x_clipboard.start()

    def _start_wayland(self, snapshot: dict[str, Any], request: dict[str, Any]) -> None:
        from utils.background_mode.wayland import PrivateSeat, capture_wayland_snapshot

        if int(snapshot.get("seat_capabilities") or 0) & 4:
            raise RuntimeError("your desktop has a touchscreen, which the private display cannot reproduce")
        compositor_log = str(request.get("compositor_log") or os.devnull)
        parent_pid = int(request.get("parent_pid") or os.getppid())
        self._start_sway(snapshot, compositor_log, parent_pid)
        self.seat = PrivateSeat(self.wayland_socket, snapshot)
        self.seat.start()

        # Read the private compositor back the way Chromium will see it and compare.
        private = capture_wayland_snapshot(self.wayland_socket)
        wanted = snapshot.get("outputs") or []
        got = private.get("outputs") or []
        keys = ("mode_w", "mode_h", "refresh_mhz", "width", "height", "x", "y", "transform")
        mismatches = []
        if len(wanted) != len(got):
            mismatches.append(f"{len(got)} outputs instead of {len(wanted)}")
        for want, have in zip(wanted, got):
            for key in keys:
                if int(want.get(key) or 0) != int(have.get(key) or 0):
                    mismatches.append(f"{key} {have.get(key)} instead of {want.get(key)}")
            if abs(float(want.get("scale") or 1) - float(have.get("scale") or 1)) > 0.01:
                mismatches.append(f"scale {have.get('scale')} instead of {want.get('scale')}")
        if (private.get("keymap") or "") != (snapshot.get("keymap") or ""):
            mismatches.append("keymap differs")
        if int(private.get("seat_capabilities") or 0) != int(snapshot.get("seat_capabilities") or 0):
            mismatches.append(
                f"input capabilities {private.get('seat_capabilities')} instead of {snapshot.get('seat_capabilities')}"
            )
        if mismatches:
            raise RuntimeError("the private display does not match your desktop: " + "; ".join(mismatches))
        for out in got:
            _log(
                f"{out['mode_w']}x{out['mode_h']}@{out['refresh_mhz'] / 1000:.3f}Hz scale {out['scale']:g} "
                "mirrored exactly",
                "info",
            )

    def _wait_for_helpers(self, timeout_s: float = 10.0) -> None:
        """Fail the start (the app then opens the browser visibly) if a helper did not come up."""
        deadline = time.monotonic() + timeout_s
        for thread in (self.placer, self.x_clipboard, self.seat):
            ready = getattr(thread, "ready", None)
            while thread is not None and ready is not None and not ready.is_set() and thread.is_alive():
                if time.monotonic() > deadline:
                    raise RuntimeError(f"a private display helper ({thread.name}) did not start")
                time.sleep(0.02)
        problem = self.health_problem()
        if problem:
            raise RuntimeError(problem)

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        snapshot, source = self._choose_snapshot(request)
        self.session = str(snapshot.get("session_type") or SESSION_X11)
        if self.session == SESSION_WAYLAND:
            self._start_wayland(snapshot, request)
        else:
            self._start_x11(snapshot, request)
        self._wait_for_helpers()
        return {
            "display": self.x_display,
            "wayland_socket": self.wayland_socket,
            "snapshot": snapshot,
            "snapshot_source": source,
            "sway_pid": self.sway.pid if self.sway else None,
            "refresh_plan": self.refresh_plan,
        }

    # -- requests --------------------------------------------------------------

    def set_clipboard(self, text: str) -> dict[str, Any]:
        owner = self.seat if self.session == SESSION_WAYLAND else self.x_clipboard
        if owner is None:
            return {"ok": False, "error": "no clipboard on the private display"}
        return owner.set_text(text)

    def _views(self) -> list[dict[str, Any]]:
        try:
            ipc = SwayIPC(self.swaysock)
            try:
                return list(iter_views(ipc.tree()))
            finally:
                ipc.close()
        except Exception:
            return []

    def mark_launch(self) -> None:
        self.known_views = {int(v["id"]) for v in self._views()}

    def settle(self, timeout_s: float) -> dict[str, Any]:
        """
        Wait for the announced browser window to map and come to rest.

        Chromium maps its window about a second after Playwright hands over the page, and
        the compositor's first configure (tiled, then floating) only happens then.
        """
        deadline = time.monotonic() + max(0.5, timeout_s)
        known, self.known_views = self.known_views, None
        if known is not None:
            while time.monotonic() < deadline:
                if any(int(v["id"]) not in known for v in self._views()):
                    break
                time.sleep(0.05)
            else:
                return {"ok": False, "error": "the browser window did not appear"}
        if self.session == SESSION_X11 and self.placer is not None:
            return self.placer.settle(max(0.5, deadline - time.monotonic()))
        last, steady = None, 0
        while time.monotonic() < deadline:
            state = sorted(
                (int(v["id"]), v.get("type"), tuple(sorted((v.get("rect") or {}).items())))
                for v in self._views()
            )
            steady = steady + 1 if state == last else 0
            if steady >= 3 and all(item[1] == "floating_con" for item in state):
                return {"ok": True}
            last = state
            time.sleep(0.1)
        return {"ok": False, "error": "windows did not settle"}

    # -- shutdown --------------------------------------------------------------

    def _teardown_compositor(self) -> None:
        if self.sway is not None and self.sway.poll() is None:
            graceful = False
            if self.swaysock:
                try:
                    ipc = SwayIPC(self.swaysock)
                    ipc.command("exit")
                    ipc.close()
                    graceful = True
                except Exception:
                    pass
            try:
                self.sway.wait(timeout=3 if graceful else 0.01)
            except Exception:
                try:
                    self.sway.terminate()
                    self.sway.wait(timeout=3)
                except Exception:
                    try:
                        self.sway.kill()
                    except Exception:
                        pass
        self.sway = None
        # Xwayland is gone by now; skip XCloseDisplay so Xlib does not hit an I/O error.
        self.private_handle = None
        if self.runtime_dir is not None:
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            self.runtime_dir = None
        if self.xwayland_wrapper is not None:
            try:
                self.xwayland_wrapper.unlink()
            except Exception:
                pass
            self.xwayland_wrapper = None

    def health_problem(self) -> Optional[str]:
        """Why the private display can no longer mirror the desktop, else None."""
        if self.sway is None or self.sway.poll() is not None:
            return "the private compositor exited"
        for name, thread in (
            ("window placement", self.placer),
            ("clipboard", self.x_clipboard),
            ("virtual input", self.seat),
        ):
            if thread is not None and not thread.is_alive():
                return f"the private display's {name} helper stopped"
        return None

    def stop(self) -> None:
        if self.placer is not None:
            self.placer.stop_event.set()
            self.placer.join(timeout=2)
        if self.x_clipboard is not None:
            self.x_clipboard.stop_event.set()
            try:
                os.write(self.x_clipboard.wake_w, b"x")
            except Exception:
                pass
            self.x_clipboard.join(timeout=2)
        if self.seat is not None:
            self.seat.stop()
        self._teardown_compositor()


def _compositor_exit_reason(compositor_log: str) -> str:
    try:
        text = Path(compositor_log).read_text(encoding="utf-8", errors="replace")[-20000:]
    except Exception:
        text = ""
    if "Proprietary Nvidia drivers are NOT supported" in text:
        return "sway refused to run with NVIDIA's proprietary driver, which the private display needs"
    return f"the compositor exited during startup (see {compositor_log})"


def _watch_health(supervisor: Supervisor, stop_event: threading.Event) -> None:
    """Report a failed compositor or helper once; the app then closes the hidden browser."""
    while not stop_event.wait(1.0):
        problem = supervisor.health_problem()
        if problem and not stop_event.is_set():
            _emit("fatal", message=problem)
            return


def serve() -> int:
    supervisor = Supervisor()
    started = False
    health_stop = threading.Event()
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                request = json.loads(raw)
            except Exception:
                continue
            cmd = request.get("cmd")
            if cmd == "start" and not started:
                try:
                    info = supervisor.start(request)
                except Exception as exc:
                    _emit("fatal", message=str(exc))
                    return 1
                started = True
                _emit("ready", **info)
                threading.Thread(
                    target=_watch_health, args=(supervisor, health_stop), name="irp-bg-health", daemon=True
                ).start()
            elif cmd == "clipboard":
                _emit("reply", id=request.get("id"), **supervisor.set_clipboard(str(request.get("text") or "")))
            elif cmd == "maximize_hint" and supervisor.placer is not None:
                supervisor.placer.add_maximize_hint(str(request.get("class_contains") or ""))
            elif cmd == "mark_launch":
                supervisor.mark_launch()
            elif cmd == "settle":
                _emit("reply", id=request.get("id"), **supervisor.settle(float(request.get("timeout") or 4.0)))
            elif cmd == "stop":
                break
    finally:
        health_stop.set()
        supervisor.stop()
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv[1:]:
        sys.exit(serve())
    print("This module is started by IntenseRP (Background Mode).")
