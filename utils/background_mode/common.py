"""Constants and small helpers shared by the app side and the supervisor process.

Keep this module free of Qt/Playwright imports: the supervisor imports it too.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

PROTOCOL_PREFIX = "@@IRP-BG@@ "
SNAPSHOT_VERSION = 2
STATE_DIR_NAME = "background_mode"
SNAPSHOT_FILE = "desktop_snapshot.json"
CALIBRATION_FILE = "calibration.json"
SUPERVISOR_LOG_FILE = "supervisor.log"
COMPOSITOR_LOG_FILE = "compositor.log"
SHIM_DIR_NAME = "xwayland"

SESSION_X11 = "x11"
SESSION_WAYLAND = "wayland"
SESSION_WINDOWS = "windows"

SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

# WebGL renderer substrings that mean "no real GPU". Running hidden on one of these
# would change the WebGL/canvas fingerprint, so Background Mode refuses.
SOFTWARE_RENDERER_MARKERS: tuple[str, ...] = (
    "swiftshader",
    "llvmpipe",
    "softpipe",
    "lavapipe",
    "software rasterizer",
    "software adapter",
    "microsoft basic render",
)


class BackgroundModeUnavailable(RuntimeError):
    """Background Mode cannot run here with a fingerprint identical to visible mode."""


def state_dir(config_dir: str | Path | None) -> Path:
    base = Path(config_dir) if config_dir else Path(tempfile.gettempdir()) / "intenserp"
    path = base / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_desktop_snapshot(config_dir: str | Path | None) -> Optional[dict[str, Any]]:
    data = read_json(state_dir(config_dir) / SNAPSHOT_FILE)
    if not data or data.get("version") != SNAPSHOT_VERSION or data.get("degraded"):
        return None
    return data


def save_desktop_snapshot(config_dir: str | Path | None, snapshot: dict[str, Any]) -> None:
    write_json(state_dir(config_dir) / SNAPSHOT_FILE, snapshot)


def is_software_renderer(renderer: str | None) -> bool:
    text = str(renderer or "").lower()
    return any(marker in text for marker in SOFTWARE_RENDERER_MARKERS)


def system_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """
    Environment for system tools (sway, Xwayland, xrandr, cc...).

    Frozen PyInstaller builds point LD_LIBRARY_PATH at the bundled libraries, which
    system binaries must not load. PyInstaller keeps the original in *_ORIG.
    """
    env = dict(os.environ if base is None else base)
    if "LD_LIBRARY_PATH_ORIG" in env:
        original = env.pop("LD_LIBRARY_PATH_ORIG")
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env
