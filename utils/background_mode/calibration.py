"""
Calibration: record what a page can see of the desktop in a visible run, then require a
hidden run to show exactly the same before any provider page loads.

The probe runs on the browser's first, still blank page in an isolated world, so no
site can observe it. Refresh rate is not probed here (the frame clock is not settled
that early); the supervisor verifies it on the private display instead. Calibration is what makes Background Mode safe on desktops it was
never tested on: anything the private display cannot reproduce (a touchscreen, a tiling
window manager, an unusual compositor) shows up as a mismatch, and the start falls
back to the normal visible browser instead of running hidden with a different
fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from utils.background_mode.common import CALIBRATION_FILE, read_json, state_dir, write_json

CALIBRATION_VERSION = 1
MAX_DESKTOPS = 8
MAX_WINDOW_RECORDS = 32

# Wait until the window is really on screen and its metrics have settled. On Wayland,
# requestAnimationFrame only ticks once the window is mapped, and a fractional scale is
# applied at that point, so probing earlier would read pre-map values.
SETTLE_JS = """async () => {
    const frames = await new Promise((resolve) => {
        let count = 0;
        const step = () => { if (++count >= 10) resolve(count); else requestAnimationFrame(step); };
        requestAnimationFrame(step);
        setTimeout(() => resolve(count), 8000);
    });
    const snap = () => [devicePixelRatio, outerWidth, outerHeight, innerWidth, innerHeight, screen.width, screen.height].join(',');
    let last = snap();
    let since = performance.now();
    const start = performance.now();
    while (performance.now() - start < 4000) {
        await new Promise((r) => setTimeout(r, 100));
        const now = snap();
        if (now !== last) { last = now; since = performance.now(); }
        else if (performance.now() - since >= 600) return { frames, stable: true };
    }
    return { frames, stable: false };
}"""

PROBE_JS = """async () => {
    const out = {};
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        if (gl) {
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            out.gpuVendor = String(ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR));
            out.gpuRenderer = String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
            const lose = gl.getExtension('WEBGL_lose_context');
            if (lose) lose.loseContext();
        }
    } catch (e) {
        out.gpuError = String(e);
    }
    const s = screen;
    out.screen = [s.width, s.height, s.availWidth, s.availHeight, s.availLeft, s.availTop, s.colorDepth, s.pixelDepth];
    out.dpr = devicePixelRatio;
    const queries = [
        '(pointer: fine)', '(pointer: coarse)', '(pointer: none)', '(hover: hover)', '(hover: none)',
        '(any-pointer: fine)', '(any-pointer: coarse)', '(any-hover: hover)', '(prefers-color-scheme: dark)',
        '(prefers-reduced-motion: reduce)', '(prefers-contrast: more)', '(forced-colors: active)',
        '(color-gamut: p3)', '(dynamic-range: high)', '(inverted-colors: inverted)',
    ];
    out.media = queries.filter((q) => matchMedia(q).matches);
    out.touch = navigator.maxTouchPoints;
    try {
        const root = document.documentElement;
        const box = document.createElement('div');
        box.style.cssText = 'position:absolute;top:-9999px;width:100px;height:100px;overflow:scroll';
        root.appendChild(box);
        out.scrollbar = [box.offsetWidth - box.clientWidth, box.offsetHeight - box.clientHeight];
        box.remove();
        const text = document.createElement('span');
        text.style.cssText = 'position:absolute;top:-9999px;font:16px system-ui;white-space:nowrap';
        text.textContent = 'Hamburgefonstiv 0123456789';
        root.appendChild(text);
        const rect = text.getBoundingClientRect();
        out.systemFont = [Math.round(rect.width * 100) / 100, Math.round(rect.height * 100) / 100];
        text.remove();
    } catch (e) {}
    try {
        if (navigator.keyboard && navigator.keyboard.getLayoutMap) {
            const map = await navigator.keyboard.getLayoutMap();
            out.keyboard = [...map.entries()].map((e) => e.join('=')).sort().join(',');
        }
    } catch (e) {}
    out.visibility = [document.visibilityState, document.hidden, document.hasFocus()];
    // Frame liveness: a hidden window the OS throttles (macOS app hiding drops to ~1 fps)
    // still reports "visible", so count frames instead of trusting the visibility state.
    out.framesAlive = await new Promise((resolve) => {
        let n = 0;
        const step = () => { n++; requestAnimationFrame(step); };
        requestAnimationFrame(step);
        setTimeout(() => resolve(n >= 15), 600);
    });
    out.window = [screenX, screenY, outerWidth, outerHeight, innerWidth, innerHeight];
    return out;
}"""

# What must match exactly between a visible baseline and a hidden run.
_INTRINSIC = {
    "gpuVendor": "GPU vendor",
    "gpuRenderer": "GPU",
    "screen": "screen size",
    "dpr": "display scale",
    "media": "pointer/theme media queries",
    "touch": "touch points",
    "scrollbar": "scrollbar size",
    "systemFont": "system font metrics",
    # navigator.keyboard is not available on the blank page today; the layout is verified
    # by the supervisor instead. Kept so it is compared if Chromium starts exposing it.
    "keyboard": "keyboard layout",
    "visibility": "page visibility and focus",
    "framesAlive": "frame rendering",
}


def desktop_signature(snapshot: dict[str, Any]) -> str:
    """Identify a desktop setup; each one gets its own baseline."""
    parts = {
        "session": snapshot.get("session_type"),
        "wm": snapshot.get("wm_name") or snapshot.get("desktop") or "",
        "outputs": [
            [o.get("width"), o.get("height"), o.get("x"), o.get("y"), round(float(o.get("refresh") or 0), 3),
             round(float(o.get("scale") or 1), 4)]
            for o in snapshot.get("outputs") or []
        ],
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def profile_key(user_data_dir: str) -> str:
    return hashlib.sha256(str(user_data_dir).encode()).hexdigest()[:16]


def _load(config_dir) -> dict[str, Any]:
    data = read_json(state_dir(config_dir) / CALIBRATION_FILE) or {}
    if data.get("version") != CALIBRATION_VERSION:
        data = {"version": CALIBRATION_VERSION, "desktops": {}, "windows": {}}
    data.setdefault("desktops", {})
    data.setdefault("windows", {})
    return data


def _save(config_dir, data: dict[str, Any]) -> None:
    for key, limit in (("desktops", MAX_DESKTOPS), ("windows", MAX_WINDOW_RECORDS)):
        entries = data.get(key) or {}
        if len(entries) > limit:
            newest = sorted(entries.items(), key=lambda kv: kv[1].get("recorded_at", 0), reverse=True)[:limit]
            data[key] = dict(newest)
    write_json(state_dir(config_dir) / CALIBRATION_FILE, data)


def load_baseline(config_dir, signature: str, viewport_mode: str) -> Optional[dict[str, Any]]:
    """The visible-run probe for this desktop and viewport mode (Playwright emulates the
    screen in the fixed-viewport mode, so the two modes look different to pages)."""
    entry = (_load(config_dir).get("desktops") or {}).get(signature) or {}
    return (entry.get("probes") or {}).get(viewport_mode)


def save_baseline(config_dir, signature: str, viewport_mode: str, probe: dict[str, Any]) -> None:
    data = _load(config_dir)
    entry = data["desktops"].get(signature) or {}
    entry.setdefault("probes", {})[viewport_mode] = probe
    entry["recorded_at"] = time.time()
    data["desktops"][signature] = entry
    _save(config_dir, data)


def load_window_policy(config_dir, signature: str, viewport_mode: str) -> Optional[dict[str, Any]]:
    """How the desktop treated the browser window in a visible run for this viewport mode."""
    entry = (_load(config_dir).get("desktops") or {}).get(signature) or {}
    return (entry.get("window_policy") or {}).get(viewport_mode)


def save_window_policy(config_dir, signature: str, viewport_mode: str, policy: dict[str, Any]) -> None:
    data = _load(config_dir)
    entry = data["desktops"].get(signature)
    if entry is None:
        return
    entry.setdefault("window_policy", {})[viewport_mode] = policy
    _save(config_dir, data)


def load_fresh_window(config_dir, signature: str, viewport_mode: str) -> Optional[list]:
    """Where a never-used profile's window ends up on this desktop (Chromium's default placement)."""
    entry = (_load(config_dir).get("desktops") or {}).get(signature) or {}
    return (entry.get("fresh_window") or {}).get(viewport_mode)


def save_fresh_window(config_dir, signature: str, viewport_mode: str, window: list) -> None:
    data = _load(config_dir)
    entry = data["desktops"].get(signature)
    if entry is None:
        return
    entry.setdefault("fresh_window", {})[viewport_mode] = window
    _save(config_dir, data)


def load_window_record(config_dir, user_data_dir: str) -> Optional[dict[str, Any]]:
    return (_load(config_dir).get("windows") or {}).get(profile_key(user_data_dir))


def save_window_record(config_dir, user_data_dir: str, record: dict[str, Any]) -> None:
    data = _load(config_dir)
    data["windows"][profile_key(user_data_dir)] = {**record, "recorded_at": time.time()}
    _save(config_dir, data)


def compare_probes(baseline: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Human-readable differences between a visible baseline and a hidden probe."""
    mismatches = []
    for key, label in _INTRINSIC.items():
        if key not in baseline and key in ("visibility", "framesAlive"):
            continue  # Recorded by newer versions only; checked once the baseline is refreshed.
        if baseline.get(key) != probe.get(key):
            mismatches.append(f"{label} ({probe.get(key)!r} instead of {baseline.get(key)!r})")
    return mismatches
