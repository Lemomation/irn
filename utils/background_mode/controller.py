"""Per-driver Background Mode lifecycle, kept out of BaseDriver so the driver only calls hooks."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from utils.background_mode.calibration import (
    PROBE_JS,
    SETTLE_JS,
    compare_probes,
    desktop_signature,
    load_baseline,
    load_fresh_window,
    load_window_policy,
    load_window_record,
    save_baseline,
    save_fresh_window,
    save_window_policy,
    save_window_record,
)
from utils.background_mode.client import acquire_private_display, release_private_display
from utils.background_mode.common import (
    SESSION_WAYLAND,
    SESSION_WINDOWS,
    BackgroundModeUnavailable,
    is_software_renderer,
)
from utils.background_mode.support import can_open_visible_browser
from utils.logger import Logger

if TYPE_CHECKING:
    from drivers.base_driver import BaseDriver

StatusCallback = Optional[Callable[[str], None]]

# Safety net for login paths without an explicit login_needed() hook.
LOGIN_TIMEOUT_S = 300.0

# Playwright's headed Chromium window = viewport + these insets on Linux (+46 px in
# persistent contexts for the infobar area), see crPage.updateViewport in Playwright.
_PLAYWRIGHT_LINUX_WINDOW_INSETS = (8, 85 + 46)
_PLAYWRIGHT_DEFAULT_VIEWPORT = (1280, 720)

# One hidden launch at a time, process-wide: settle() and the probe wait for "the new
# window", which parallel provider starts would otherwise confuse. (A threading lock,
# polled, because parallel drivers may run on different event loops.)
_LAUNCH_SLOT = threading.Lock()
# A hidden start that waited this long for the slot opens visibly instead.
_LAUNCH_SLOT_WAIT_S = 120.0


class BackgroundModeSession:
    """Background Mode state for one provider driver."""

    def __init__(self, driver: "BaseDriver"):
        self.driver = driver
        # client.PrivateDisplay (Linux) or windows.WindowsDisplay.
        self.display: Optional[Any] = None
        self.active = False
        self.enabled_for_start = False
        self.calibrating = False
        self.signature: Optional[str] = None
        self.session_type: Optional[str] = None
        self.probe: Optional[dict[str, Any]] = None
        self.user_data_dir: Optional[str] = None
        self.placement_before: Optional[dict[str, Any]] = None
        self.login_event: Optional[asyncio.Event] = None
        self.login_reason: Optional[str] = None
        self.login_notice_sent = False
        self._holds_launch_slot = False
        self._health_task: Optional[asyncio.Task] = None

    # -- helpers -----------------------------------------------------------

    @property
    def _label(self) -> str:
        return self.driver.provider_label

    @property
    def _config_dir(self):
        return getattr(self.driver.config_manager, "config_dir", None)

    def _setting(self, key: str) -> bool:
        try:
            return bool(self.driver.config_manager.get_setting("system_settings", key))
        except Exception:
            return False

    def enabled(self) -> bool:
        return self._setting("browser_background_mode")

    def _viewport_mode(self) -> str:
        return "follow" if self._setting("browser_resize_viewport_with_window") else "fixed"

    async def _window_bounds(self) -> Optional[dict[str, Any]]:
        """The browser window's real bounds and state, from Chromium (not visible to pages)."""
        context, page = self.driver.context, self.driver.page
        if context is None or page is None:
            return None
        session = None
        try:
            session = await context.new_cdp_session(page)
            info = await session.send("Browser.getWindowForTarget")
            bounds = info.get("bounds") if isinstance(info, dict) else None
            return bounds if isinstance(bounds, dict) else None
        except Exception as e:
            Logger.debug(f"{self._label}: could not read the browser window bounds: {e}")
            return None
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    pass

    async def _restore_wayland_origin(self) -> None:
        """
        Wayland clients never learn their position, so Chromium reports the origin it asked
        for (the saved placement, or 10 px into the screen). The private sway briefly tiles
        new windows, which resets that origin to 0,0; put Chromium's own origin back.
        """
        placement = self.placement_before or {}
        fresh = load_fresh_window(self._config_dir, self.signature or "", self._viewport_mode())
        if placement and not placement.get("maximized") and placement.get("left") is not None:
            left, top = int(placement.get("left") or 0), int(placement.get("top") or 0)
        elif fresh:
            # A never-used profile: use the origin the desktop gave one during calibration
            # (GNOME reports either 0,0 or Chromium's 10,10 for new windows).
            left, top = int(fresh[0]), int(fresh[1])
        else:
            outputs = (self.display.snapshot.get("outputs") if self.display else None) or [{}]
            left, top = int(outputs[0].get("x") or 0) + 10, int(outputs[0].get("y") or 0) + 10
        context, page = self.driver.context, self.driver.page
        if context is None or page is None:
            return
        session = None
        try:
            session = await context.new_cdp_session(page)
            # The compositor's configure sequence (tiled, then floating) can arrive after the
            # page exists and reset the origin again, so repeat until it holds.
            steady = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 4.0
            while loop.time() < deadline and steady < 3:
                info = await session.send("Browser.getWindowForTarget")
                bounds = info.get("bounds") or {}
                if bounds.get("windowState") != "normal":
                    return
                if (bounds.get("left"), bounds.get("top")) == (left, top):
                    steady += 1
                else:
                    steady = 0
                    await session.send(
                        "Browser.setWindowBounds",
                        {"windowId": info["windowId"], "bounds": {"left": left, "top": top}},
                    )
                await asyncio.sleep(0.15)
        except Exception as e:
            Logger.debug(f"{self._label}: could not restore the window origin: {e}")
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    pass

    def _watch_wayland_popups(self) -> None:
        """
        On a Wayland mirror, new windows (e.g. a Google sign-in popup) cannot be reproduced
        exactly: sway briefly tiles every new window, so Chromium loses the origin and drop
        shadows it shows on GNOME/KDE. Popups during sign-in therefore reopen the browser
        visibly for that session; later ones are logged.
        """
        context = self.driver.context
        if context is None:
            return

        async def fall_back_after_init(page) -> None:
            # Cancelling sign-in while Playwright is still setting up the new page crashes the
            # Patchright driver (unhandled "session closed" in Node), so let it finish first.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.3)
            self.login_needed("a sign-in window opened, which Background Mode cannot reproduce exactly on Wayland")

        def on_page(page) -> None:
            if not self.active:
                return
            if self.login_event is not None:
                try:
                    asyncio.get_running_loop().create_task(fall_back_after_init(page))
                except Exception:
                    self.login_needed("a sign-in window opened, which Background Mode cannot reproduce exactly on Wayland")
            elif not self.login_notice_sent:
                self.login_notice_sent = True
                Logger.warning(
                    f"{self._label}: a new browser window opened while hidden on a Wayland desktop; its "
                    "position and frame may differ from your normal browser."
                )

        try:
            context.on("page", on_page)
        except Exception as e:
            Logger.debug(f"{self._label}: could not watch new windows: {e}")

    async def _learn_window_policy(self) -> None:
        """
        Visible runs: learn whether the desktop resizes or maximizes the browser window itself.

        GNOME, for example, maximizes windows that are almost as large as the screen, and
        tiling window managers size every window. The private display cannot reproduce that,
        so such desktops only run hidden in a viewport mode where the browser keeps its size.
        """
        if not self.signature:
            return
        bounds = await self._window_bounds()
        if not bounds:
            return
        state = str(bounds.get("windowState") or "normal")
        size = [int(bounds.get("width") or 0), int(bounds.get("height") or 0)]
        mode = self._viewport_mode()
        if self.session_type == SESSION_WAYLAND and state in ("maximized", "fullscreen"):
            # The private display cannot give Chromium a maximized window on Wayland, whether
            # the user or the desktop (GNOME auto-maximize) maximized it.
            save_window_policy(
                self._config_dir,
                self.signature,
                mode,
                {"imposed": True, "size": size, "state": state, "persistent": self.user_data_dir is not None},
            )
            return
        if (self.placement_before or {}).get("maximized"):
            # Chromium restored a maximized window, so this run says nothing about whether
            # the desktop resizes windows on its own. Keep what an earlier run learned, or
            # record "unknown" so calibration finishes instead of repeating every start.
            if load_window_policy(self._config_dir, self.signature, mode) is None:
                save_window_policy(
                    self._config_dir,
                    self.signature,
                    mode,
                    {"imposed": None, "size": size, "state": state, "persistent": self.user_data_dir is not None},
                )
            return
        imposed = state in ("maximized", "fullscreen")
        persistent = self.user_data_dir is not None
        if mode == "fixed":
            expected = [
                _PLAYWRIGHT_DEFAULT_VIEWPORT[0] + _PLAYWRIGHT_LINUX_WINDOW_INSETS[0],
                _PLAYWRIGHT_DEFAULT_VIEWPORT[1] + (_PLAYWRIGHT_LINUX_WINDOW_INSETS[1] if persistent else 85),
            ]
            imposed = imposed or size != expected
        save_window_policy(
            self._config_dir,
            self.signature,
            mode,
            {"imposed": imposed, "size": size, "state": state, "persistent": persistent},
        )

    def _notify(self, title: str, message: str, details: str) -> None:
        self.driver.notify_user(f"{self._label}: {title}", message, level="warning", dialog_message=details)

    @staticmethod
    def _read_placement(user_data_dir: str) -> Optional[dict[str, Any]]:
        try:
            data = json.loads((Path(user_data_dir) / "Default" / "Preferences").read_text(encoding="utf-8"))
        except Exception:
            return None
        placement = (data.get("browser") or {}).get("window_placement") if isinstance(data, dict) else None
        return dict(placement) if isinstance(placement, dict) else None

    @staticmethod
    def _release_now(display) -> None:
        if getattr(display, "session_type", None) == SESSION_WINDOWS:
            display.stop()
        else:
            release_private_display(display)

    async def _acquire_display(self):
        if sys.platform != "win32":
            return await acquire_private_display(self._config_dir)
        try:
            from utils.background_mode.windows import open_windows_display
        except Exception as exc:
            raise BackgroundModeUnavailable(f"the window concealer could not load ({exc}).") from exc

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, open_windows_display, self.driver.playwright)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # The start was cancelled while the concealer was starting: stop it once it is up.
            future.add_done_callback(
                lambda f: f.cancelled() or f.exception() is not None or self._release_in_thread(f.result())
            )
            raise
        except Exception as exc:
            raise BackgroundModeUnavailable(f"the window concealer could not start ({exc}).") from exc

    def _release_in_thread(self, display) -> None:
        threading.Thread(target=self._release_now, args=(display,), name="irp-bg-release", daemon=True).start()

    async def _take_launch_slot(self) -> bool:
        deadline = asyncio.get_running_loop().time() + _LAUNCH_SLOT_WAIT_S
        while not _LAUNCH_SLOT.acquire(blocking=False):
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.05)
        self._holds_launch_slot = True
        return True

    def _free_launch_slot(self) -> None:
        if self._holds_launch_slot:
            self._holds_launch_slot = False
            _LAUNCH_SLOT.release()

    def health_problem(self) -> Optional[str]:
        """Why a running hidden browser must stop (its display or concealer failed), else None."""
        display = self.display
        if not self.active or display is None:
            return None
        try:
            if display.alive():
                return None
            return getattr(display, "dead_reason", None) or "the hidden display stopped"
        except Exception as e:
            return f"the hidden display could not be checked ({e})"

    async def _watch_health(self) -> None:
        """While running hidden (from the first page on, sign-in included), stop on a failed display."""
        while self.active:
            await asyncio.sleep(1.0)
            problem = self.health_problem()
            if problem:
                self._health_task = None
                await self.driver._stop_failed_background_browser(problem)
                return

    @property
    def private_clipboard(self) -> bool:
        """True when the hidden browser has its own clipboard (a private display)."""
        return bool(self.active and getattr(self.display, "private_clipboard", False))

    async def _release_display(self) -> None:
        self._free_launch_slot()
        task, self._health_task = self._health_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        display, self.display = self.display, None
        self.active = False
        if display is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await asyncio.shield(loop.run_in_executor(None, self._release_now, display))
        except Exception as e:
            Logger.debug(f"Error releasing the Background Mode display: {e}")

    # -- start -------------------------------------------------------------

    async def prepare(self, status_callback: StatusCallback = None) -> Optional[dict[str, str]]:
        """Start (or share) the private display. Returns Chromium's environment, or None to launch visibly."""
        self.active = False
        self.calibrating = False
        self.signature = None
        self.probe = None
        self.login_notice_sent = False
        self.user_data_dir = None
        self.placement_before = None
        self._free_launch_slot()
        self.enabled_for_start = self.enabled()
        if not self.enabled_for_start:
            return None

        if status_callback:
            status_callback("Preparing Background Mode...")
        try:
            display = await self._acquire_display()
        except BackgroundModeUnavailable as exc:
            reason = str(exc)
            if not can_open_visible_browser():
                raise RuntimeError(
                    f"Background Mode could not start: {reason} There is also no desktop session "
                    "to open a visible browser on."
                ) from exc
            Logger.warning(f"{self._label}: Background Mode is unavailable ({reason}) Opening the browser normally.")
            self._notify(
                "Background Mode Unavailable",
                "The browser was opened normally instead. Check the app for details.",
                f"Background Mode could not start: {reason}\n\n"
                f"{self._label} was opened in a normal, visible browser window instead.",
            )
            return None

        released: list[bool] = []
        try:
            return await self._prepare_with_display(display, released)
        finally:
            if self.display is not display and not released:
                # An error or cancellation before this session took the display over.
                self._release_in_thread(display)

    async def _prepare_with_display(self, display, released: list[bool]) -> Optional[dict[str, str]]:
        """prepare() once a display is acquired. Returns None after releasing it (visible start)."""

        async def release() -> None:
            released.append(True)
            loop = asyncio.get_running_loop()
            await asyncio.shield(loop.run_in_executor(None, self._release_now, display))

        self.signature = desktop_signature(display.snapshot)
        self.session_type = display.session_type
        if not (
            load_baseline(self._config_dir, self.signature, "fixed")
            or load_baseline(self._config_dir, self.signature, "follow")
        ):
            # First start on this desktop setup: run visibly once and record what pages see.
            self.calibrating = True
            await release()
            if not can_open_visible_browser():
                raise RuntimeError(
                    "Background Mode needs one visible start on this desktop to calibrate, but there "
                    "is no desktop session to open a visible browser on."
                )
            Logger.info(
                f"{self._label}: Background Mode is calibrating for this desktop. This start runs "
                "visibly; the next ones run hidden."
            )
            self.driver.notify_user(
                f"{self._label}: Background Mode Calibrating",
                "This first start is visible so IntenseRP can record your desktop. Next starts run hidden.",
                level="info",
            )
            return None

        mode = self._viewport_mode()
        policy = load_window_policy(self._config_dir, self.signature, mode)
        refusal = None
        if (
            policy is None
            or load_baseline(self._config_dir, self.signature, mode) is None
            or (self._next_profile_is_fresh() and load_fresh_window(self._config_dir, self.signature, mode) is None)
        ):
            refusal = "calibrate"
        elif policy.get("imposed") and display.session_type != SESSION_WINDOWS:
            # (On Windows the hidden window stays on the user's own desktop and window manager,
            # so whatever the desktop does to it happens in both modes.)
            other = "follow" if self._viewport_mode() == "fixed" else "fixed"
            other_policy = load_window_policy(self._config_dir, self.signature, other)
            hint = ""
            if other == "follow" and not (other_policy or {}).get("imposed"):
                hint = (
                    " Turning on Resize Viewport With Window lets the browser keep its own size; note "
                    "that it also changes what sites see in normal mode (your real screen size instead "
                    "of Playwright's fixed 1280x720 viewport)."
                )
            refusal = (
                f"your desktop resizes or maximizes the browser window itself ({policy.get('state')}, "
                f"{policy.get('size')}), which the private display cannot reproduce.{hint}"
            )
        if refusal is not None:
            await release()
            if not can_open_visible_browser():
                raise RuntimeError("Background Mode cannot run hidden here and there is no desktop session.")
            if refusal == "calibrate":
                self.calibrating = True
                Logger.info(
                    f"{self._label}: Background Mode is calibrating this viewport mode. This start runs "
                    "visibly; the next ones run hidden."
                )
            else:
                Logger.warning(f"{self._label}: Background Mode is unavailable: {refusal} Opening the browser normally.")
                self._notify(
                    "Background Mode Unavailable",
                    "The browser was opened normally instead. Check the app for details.",
                    f"Background Mode cannot run hidden here: {refusal}\n\n"
                    f"{self._label} was opened in a normal, visible browser window instead.",
                )
            return None

        if not await self._take_launch_slot():
            await release()
            Logger.warning(
                f"{self._label}: Background Mode is unavailable (another hidden start did not finish). "
                "Opening the browser normally."
            )
            return None
        self.display = display
        self.active = True
        display.mark_launch()
        outputs = ", ".join(
            f"{o.get('width')}x{o.get('height')}@{float(o.get('refresh') or 60):g}Hz"
            for o in display.snapshot.get("outputs") or []
        )
        source = "saved desktop snapshot" if display.snapshot_source == "saved" else "live desktop"
        if display.session_type == SESSION_WINDOWS:
            Logger.info(f"{self._label}: Background Mode on (windows). The browser runs on {display.description}.")
        else:
            Logger.info(
                f"{self._label}: Background Mode on ({display.session_type}). The browser runs on private "
                f"display {display.description} mirroring your {source} ({outputs})."
            )
        return display.browser_env()

    def _next_profile_is_fresh(self) -> bool:
        """True when the browser will open a profile with no saved window placement."""
        if not self._setting("persistent_sessions"):
            return True
        try:
            profile = self.driver._get_persistent_profile_dir()
        except Exception:
            return True
        return self._read_placement(profile) is None

    def before_profile_launch(self, user_data_dir: str) -> None:
        """Called right before Chromium opens a persistent profile."""
        self.user_data_dir = str(user_data_dir)
        self.placement_before = self._read_placement(user_data_dir) if self.enabled_for_start else None
        if self.active and self.display is not None and (self.placement_before or {}).get("maximized"):
            self.display.hint_maximized_window(str(user_data_dir))

    async def after_launch(self) -> None:
        """
        Called once the first page exists, before any navigation.

        Hidden runs must match the visible baseline here, or BackgroundModeUnavailable is
        raised and the caller relaunches visibly. Visible runs with the setting on record
        the baseline.
        """
        try:
            await self._check_launch()
        finally:
            self._free_launch_slot()

    async def _check_launch(self) -> None:
        if not self.enabled_for_start or self.driver.page is None:
            return
        if self.active and self.display is not None:
            # Playwright resizes the window for the viewport right after page creation; wait
            # until it sits where the desktop's window manager would put it (on Windows:
            # until it is concealed).
            settled = await self.display.settle(timeout_s=8.0)
            if not settled.get("ok"):
                error = settled.get("error") or "unknown error"
                if self.display.session_type == SESSION_WINDOWS:
                    raise BackgroundModeUnavailable(f"the browser window could not be hidden ({error}).")
                raise BackgroundModeUnavailable(
                    f"the browser window did not settle where your desktop would put it ({error})."
                )
            if self.display.session_type == SESSION_WINDOWS:
                try:
                    problem = await self.display.verify_on_screen(self.driver.page)
                except Exception as e:
                    problem = f"could not check that the browser window is hidden ({e})"
                if problem:
                    raise BackgroundModeUnavailable(f"{problem}.")
            if self.display.session_type == SESSION_WAYLAND:
                await self._restore_wayland_origin()
                self._watch_wayland_popups()

        try:
            settled_page = await self.driver.page.evaluate(SETTLE_JS)
            if not (isinstance(settled_page, dict) and settled_page.get("stable")):
                Logger.debug(f"{self._label}: Background Mode: window did not settle before probing ({settled_page})")
            probe = await self.driver.page.evaluate(PROBE_JS)
        except Exception as e:
            probe = None
            Logger.debug(f"{self._label}: Background Mode probe failed: {e}")
        self.probe = probe if isinstance(probe, dict) else None

        if not self.active:
            if self.signature and self.probe and (self.probe.get("gpuRenderer") or self.session_type == SESSION_WINDOWS):
                # Bookkeeping only: a visible start must never fail because of it.
                try:
                    save_baseline(self._config_dir, self.signature, self._viewport_mode(), self.probe)
                    if self.placement_before is None and self.probe.get("window"):
                        save_fresh_window(self._config_dir, self.signature, self._viewport_mode(), self.probe["window"])
                    await self._learn_window_policy()
                except Exception as e:
                    Logger.warning(f"{self._label}: could not save the Background Mode calibration: {e}")
                    return
                if self.calibrating:
                    Logger.info(f"{self._label}: Background Mode calibration recorded for this desktop.")
            return

        if not self.probe:
            raise BackgroundModeUnavailable("could not check the hidden browser against your desktop.")
        renderer = str(self.probe.get("gpuRenderer") or "")
        # (On Windows the hidden browser renders exactly like the visible one, on the same desktop.)
        if is_software_renderer(renderer) and self.session_type != SESSION_WINDOWS:
            raise BackgroundModeUnavailable(
                f"the private display has no GPU acceleration (WebGL renderer: {renderer or 'none'})."
            )
        baseline = load_baseline(self._config_dir, self.signature or "", self._viewport_mode()) or {}
        mismatches = compare_probes(baseline, self.probe)
        visibility = self.probe.get("visibility") or []
        if visibility and visibility[0] != "visible":
            mismatches.append(f"page visibility ({visibility[0]})")
        if self.probe.get("framesAlive") is False:
            mismatches.append("the hidden window is not rendering frames at full rate")

        if self.placement_before is None:
            fresh = load_fresh_window(self._config_dir, self.signature or "", self._viewport_mode())
            if fresh is not None and fresh != self.probe.get("window"):
                mismatches.append(
                    f"window geometry for a new profile ({self.probe.get('window')} instead of {fresh}); "
                    "your desktop places new windows differently than IntenseRP can reproduce"
                )

        record = load_window_record(self._config_dir, self.user_data_dir) if self.user_data_dir else None
        if (
            record
            and record.get("signature") == self.signature
            and record.get("placement") == self.placement_before
            and record.get("follow_window") == self._setting("browser_resize_viewport_with_window")
            and record.get("window") != self.probe.get("window")
        ):
            mismatches.append(
                f"window geometry ({self.probe.get('window')} instead of {record.get('window')}); your "
                "window manager places windows differently than IntenseRP can reproduce"
            )
        if (
            self.display is not None
            and self.display.session_type == SESSION_WAYLAND
            and (self.placement_before or {}).get("maximized")
        ):
            mismatches.append(
                "this browser window was last used maximized, and the private display cannot maximize "
                "windows on Wayland desktops (un-maximize it once in a visible run)"
            )
        if mismatches:
            raise BackgroundModeUnavailable(
                "the hidden browser would not look like your normal one: " + "; ".join(mismatches) + "."
            )
        Logger.info(f"{self._label}: Background Mode matches your desktop (GPU: {renderer}).")
        self._health_task = asyncio.create_task(self._watch_health())

    async def fall_back_to_visible(
        self, reason: str, status_callback: StatusCallback = None, *, for_login: bool = False
    ) -> None:
        """Tear down the hidden browser before a visible relaunch."""
        await self.driver._close_browser_only()
        await self._release_display()
        if not can_open_visible_browser():
            raise RuntimeError(
                f"{self._label}: Background Mode stopped ({reason}) and there is no desktop session "
                "to open a visible browser on."
            )
        if for_login:
            Logger.warning(
                f"{self._label}: {reason}. Background Mode hides the browser, so it is being reopened "
                "in a visible window for this session."
            )
            self._notify(
                "Sign-in Needed",
                "The browser window was opened so you can sign in. Check the app for details.",
                f"{self._label} needs you in the browser ({reason}).\n\n"
                "Background Mode hides the browser, so IntenseRP reopened it in a visible window for "
                "this session. Your session is saved, so the next start can run in the background again.",
            )
        else:
            Logger.warning(f"{self._label}: Background Mode stopped: {reason} Opening the browser normally.")
            self._notify(
                "Background Mode Unavailable",
                "The browser was opened normally instead. Check the app for details.",
                f"Background Mode stopped: {reason}\n\n{self._label} was opened in a normal, visible "
                "browser window instead, so your browser fingerprint stays the same.",
            )
        if status_callback:
            status_callback("Launching Browser...")

    @staticmethod
    def describe_failure(error: BaseException) -> str:
        text = str(error or "").strip() or error.__class__.__name__
        if isinstance(error, BackgroundModeUnavailable):
            return text
        return f"the hidden browser could not start ({text.splitlines()[0]})"

    # -- sign-in -----------------------------------------------------------

    def login_needed(self, reason: str = "") -> None:
        """A driver is about to wait for the user to finish signing in inside the browser."""
        if not self.active:
            return
        self.login_reason = str(reason or "").strip() or "the provider needs you to sign in"
        if self.login_event is not None:
            self.login_event.set()
            return
        if not self.login_notice_sent:
            self.login_notice_sent = True
            Logger.warning(
                f"{self._label}: {self.login_reason}, but the browser is hidden by Background Mode. "
                "Restart the provider to sign in."
            )
            self._notify(
                "Sign-in Needed",
                "The hidden browser needs you to sign in. Check the app for details.",
                f"{self._label} needs you to sign in, but its browser is hidden by Background Mode.\n\n"
                "Restart the provider; IntenseRP opens the browser visibly when sign-in is needed at "
                "startup. You can also turn Background Mode off.",
            )

    async def run_login(self) -> bool:
        """Run driver.login(); True if the user is needed (the login task was cancelled)."""
        self.login_reason = None
        self.login_event = asyncio.Event()
        login_task = asyncio.create_task(self.driver.login())
        signal_task = asyncio.create_task(self.login_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {login_task, signal_task}, timeout=LOGIN_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED
            )
            if login_task in done:
                login_task.result()
                return False
            if not self.login_reason:
                self.login_reason = f"sign-in did not finish within {int(LOGIN_TIMEOUT_S)} seconds"
            login_task.cancel()
            await asyncio.wait({login_task}, timeout=10.0)
            if login_task.done() and not login_task.cancelled():
                try:
                    login_task.exception()
                except Exception:
                    pass
            return True
        except asyncio.CancelledError:
            login_task.cancel()
            raise
        finally:
            signal_task.cancel()
            self.login_event = None

    # -- clipboard ---------------------------------------------------------

    async def set_clipboard_text(self, text: str) -> bool:
        """Set the private display's clipboard. False if not running hidden."""
        if not self.active or self.display is None:
            return False
        try:
            return await self.display.set_clipboard_text(text)
        except Exception as e:
            Logger.debug(f"{self._label}: Background Mode clipboard failed: {e}")
            return False

    # -- close -------------------------------------------------------------

    def _fix_up_hidden_placement(self) -> None:
        """
        Leave the profile's saved window placement exactly as a visible run would.

        Chromium saves the same placement in both modes for normal windows. For a window
        restored maximized, the desktop window manager reports the maximized state while
        sway does not, so fix up what Chromium wrote to what visible mode writes: still
        maximized when the viewport follows the window, otherwise un-maximized at
        Playwright's viewport-sized window (calibrated against xfwm4).
        """
        original = self.placement_before
        if not original or not original.get("maximized") or not self.user_data_dir:
            return
        placement = dict(original)
        if not self._setting("browser_resize_viewport_with_window"):
            try:
                left, top = int(original.get("left") or 0), int(original.get("top") or 0)
            except Exception:
                left, top = 0, 0
            width = _PLAYWRIGHT_DEFAULT_VIEWPORT[0] + _PLAYWRIGHT_LINUX_WINDOW_INSETS[0]
            height = _PLAYWRIGHT_DEFAULT_VIEWPORT[1] + _PLAYWRIGHT_LINUX_WINDOW_INSETS[1]
            placement.update(maximized=False, left=left, top=top, right=left + width, bottom=top + height)
        try:
            self.driver._update_browser_profile_json(
                Path(self.user_data_dir) / "Default" / "Preferences",
                {"browser": {"window_placement": placement}},
            )
        except Exception as e:
            Logger.debug(f"Could not fix up the saved browser window placement: {e}")

    async def before_close(self) -> None:
        """Visible runs: remember the window's final geometry (the user may have moved it)."""
        if not self.enabled_for_start or self.active or not self.probe or self.driver.page is None:
            return
        try:
            window = await asyncio.wait_for(
                self.driver.page.evaluate("() => [screenX, screenY, outerWidth, outerHeight, innerWidth, innerHeight]"),
                timeout=2.0,
            )
        except Exception:
            return
        if isinstance(window, list):
            self.probe["window"] = window

    def on_browser_closed(self) -> None:
        """Called after Chromium exited (normal close or before a relaunch)."""
        if not self.enabled_for_start or not self.user_data_dir:
            return
        if self.active:
            if self.session_type != SESSION_WINDOWS:
                # (On Windows the hidden window is on the real desktop, so Chromium already
                # saved what a visible run saves.)
                self._fix_up_hidden_placement()
        elif self.signature and self.probe and self.probe.get("window"):
            # Remember where the desktop's window manager put this profile's window, keyed
            # by the placement Chromium saved; the next hidden start from that placement
            # must end up in the same spot.
            try:
                save_window_record(
                    self._config_dir,
                    self.user_data_dir,
                    {
                        "signature": self.signature,
                        "placement": self._read_placement(self.user_data_dir),
                        "follow_window": self._setting("browser_resize_viewport_with_window"),
                        "window": self.probe.get("window"),
                    },
                )
            except Exception as e:
                Logger.warning(f"{self._label}: could not save the Background Mode window record: {e}")
        self.probe = None
        self.placement_before = None

    async def on_close(self) -> None:
        self.on_browser_closed()
        await self._release_display()
