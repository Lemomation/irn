"""Refresh-rate matching for the private Xwayland.

Xwayland builds its X11 mode lines with libxcvt from the Wayland output's refresh rate,
and Chromium paces requestAnimationFrame from that mode line. Two strategies:

* exact: a tiny interposer preloaded into the private Xwayland only returns the
  desktop's real mode line (needs a C compiler once; the result is cached), and
* nearest: pick the Wayland refresh whose CVT mode lands closest to the desktop's
  rate (CVT rounds the pixel clock to 250 kHz, so typically within a few 0.01 Hz).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from utils.background_mode.common import system_env
from utils.background_mode.ipc import log as _log


# Xwayland builds its X11 mode lines with libxcvt (CVT formula, 250 kHz clock steps), so
# a 60.000 Hz Wayland output becomes e.g. a 59.80 Hz X11 mode, and Chromium paces
# requestAnimationFrame from that mode line. This tiny interposer, preloaded into the private
# Xwayland only, returns the desktop's real mode line for the exact (size, refresh) we ask for
# and defers to libxcvt for everything else.
_XCVT_SHIM_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct libxcvt_mode_info {
    uint32_t hdisplay; uint32_t vdisplay; float vrefresh; float hsync; uint64_t dot_clock;
    uint16_t hsync_start; uint16_t hsync_end; uint16_t htotal;
    uint16_t vsync_start; uint16_t vsync_end; uint16_t vtotal; uint32_t mode_flags;
};
typedef struct libxcvt_mode_info *(*gen_fn)(int, int, float, bool, bool);

struct libxcvt_mode_info *
libxcvt_gen_mode_info(int hdisplay, int vdisplay, float vrefresh, bool reduced, bool interlaced)
{
    static gen_fn real_fn = NULL;
    if (!real_fn)
        real_fn = (gen_fn) dlsym(RTLD_NEXT, "libxcvt_gen_mode_info");
    /* IRP_XCVT_MODES="w,h,refresh_mhz,clock_khz,hss,hse,htotal,vss,vse,vtotal,flags;..." */
    const char *p = getenv("IRP_XCVT_MODES");
    if (p && !interlaced) {
        int want_mhz = (int) (vrefresh * 1000.0f + 0.5f);
        while (*p) {
            int w, h, mhz, hss, hse, ht, vss, vse, vt;
            unsigned long long clk;
            unsigned int flags;
            if (sscanf(p, "%d,%d,%d,%llu,%d,%d,%d,%d,%d,%d,%u",
                       &w, &h, &mhz, &clk, &hss, &hse, &ht, &vss, &vse, &vt, &flags) == 11 &&
                w == hdisplay && h == vdisplay && mhz == want_mhz && ht > 0 && vt > 0) {
                struct libxcvt_mode_info *m = calloc(1, sizeof *m);
                if (!m)
                    break;
                m->hdisplay = (uint32_t) w; m->vdisplay = (uint32_t) h; m->dot_clock = (uint64_t) clk;
                m->hsync_start = (uint16_t) hss; m->hsync_end = (uint16_t) hse; m->htotal = (uint16_t) ht;
                m->vsync_start = (uint16_t) vss; m->vsync_end = (uint16_t) vse; m->vtotal = (uint16_t) vt;
                m->mode_flags = flags;
                m->hsync = (float) clk / (float) ht;
                m->vrefresh = (float) (1000.0 * (double) clk / ((double) ht * (double) vt));
                return m;
            }
            p = strchr(p, ';');
            if (!p)
                break;
            p++;
        }
    }
    return real_fn ? real_fn(hdisplay, vdisplay, vrefresh, reduced, interlaced) : NULL;
}
"""


def build_xcvt_shim(directory: Path) -> Optional[Path]:
    """Compile (once, cached) the Xwayland mode-line shim. None when no C compiler is available."""
    import hashlib
    import platform

    digest = hashlib.sha256(_XCVT_SHIM_SOURCE.encode()).hexdigest()[:12]
    target = directory / f"xcvt_exact-{digest}-{platform.machine()}.so"
    if target.exists():
        return target
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        _log("no C compiler found; matching the refresh rate with the nearest CVT mode instead", "info")
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / f"xcvt_exact-{digest}.c"
        source.write_text(_XCVT_SHIM_SOURCE, encoding="utf-8")
        tmp = directory / f".{target.name}.{os.getpid()}.tmp"
        result = subprocess.run(
            [compiler, "-shared", "-fPIC", "-O2", "-o", str(tmp), str(source), "-ldl"],
            capture_output=True,
            text=True,
            timeout=60,
            env=system_env(),
        )
        if result.returncode != 0 or not tmp.exists():
            _log(f"could not build the Xwayland mode-line shim: {result.stderr.strip()[:300]}", "warning")
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(target)
        return target
    except Exception as exc:
        _log(f"could not build the Xwayland mode-line shim: {exc}", "warning")
        return None


def exact_mode_spec(output: dict[str, Any]) -> Optional[str]:
    """Shim spec for this output's mode line, with a {mhz} placeholder for the Wayland refresh."""
    modeline = output.get("modeline")
    if not isinstance(modeline, dict):
        return None
    try:
        width, height = int(output["width"]), int(output["height"])
        h, v = [int(x) for x in modeline["h"]], [int(x) for x in modeline["v"]]
        clock, flags = int(modeline["clock_khz"]), int(modeline.get("flags") or 0)
    except Exception:
        return None
    if len(h) != 4 or len(v) != 4 or h[0] != width or v[0] != height or flags & 16 or clock <= 0:
        return None
    return f"{width},{height},{{mhz}},{clock},{h[1]},{h[2]},{h[3]},{v[1]},{v[2]},{v[3]},{flags}"


class XCVT:
    """Xwayland builds its X11 modes with libxcvt from the Wayland output's refresh."""

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        class Info(ctypes.Structure):
            _fields_ = [
                ("hdisplay", ctypes.c_uint32), ("vdisplay", ctypes.c_uint32),
                ("vrefresh", ctypes.c_float), ("hsync", ctypes.c_float),
                ("dot_clock", ctypes.c_uint64),
                ("hsync_start", ctypes.c_uint16), ("hsync_end", ctypes.c_uint16), ("htotal", ctypes.c_uint16),
                ("vsync_start", ctypes.c_uint16), ("vsync_end", ctypes.c_uint16), ("vtotal", ctypes.c_uint16),
                ("mode_flags", ctypes.c_uint32),
            ]

        lib = ctypes.CDLL(ctypes.util.find_library("xcvt") or "libxcvt.so.0")
        lib.libxcvt_gen_mode_info.restype = ctypes.POINTER(Info)
        lib.libxcvt_gen_mode_info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_bool, ctypes.c_bool]
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        libc.free.argtypes = [ctypes.c_void_p]
        self.lib, self.libc, self.ct = lib, libc, ctypes

    def x11_refresh(self, width: int, height: int, refresh_mhz: int) -> float:
        ptr = self.lib.libxcvt_gen_mode_info(width, height, refresh_mhz / 1000.0, False, False)
        if not ptr:
            return 0.0
        info = ptr.contents
        value = (info.dot_clock * 1000.0) / float(info.htotal * info.vtotal) if info.htotal and info.vtotal else 0.0
        self.libc.free(self.ct.cast(ptr, self.ct.c_void_p))
        return value


def pick_output_refresh(width: int, height: int, target_hz: float) -> tuple[int, Optional[float]]:
    """
    Choose the compositor refresh (mHz) whose Xwayland CVT mode is closest to target_hz.

    CVT rounds the pixel clock to 250 kHz, so asking for exactly 60 Hz typically yields
    59.80 Hz. Searching the input rate gets within one CVT step instead.
    """
    nominal = int(round(target_hz * 1000))
    try:
        cvt = XCVT()
    except Exception:
        return nominal, None
    best_mhz, best_hz, best_err = nominal, None, float("inf")
    for mhz in range(max(1000, nominal - 3000), nominal + 3001):
        achieved = cvt.x11_refresh(width, height, mhz)
        err = abs(achieved - target_hz)
        if err < best_err or (err == best_err and abs(mhz - nominal) < abs(best_mhz - nominal)):
            best_mhz, best_hz, best_err = mhz, achieved, err
    return best_mhz, best_hz
