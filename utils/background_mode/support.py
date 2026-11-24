"""Which desktop are we on, and can Background Mode run here?"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from utils.background_mode.common import (
    SESSION_WAYLAND,
    SESSION_X11,
    load_desktop_snapshot,
)


def detect_session() -> Optional[str]:
    """
    The display server visible-mode Chromium uses right now, or None without a desktop.

    Chromium (148, ozone platform "auto") runs natively on Wayland only when
    XDG_SESSION_TYPE is "wayland" and a Wayland display is set; otherwise it uses X11 via
    DISPLAY, even inside a Wayland session. Background Mode mirrors that same choice.
    """
    if not sys.platform.startswith("linux"):
        return None
    session_type = str(os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session_type == SESSION_WAYLAND and (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("WAYLAND_SOCKET")):
        return SESSION_WAYLAND
    if os.environ.get("DISPLAY"):
        return SESSION_X11
    return None


def wayland_socket_path() -> Optional[str]:
    """Absolute path of the user's Wayland socket (WAYLAND_DISPLAY may be a name or a path)."""
    name = os.environ.get("WAYLAND_DISPLAY") or ""
    if not name:
        return None
    if name.startswith("/"):
        return name
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not runtime:
        return None
    return str(Path(runtime) / name)


def can_open_visible_browser() -> bool:
    """True when a normal, visible browser window can be opened (Background Mode's fallback)."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("WAYLAND_SOCKET")
    )


# Package per tool for each _INSTALLERS entry.
_INSTALLERS = (("Debian/Ubuntu/Mint", "sudo apt install"), ("Fedora", "sudo dnf install"), ("Arch", "sudo pacman -S"))
_PACKAGES = {
    "sway": ("sway", "sway", "sway"),
    "swaymsg": ("sway", "sway", "sway"),
    "Xwayland": ("xwayland", "xorg-x11-server-Xwayland", "xorg-xwayland"),
    "xrandr": ("x11-xserver-utils", "xrandr", "xorg-xrandr"),
    "setxkbmap": ("x11-xkb-utils", "setxkbmap", "xorg-setxkbmap"),
}


def check_background_mode_support(config_dir=None) -> tuple[bool, str]:
    """Cheap host checks before starting anything. Returns (supported, reason_if_not)."""
    if sys.platform == "win32":
        return True, ""
    if sys.platform == "darwin":
        return False, (
            "Background Mode is not available on macOS: macOS slows down hidden browser windows "
            "(about one frame per second), which pages can measure."
        )
    if not sys.platform.startswith("linux"):
        return False, "Background Mode is not available on this system."

    session = detect_session()
    if session is None:
        saved = load_desktop_snapshot(config_dir)
        if not saved:
            return False, (
                "there is no desktop to mirror yet. Start a provider once from your normal "
                "desktop session with Background Mode on, so IntenseRP can record your screen, "
                "fonts, theme and keyboard settings."
            )
        session = str(saved.get("session_type") or SESSION_X11)
    if session == SESSION_WAYLAND and os.environ.get("WAYLAND_SOCKET") and not os.environ.get("WAYLAND_DISPLAY"):
        return False, "IntenseRP was started with a pre-connected Wayland socket, which cannot be inspected."

    needed = ["sway", "swaymsg"]
    if session == SESSION_X11:
        needed += ["Xwayland", "xrandr", "setxkbmap"]
    missing = [name for name in needed if not shutil.which(name)]
    if missing:
        hints = "; ".join(
            f"{distro}: {command} {' '.join(dict.fromkeys(_PACKAGES[name][i] for name in missing))}"
            for i, (distro, command) in enumerate(_INSTALLERS)
        )
        return False, f"missing {', '.join(missing)}. Install them ({hints})."

    render_nodes = sorted(glob.glob("/dev/dri/renderD*"))
    if not render_nodes:
        return False, "no GPU render node (/dev/dri/renderD*) was found."
    if not any(os.access(node, os.R_OK | os.W_OK) for node in render_nodes):
        return False, (
            "your user cannot access the GPU render node (/dev/dri/renderD*). Add yourself "
            "to the 'render' group (sudo usermod -aG render $USER) and log in again."
        )
    return True, ""
