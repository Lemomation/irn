"""
Background Mode: run the provider browser hidden (a private display on Linux, concealed
windows on Windows), with nothing a page can tell apart from the visible browser.

Why it is built this way
------------------------
Background Mode has to look exactly like the normal visible browser to the pages it
loads. That rules out two easy options:

* Chromium headless mode uses different code paths and leaks through scrollbars,
  window metrics, pointer/hover media queries and more.
* Xvfb renders in software, so the WebGL and canvas fingerprints change.

On Linux the same headed Chromium, with the same flags, runs on a private sway
compositor (wlroots ``headless`` backend) that renders on the real GPU, and everything a
page can observe about the desktop is mirrored from the user's session:

* X11 desktops (``x11.py``): screen layout and exact refresh rate, work area, X
  resources, XSETTINGS (theme, fonts, scaling), keyboard layout, and the window
  manager's placement rule.
* Wayland desktops (``wayland.py``): output modes, exact refresh rate, scale and layout,
  the seat's input devices (virtual pointer/keyboard with the desktop's exact keymap).

Calibration (``calibration.py``) records what a page sees in one visible run and
requires every hidden run to match before a provider page loads; anything that does not
match falls back to the normal visible browser.

The work happens in a supervisor subprocess (``supervisor.py``) so Xlib can never take
the app down, and the compositor dies with the app. The app side lives in ``client.py``
and ``controller.py``.

On Windows (``windows.py``) there is no private display: the same browser stays on the
user's desktop and its windows are made transparent, click-through and unfocusable from
the moment they are created, so there is nothing to mirror. macOS is not supported: it
throttles hidden windows to about one frame per second while pages still report
themselves as visible.
"""

from utils.background_mode.common import BackgroundModeUnavailable
from utils.background_mode.controller import BackgroundModeSession

__all__ = ["BackgroundModeSession", "BackgroundModeUnavailable"]


def _bundle_for_frozen_builds() -> None:  # pragma: no cover - never called
    # PyInstaller only bundles modules it sees imported. The supervisor runs as
    # "<app> -m utils.background_mode.supervisor" in frozen builds, so name it here.
    from utils.background_mode import supervisor, wayland, x11, xcvt  # noqa: F401
