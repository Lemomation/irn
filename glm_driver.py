import asyncio
import base64
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from drivers.base_driver import BaseDriver
from drivers.providers import DriverProvider
from drivers.shared_utils import (
    COMMON_REQUEST_MACRO_ACTIONS,
    IncrementalTextAccumulator,
    build_prompt_text_file_payload,
    clear_clean_regeneration_cache,
    compute_missing_suffix,
    extract_macro_overrides,
    find_multi_slot_cache_entry,
    format_request_messages,
    make_openai_delta_sse,
    make_openai_usage_sse,
    read_multi_slot_cache_payload,
    remove_multi_slot_cache_entry,
    strip_macros_from_messages,
    upsert_multi_slot_cache_entry,
)
from utils.cache_manager import CacheManager
from utils.logger import Logger
from utils.model_ids import (
    resolve_real_model_label_from_model_id,
)

load_dotenv()


GLM_REQUEST_MACRO_ACTIONS: Dict[str, tuple[str, Any]] = {
    **COMMON_REQUEST_MACRO_ACTIONS,
    "tool": ("tools_enabled", True),
    "tools": ("tools_enabled", True),
    "notool": ("tools_enabled", False),
    "no_tool": ("tools_enabled", False),
    "no-tool": ("tools_enabled", False),
    "notools": ("tools_enabled", False),
    "no_tools": ("tools_enabled", False),
    "no-tools": ("tools_enabled", False),
}


class GLMDriver(BaseDriver):
    REQUEST_CAPTURE_MODE_REPLAY = "replay"
    REQUEST_CAPTURE_MODE_CDP_TEEING = "cdp_teeing"
    CHAT_URL = "https://chat.z.ai/"
    AUTH_URL = "https://chat.z.ai/auth"
    CONVERSATION_URL_RE = re.compile(r"^https://chat\.z\.ai/c/([^/?#]+)", re.IGNORECASE)
    COMPLETION_ROUTE_GLOB = "**/api/**/chat/completions**"
    COMPLETION_URL_PATHS = {"/api/chat/completions", "/api/v2/chat/completions"}
    MODEL_CONCURRENCY_LIMIT_CODE = "MODEL_CONCURRENCY_LIMIT"
    DEEPTHINK_EFFORT_MODEL_FRIENDLIES: frozenset[str] = frozenset({"GLM-5.2", "GLM-5.3"})
    TOOLS_SUPPORTED_MODEL_FRIENDLY = "GLM-5V-Turbo"
    DEFAULT_GLM_52_DEEPTHINK_EFFORT = "max"
    MODEL_CAPACITY_TEXT_MARKERS = (
        "model_concurrency_limit",
        "currently at capacity",
        "peak hours",
        "switch to another model",
    )
    EMPTY_COMPLETION_STREAM_ERROR_CODE = "20001"
    SENSITIVE_ERROR_CODE = "SENSITIVE"
    RISK_CONTROL_BLOCKED_ERROR_CODE = "RISK_CONTROL_BLOCKED"
    CAPTCHA_ERROR_CODES: frozenset[str] = frozenset(
        {
            "FRONTEND_CAPTCHA_REQUIRED",
            "CAPTCHA_VERIFICATION_FAILED",
            "CAPTCHA_UNAVAILABLE",
        }
    )
    STREAM_ERROR_MESSAGES_BY_CODE: Dict[str, str] = {
        SENSITIVE_ERROR_CODE: (
            "GLM Chat's moderation flagged this prompt (code: SENSITIVE) and refused to "
            "process it. The current chat is now blocked on GLM's side - IntenseRP will "
            "use a fresh chat for the next request, but the prompt itself likely needs "
            "rephrasing."
        ),
        RISK_CONTROL_BLOCKED_ERROR_CODE: (
            "GLM Chat risk control temporarily blocked this account "
            "(code: RISK_CONTROL_BLOCKED). Wait a while before retrying."
        ),
        "FRONTEND_CAPTCHA_REQUIRED": (
            "GLM Chat requires manual verification (Alibaba FeiLin CAPTCHA) before this "
            "request can proceed. Solve the CAPTCHA in the GLM Chat browser window, then "
            "retry."
        ),
        "CAPTCHA_VERIFICATION_FAILED": (
            "GLM Chat CAPTCHA verification failed. Solve the CAPTCHA in the GLM Chat "
            "browser window and retry."
        ),
        "CAPTCHA_UNAVAILABLE": (
            "GLM Chat's CAPTCHA service is currently unavailable. Wait a moment and "
            "retry."
        ),
    }

    REFRESH_AFTER_GENERATION_DELAY_S = 2.0
    COMPLETION_REQUEST_TIMEOUT_S = 150.0
    INTERCEPT_FIRST_CHUNK_TIMEOUT_S = 45.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    MODEL_SELECTOR_READY_TIMEOUT_MS = 20000
    CLEAN_REGEN_STATE_KEYS = (
        "deepthink_enabled",
        "deepthink_effort",
        "search_enabled",
        "advanced_search_enabled",
        "tools_enabled",
        "send_as_text_file",
        "ui_model",
    )

    SIDEBAR_NEW_CHAT_BUTTON_SELECTOR = "button#sidebar-new-chat-button"
    QUICK_NEW_CHAT_BUTTON_SELECTOR = "button[data-scene='chat']"
    LEGACY_QUICK_NEW_CHAT_BUTTON_SELECTOR = "button#new-chat-button"

    MODEL_SELECTOR_BUTTON_SELECTOR = "button.modelSelectorButton"
    MODEL_DROPDOWN_ID = "f8T9iEf1QC"
    MODEL_DROPDOWN_SELECTOR = f"div#{MODEL_DROPDOWN_ID}"
    MODEL_OPTION_SELECTOR = "button[aria-label='model-item'][data-value], div[role='menu'] button[data-value]"
    MODEL_DATA_VALUE_BY_FRIENDLY: Dict[str, str] = {
        "GLM-5.3-Flash": "glm-5.3-flash",
        "GLM-5.3": "glm-5.3",
        "GLM-5.2": "glm-5.2",
        "GLM-5.1": "GLM-5.1",
        "GLM-5-Turbo": "GLM-5-Turbo",
        "GLM-5V-Turbo": "GLM-5v-Turbo",
        "GLM-4.7": "glm-4.7",
    }

    # Models hidden behind a collapsible section in older dropdown variants.
    MODELS_IN_COLLAPSIBLE: set = set()

    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.GLM_CHAT)
        self.cache_manager = CacheManager()

        self.current_model: Optional[str] = None
        self.current_send_deepthink: Optional[bool] = None
        self.thinking_active = False
        self._pending_request_overrides: dict[str, Any] = {}

        self.clean_regen_message_cache_key = "glm_last_message.txt"
        self.clean_regen_state_cache_key = "glm_last_message_state.json"
        self.multi_slot_cache_key = "glm_multi_slot_cache.json"
        self.repetition_buster_cache_key = "glm_repetition_buster_prompt_cache.json"

        self._refresh_after_generation = False
        self._refresh_after_generation_task: asyncio.Task | None = None
        self._last_captcha_notice_ts = 0.0

        self._refresh_quirks()

    def _refresh_quirks(self) -> None:
        """Read quirks settings from config and cache them as instance attributes."""
        try:
            ui_timeout = int(self.config_manager.get_setting("glm_behavior", "ui_click_timeout") or 3000)
        except Exception:
            ui_timeout = 3000
        try:
            post_delay = int(self.config_manager.get_setting("glm_behavior", "post_action_delay") or 500)
        except Exception:
            post_delay = 500
        try:
            msg_send_timeout = int(self.config_manager.get_setting("glm_behavior", "message_send_timeout") or 5)
        except Exception:
            msg_send_timeout = 5
        try:
            completion_request_timeout = float(
                self.config_manager.get_setting("glm_behavior", "completion_request_timeout")
                or self.COMPLETION_REQUEST_TIMEOUT_S
            )
        except Exception:
            completion_request_timeout = self.COMPLETION_REQUEST_TIMEOUT_S
        try:
            first_chunk_timeout = float(
                self.config_manager.get_setting("glm_behavior", "first_chunk_timeout") or self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S
            )
        except Exception:
            first_chunk_timeout = self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S
        try:
            refresh_after_generation = bool(
                self.config_manager.get_setting("glm_behavior", "refresh_after_generation")
            )
        except Exception:
            refresh_after_generation = False

        self._ui_timeout = max(ui_timeout, 500)
        self._post_delay_s = max(post_delay, 0) / 1000.0
        self._msg_send_timeout = max(msg_send_timeout, 1)
        self._completion_request_timeout_s = max(completion_request_timeout, 5.0)
        self._first_chunk_timeout_s = max(first_chunk_timeout, 5.0)
        self._refresh_after_generation = bool(refresh_after_generation)

    async def _await_pending_refresh_after_generation(self, abort_event: asyncio.Event | None = None) -> None:
        task = getattr(self, "_refresh_after_generation_task", None)
        if not task:
            return

        if abort_event and abort_event.is_set():
            try:
                if not task.done():
                    task.cancel()
            except Exception:
                pass
            self._refresh_after_generation_task = None
            return

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            Logger.warning(f"GLM Chat: Refresh After Generation task failed: {e}")
        finally:
            self._refresh_after_generation_task = None

    def _schedule_refresh_after_generation(self) -> None:
        if not self.page:
            return
        if not getattr(self, "_refresh_after_generation", False):
            return

        try:
            if self.page.is_closed():
                return
        except Exception:
            pass

        existing = getattr(self, "_refresh_after_generation_task", None)
        if existing and (not existing.done()):
            try:
                existing.cancel()
            except Exception:
                pass

        self._refresh_after_generation_task = asyncio.create_task(self._refresh_page_after_generation())

    async def _refresh_page_after_generation(self) -> None:
        if not self.page:
            return

        try:
            await asyncio.sleep(self.REFRESH_AFTER_GENERATION_DELAY_S)
        except asyncio.CancelledError:
            return

        await self._reload_chat_page("Refresh After Generation enabled")

    async def _reload_chat_page(self, reason: str) -> None:
        if not self.page:
            return

        try:
            if self.page.is_closed():
                return
        except Exception:
            pass

        Logger.info(f"GLM Chat: {reason}, reloading page...")

        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=45000)
        except asyncio.CancelledError:
            return
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to reload page: {e}")
            return

        try:
            await self._wait_for_chat_shell_ready(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"GLM Chat: page reload completed but chat shell was not ready: {e}")
            return

        # Best-effort: if the session expired, warn early so the next request isn't a surprise
        try:
            if await self._chat_page_contains_sign_in():
                Logger.warning("GLM Chat: after reload, Sign in was detected - you may need to log in again.")
        except Exception:
            pass

    async def _refresh_page_after_capacity_error(self) -> None:
        if not self.page:
            return

        try:
            if self.page.is_closed():
                return
        except Exception:
            pass

        Logger.info("GLM Chat: Model capacity error detected, returning to main chat page...")

        try:
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        except asyncio.CancelledError:
            return
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to return to main chat page after capacity error: {e}")
            return

        try:
            await self._wait_for_chat_shell_ready(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"GLM Chat: main chat page loaded but shell was not ready after capacity error: {e}")
            return

        try:
            if await self._chat_page_contains_sign_in():
                Logger.warning("GLM Chat: after capacity-error recovery, Sign in was detected - you may need to log in again.")
        except Exception:
            pass

    def _reset_generation_state(self) -> None:
        self.current_abort_event = None
        self.abort_requested = False
        self.current_model = None
        self.current_send_deepthink = None
        self.thinking_active = False

    def get_start_url(self) -> str:
        return self.CHAT_URL

    async def after_start(self, status_callback: Optional[Callable[[str], None]] = None) -> None:
        await self.check_ui_language(status_callback=status_callback)
        clear_clean_regeneration_cache(
            self.cache_manager,
            self.clean_regen_message_cache_key,
            self.clean_regen_state_cache_key,
        )

    async def before_initial_navigation(self) -> None:
        if not self.context:
            return

        # Alibaba's FeiLin CAPTCHA SDK wipes the DevTools console as an
        # anti-debug nuisance - exactly when users need to see what is
        # happening. Neutralize console.clear() in every frame before any
        # page script runs so logs survive a challenge.
        try:
            await self.context.add_init_script(
                """
                (() => {
                    try {
                        const noop = () => {};
                        window.console.clear = noop;
                    } catch (e) {}
                })();
                """
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to install console-clear guard: {e}")

        # Observe (but never block) CAPTCHA CDN traffic so IntenseRP's log shows
        # when a verification challenge is being pulled in.
        try:
            await self.context.route("**/captcha-frontend/**", self._handle_captcha_resource_route)
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to register CAPTCHA observer: {e}")

    async def _handle_captcha_resource_route(self, route: Any) -> None:
        now = time.time()
        if (now - getattr(self, "_last_captcha_notice_ts", 0.0)) > 30.0:
            self._last_captcha_notice_ts = now
            Logger.warning(
                "GLM Chat: CAPTCHA challenge resources are loading (FeiLin). "
                "If a verification popup appears, solve it in the GLM browser window."
            )

        try:
            await route.continue_()
        except Exception:
            pass

    async def cleanup_background_tasks(self) -> None:
        await self._cancel_task(
            self._refresh_after_generation_task,
            label="stopping GLM refresh-after-generation task",
        )
        self._refresh_after_generation_task = None

    def set_request_overrides(self, overrides: dict[str, Any] | None = None) -> None:
        self._pending_request_overrides = dict(overrides or {})

    def api_real_model_labels(self) -> list[str]:
        return list(self.MODEL_DATA_VALUE_BY_FRIENDLY.keys())

    def _get_glm_model_label_for_request(self, model: Any = None) -> str:
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        return override or self._get_configured_glm_model_friendly()

    async def apply_configured_model(
        self,
        model: Any = None,
        wait_until_ready: bool = False,
    ) -> None:
        await self._dismiss_dialog_close_buttons()

        desired_friendly = self._get_glm_model_label_for_request(model)
        if not desired_friendly:
            return

        # GLM's model picker is unreliable outside the fresh-new-chat flow
        # Keep the generic provider hook as a harmless no-op and only perform
        # actual picker work when a caller explicitly opts into waiting
        if not wait_until_ready:
            return

        try:
            await self._ensure_glm_model_selected(desired_friendly, wait_until_ready=wait_until_ready)
        except Exception as e:
            Logger.warning(f"GLM Chat: Failed to apply model selection '{desired_friendly}': {e}")

    def _get_configured_glm_model_friendly(self) -> str:
        try:
            value = self.config_manager.get_setting("glm_behavior", "model")
        except Exception:
            value = None
        return str(value or "").strip()

    def _get_configured_glm_deepthink_effort(self) -> str:
        try:
            value = self.config_manager.get_setting("glm_behavior", "deepthink_effort")
        except Exception:
            value = None
        return self._normalize_glm_deepthink_effort(value)

    @staticmethod
    def _normalize_model_label(value: str) -> str:
        return re.sub(r"\\s+", " ", str(value or "")).strip().lower()

    @classmethod
    def _glm_uses_deepthink_effort_controls(cls, model_friendly: str) -> bool:
        normalized = cls._normalize_model_label(model_friendly)
        return normalized in {
            cls._normalize_model_label(friendly)
            for friendly in cls.DEEPTHINK_EFFORT_MODEL_FRIENDLIES
        }

    @classmethod
    def _normalize_glm_deepthink_effort(cls, value: Any, default: str | None = None) -> str:
        fallback = str(default or cls.DEFAULT_GLM_52_DEEPTHINK_EFFORT).strip().lower()
        normalized = str(value or "").strip().lower()
        normalized = re.sub(r"[\s_]+", "-", normalized)
        normalized = re.sub(r"-{2,}", "-", normalized).strip("-")

        if normalized in {"max", "maximum", "xhigh", "x-high", "extra-high", "extra-highest"}:
            return "max"
        if normalized in {"high", "medium", "med"}:
            return "high"
        return fallback if fallback in {"high", "max"} else cls.DEFAULT_GLM_52_DEEPTHINK_EFFORT

    def _get_request_capture_mode(self) -> str:
        try:
            mode = str(
                self.config_manager.get_setting("glm_behavior", "request_capture_mode")
                or self.REQUEST_CAPTURE_MODE_REPLAY
            ).strip().lower()
        except Exception:
            mode = self.REQUEST_CAPTURE_MODE_REPLAY

        if mode == self.REQUEST_CAPTURE_MODE_CDP_TEEING:
            return self.REQUEST_CAPTURE_MODE_CDP_TEEING
        return self.REQUEST_CAPTURE_MODE_REPLAY

    @classmethod
    def _is_completion_request_url(cls, url: Any) -> bool:
        try:
            parsed = urlsplit(str(url or ""))
        except Exception:
            return False
        return parsed.path in cls.COMPLETION_URL_PATHS

    async def _dismiss_dialog_close_buttons(self, context: str = "GLM Chat") -> int:
        if not self.page:
            return 0

        total_clicked = 0

        # 1. Original: buttons with data-dialog-close
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const isVisible = (element) => {
                        if (!element) return false;
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };

                    let clicked = 0;
                    for (const button of document.querySelectorAll('button[data-dialog-close]')) {
                        if (button.disabled || !isVisible(button)) {
                            continue;
                        }
                        try {
                            button.click();
                            clicked += 1;
                        } catch (e) {
                            // Ignore one bad close button; the next UI action will retry.
                        }
                    }
                    return clicked;
                }"""
            )
            total_clicked = int(clicked or 0)
        except Exception as e:
            Logger.debug(f"{context}: failed to dismiss data-dialog-close buttons: {e}")

        # 2. Handle modal overlays with data-dialog-overlay (dismiss by pressing Escape)
        try:
            await self.page.evaluate(
                """() => {
                    const overlays = document.querySelectorAll('[data-dialog-overlay][data-state="open"]');
                    for (const overlay of overlays) {
                        // Try pressing Escape to close
                        const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
                        document.dispatchEvent(event);
                    }
                }"""
            )
        except Exception as e:
            Logger.debug(f"{context}: failed to dismiss dialog overlays via Escape: {e}")

        # 3. Handle modal overlays with close buttons that don't have data-dialog-close
        # Try to find and click close buttons in modal overlays
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const isVisible = (element) => {
                        if (!element) return false;
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };

                    let clicked = 0;
                    // Try to find close buttons in modal overlays
                    const selectors = [
                        'div[role="dialog"] button[aria-label*="close" i]',
                        'div[role="dialog"] button[aria-label*="dismiss" i]',
                        'div[data-dialog-overlay] button[aria-label*="close" i]',
                        'div[data-dialog-overlay] button[aria-label*="dismiss" i]',
                        'div._modal-overlay button[aria-label*="close" i]',
                        'div._modal-overlay button[aria-label*="dismiss" i]',
                        'div[class*="modal-overlay"] button[aria-label*="close" i]',
                        'div[class*="modal-overlay"] button[aria-label*="dismiss" i]',
                    ];
                    let clicked = 0;
                    for (const selector of selectors) {
                        for (const button of document.querySelectorAll(selector)) {
                            if (button.disabled || !isVisible(button)) {
                                continue;
                            }
                            try {
                                button.click();
                                clicked += 1;
                            } catch (e) {
                                // Ignore
                            }
                        }
                    }
                    return clicked;
                }"""
            )
            total_clicked += int(clicked or 0)
        except Exception as e:
            Logger.debug(f"{context}: failed to dismiss modal close buttons: {e}")

        if total_clicked:
            Logger.debug(f"{context}: dismissed {total_clicked} dialog/modal close button(s).")
            await asyncio.sleep(0.1)
        return total_clicked

    async def _read_glm_pointer_events_state(self) -> dict[str, Any]:
        """Return whether GLM's app shell is currently accepting pointer events."""
        if not self.page:
            return {"ready": True}

        try:
            state = await self.page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '')
                        .replace(/\\s+/g, ' ')
                        .trim();

                    const visibleOpenControls = Array.from(
                        document.querySelectorAll('[data-state="open"], [aria-expanded="true"]')
                    ).filter((element) => {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    }).slice(0, 8).map((element) => ({
                        tag: normalize(element.tagName).toLowerCase(),
                        id: normalize(element.id),
                        text: normalize(element.textContent).slice(0, 80),
                        dataState: normalize(element.getAttribute('data-state')),
                        ariaExpanded: normalize(element.getAttribute('aria-expanded')),
                    }));

                    const bodyStyle = document.body
                        ? window.getComputedStyle(document.body)
                        : null;
                    const htmlStyle = document.documentElement
                        ? window.getComputedStyle(document.documentElement)
                        : null;
                    const bodyPointerEvents = normalize(bodyStyle?.pointerEvents || '');
                    const htmlPointerEvents = normalize(htmlStyle?.pointerEvents || '');
                    return {
                        bodyPointerEvents,
                        htmlPointerEvents,
                        openControls: visibleOpenControls,
                        ready: bodyPointerEvents.toLowerCase() !== 'none',
                    };
                }"""
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to read pointer-events state: {e}")
            return {"ready": True}

        return state if isinstance(state, dict) else {"ready": True}

    async def _ensure_glm_pointer_events_ready(
        self,
        *,
        context: str = "GLM Chat",
        timeout_ms: int | None = None,
        send_escape: bool = True,
    ) -> bool:
        """Close stale popovers until GLM restores normal click hit-testing."""
        if not self.page:
            return False

        timeout = int(timeout_ms or self._ui_timeout)
        deadline = time.time() + max(0.0, float(timeout) / 1000.0)
        last_state: dict[str, Any] = {}
        last_escape_at = 0.0

        while True:
            last_state = await self._read_glm_pointer_events_state()
            if last_state.get("ready"):
                return True

            now = time.time()
            if send_escape and (last_escape_at <= 0.0 or (now - last_escape_at) >= 0.35):
                try:
                    await self.page.keyboard.press("Escape")
                except Exception as e:
                    Logger.debug(f"{context}: failed to press Escape while unblocking UI: {e}")
                last_escape_at = now

            if now >= deadline:
                Logger.debug(
                    f"{context}: UI still has body pointer-events='{last_state.get('bodyPointerEvents', '')}' "
                    f"after {timeout}ms; open controls={last_state.get('openControls', [])}"
                )
                return False

            await asyncio.sleep(0.1)

    async def _click_glm_control(
        self,
        target: Any,
        *,
        label: str,
        timeout_ms: int | None = None,
        ensure_unblocked: bool = True,
        evaluate_fallback: bool = True,
    ) -> bool:
        """Click a GLM control with fallbacks for transient overlay hit-test failures."""
        if not self.page or target is None:
            return False

        timeout = int(timeout_ms or self._ui_timeout)
        if ensure_unblocked:
            await self._ensure_glm_pointer_events_ready(
                context=f"GLM Chat: before clicking {label}",
                timeout_ms=min(timeout, 1000),
            )

        try:
            await target.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass

        try:
            await target.click(timeout=timeout)
            return True
        except Exception as e:
            Logger.debug(f"GLM Chat: {label} click failed, trying fallbacks: {e}")

        try:
            await target.click(timeout=timeout, force=True)
            return True
        except Exception as e:
            Logger.debug(f"GLM Chat: {label} forced click failed: {e}")

        if not evaluate_fallback:
            return False

        try:
            return bool(
                await target.evaluate(
                    """(element) => {
                        if (!element) return false;
                        element.click();
                        return true;
                    }"""
                )
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: {label} DOM click fallback failed: {e}")
            return False

    async def _read_glm_model_selector_state(self) -> dict[str, Any]:
        if not self.page:
            return {"exists": False, "ready": False}

        expression = (
            "selector => {"
            "  const btn = document.querySelector(selector);"
            "  if (!btn) return { exists: false, ready: false };"
            "  const style = window.getComputedStyle(btn);"
            "  const className = (btn.className || '').toString();"
            "  const classTokens = className.split(/\\s+/).filter(Boolean);"
            "  const rect = btn.getBoundingClientRect();"
            "  const visible = !!style && style.display !== 'none' && style.visibility !== 'hidden';"
            "  const hasSize = !!rect && rect.width > 0 && rect.height > 0;"
            "  const cursorDefault = classTokens.includes('cursor-default');"
            "  const pointerEvents = ((style && style.pointerEvents) || '').toLowerCase();"
            "  const disabled = !!btn.disabled;"
            "  return {"
            "    exists: true,"
            "    visible,"
            "    hasSize,"
            "    disabled,"
            "    cursorDefault,"
            "    className,"
            "    ariaDisabled: (btn.getAttribute('aria-disabled') || '').trim().toLowerCase(),"
            "    hasDataDisabled: btn.hasAttribute('data-disabled'),"
            "    pointerEvents,"
            "    ready: visible && hasSize && !disabled && !cursorDefault && pointerEvents !== 'none'"
            "  };"
            "}"
        )
        try:
            state = await self.page.evaluate(expression, self.MODEL_SELECTOR_BUTTON_SELECTOR)
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to read model selector readiness: {e}")
            return {"exists": False, "ready": False}

        return state if isinstance(state, dict) else {"exists": False, "ready": False}

    async def _is_glm_model_selector_ready(self) -> bool:
        state = await self._read_glm_model_selector_state()
        return bool(state.get("ready"))

    async def _wait_for_glm_model_selector_ready(self, timeout_ms: int = 10000) -> bool:
        if not self.page:
            return False

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            state = await self._read_glm_model_selector_state()
            if state.get("ready"):
                return True
            if time.time() >= deadline:
                Logger.debug(
                    "GLM Chat: model selector stayed disabled "
                    f"(class='{state.get('className', '')}', "
                    f"aria-disabled='{state.get('ariaDisabled', '')}', "
                    f"data-disabled={bool(state.get('hasDataDisabled'))})."
                )
                return False
            await asyncio.sleep(0.2)
 
    async def _click_glm_model_selector_button(self) -> bool:
        if not self.page:
            return False

        button = self.page.locator(self.MODEL_SELECTOR_BUTTON_SELECTOR)
        if await button.count() == 0:
            Logger.warning("GLM Chat: model selector button not found.")
            return False

        # GLM sometimes leaves aria-disabled/data-disabled on this button a bit longer
        # than the real UI lock. The cursor-default class tracks the clickable state more reliably.
        state = await self._read_glm_model_selector_state()
        if not state.get("ready"):
            return False

        try:
            await button.first.click(timeout=self._ui_timeout, force=True)
            return True
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to click model selector button: {e}")

        try:
            clicked = await self.page.evaluate(
                """(selector) => {
                    const btn = document.querySelector(selector);
                    if (!btn) return false;
                    btn.click();
                    return true;
                }""",
                self.MODEL_SELECTOR_BUTTON_SELECTOR,
            )
            return bool(clicked)
        except Exception as e:
            Logger.warning(f"GLM Chat: JS model selector click failed: {e}")
            return False

    async def _wait_and_open_glm_model_dropdown(self, timeout_ms: int = 10000) -> bool:
        if not self.page:
            return False

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            if not await self._wait_for_glm_model_selector_ready(timeout_ms=min(self._ui_timeout, 1000)):
                if time.time() >= deadline:
                    return False
                await asyncio.sleep(0.2)
                continue

            if await self._open_glm_model_dropdown(timeout_ms=min(self._ui_timeout, 1000)):
                return True

            if time.time() >= deadline:
                return False

            await asyncio.sleep(0.2)

    async def _read_current_glm_model_label(self) -> str:
        if not self.page:
            return ""

        try:
            label = await self.page.evaluate(
                "() => {"
                "  const btn = document.querySelector('button.modelSelectorButton');"
                "  if (!btn) return '';"
                "  const div = btn.querySelector('div');"
                "  if (!div) return '';"
                "  const node = div.childNodes && div.childNodes[0];"
                "  const text = (node && node.textContent) ? node.textContent : (div.textContent || '');"
                "  return (text || '').toString().trim();"
                "}"
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to read current model label: {e}")
            return ""

        return str(label or "").strip()

    async def _open_glm_model_dropdown(self, timeout_ms: int = 5000) -> bool:
        if not self.page:
            return False

        if not await self._click_glm_model_selector_button():
            return False

        # Wait for the dropdown content to appear. The old dropdown had a fixed
        # id, but the current Bits UI generates dynamic wrapper ids.
        try:
            await self.page.wait_for_selector(
                self.MODEL_OPTION_SELECTOR,
                timeout=int(timeout_ms),
                state="visible",
            )
            return True
        except Exception:
            pass

        # Legacy fallback for older GLM sessions.
        try:
            await self.page.wait_for_selector(
                f"{self.MODEL_DROPDOWN_SELECTOR} button[data-value]",
                timeout=int(timeout_ms),
                state="visible",
            )
            return True
        except Exception:
            pass

        # Fallback if the dropdown id changes.
        try:
            await self.page.wait_for_selector("button[data-value]", timeout=int(timeout_ms), state="visible")
            return True
        except Exception:
            Logger.warning("GLM Chat: model dropdown did not appear after clicking the selector (this is usually very bad).")
            return False

    async def _is_glm_model_dropdown_open(self) -> bool:
        if not self.page:
            return False

        try:
            return bool(
                await self.page.evaluate(
                    """(optionSelector) => {
                        const isVisible = (element) => {
                            if (!element) return false;
                            const style = window.getComputedStyle(element);
                            const rect = element.getBoundingClientRect();
                            return (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        };

                        const selector = document.querySelector('button.modelSelectorButton');
                        const expanded = String(selector?.getAttribute('aria-expanded') || '')
                            .trim()
                            .toLowerCase();
                        if (expanded === 'true') {
                            return true;
                        }

                        return Array.from(document.querySelectorAll(optionSelector)).some(isVisible);
                    }""",
                    self.MODEL_OPTION_SELECTOR,
                )
            )
        except Exception:
            pass

        try:
            options = self.page.locator(self.MODEL_OPTION_SELECTOR)
            count = await options.count()
            for idx in range(min(count, 10)):
                try:
                    if await options.nth(idx).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            pass

        return False

    async def _wait_for_glm_model_dropdown_closed(self, timeout_ms: int | None = None) -> bool:
        timeout = int(timeout_ms or self._ui_timeout)
        deadline = time.time() + max(0.0, float(timeout) / 1000.0)

        while True:
            if not await self._is_glm_model_dropdown_open():
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _close_glm_model_dropdown(self) -> None:
        if not self.page:
            return

        if not await self._is_glm_model_dropdown_open():
            await self._ensure_glm_pointer_events_ready(
                context="GLM Chat: after model dropdown check",
                timeout_ms=min(self._ui_timeout, 1000),
            )
            return

        try:
            await self.page.keyboard.press("Escape")
            if await self._wait_for_glm_model_dropdown_closed(timeout_ms=self._ui_timeout):
                await self._ensure_glm_pointer_events_ready(
                    context="GLM Chat: after closing model dropdown",
                    timeout_ms=self._ui_timeout,
                )
                return
        except Exception:
            pass

        button = self.page.locator(self.MODEL_SELECTOR_BUTTON_SELECTOR)
        if await button.count() == 0:
            return

        try:
            await button.first.click(timeout=self._ui_timeout, force=True)
        except Exception:
            try:
                await self.page.evaluate(
                    """(selector) => {
                        const btn = document.querySelector(selector);
                        if (btn) btn.click();
                    }""",
                    self.MODEL_SELECTOR_BUTTON_SELECTOR,
                )
            except Exception:
                return

        try:
            await self._wait_for_glm_model_dropdown_closed(timeout_ms=self._ui_timeout)
        except Exception:
            return

        await self._ensure_glm_pointer_events_ready(
            context="GLM Chat: after closing model dropdown",
            timeout_ms=self._ui_timeout,
        )

    async def _expand_collapsible_section(self) -> bool:
        """Expand the model dropdown's More Models section when it is collapsed."""
        if not self.page:
            return False

        try:
            content = self.page.locator("div[data-melt-collapsible-content]")
            if await content.count() > 0:
                try:
                    if await content.first.is_visible():
                        return True
                except Exception:
                    pass

            trigger = self.page.locator("button[data-melt-collapsible-trigger]")
            if await trigger.count() == 0:
                return False

            await trigger.first.click(timeout=self._ui_timeout)

            # Wait for the content to appear
            await content.first.wait_for(state="visible", timeout=self._ui_timeout)
            return True
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to expand collapsible section: {e}")
            return False

    async def _click_glm_model_option(self, data_value: str, friendly_name: str = "") -> bool:
        if not self.page:
            return False

        safe_value = str(data_value or "").strip()
        if not safe_value:
            return False

        if friendly_name in self.MODELS_IN_COLLAPSIBLE:
            await self._expand_collapsible_section()

        option = self.page.locator(
            f"button[aria-label='model-item'][data-value='{safe_value}'], "
            f"div[role='menu'] button[data-value='{safe_value}']"
        )
        if await option.count() == 0:
            option = self.page.locator(f"button[data-value='{safe_value}']")
        if await option.count() == 0 and friendly_name not in self.MODELS_IN_COLLAPSIBLE:
            await self._expand_collapsible_section()
            option = self.page.locator(
                f"button[aria-label='model-item'][data-value='{safe_value}'], "
                f"div[role='menu'] button[data-value='{safe_value}']"
            )
            if await option.count() == 0:
                option = self.page.locator(f"button[data-value='{safe_value}']")

        count = await option.count()
        if count == 0:
            return False

        for idx in range(min(count, 10)):
            cand = option.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                await cand.click(timeout=self._ui_timeout)
                return True
            except Exception:
                continue

        try:
            await option.first.click(timeout=self._ui_timeout)
            return True
        except Exception:
            return False

    async def _click_first_glm_model_option(self) -> Optional[str]:
        if not self.page:
            return None

        options = self.page.locator(self.MODEL_OPTION_SELECTOR)
        if await options.count() == 0:
            options = self.page.locator("button[data-value]")

        count = await options.count()
        if count == 0:
            return None

        for idx in range(min(count, 25)):
            cand = options.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
            except Exception:
                pass

            try:
                data_value = (await cand.get_attribute("data-value")) or ""
            except Exception:
                data_value = ""

            try:
                await cand.click(timeout=self._ui_timeout)
            except Exception:
                continue

            return str(data_value or "").strip() or None

        return None

    async def _ensure_glm_model_selected(self, desired_friendly: str, wait_until_ready: bool = False) -> None:
        if not self.page:
            return

        desired = str(desired_friendly or "").strip()
        if not desired:
            return

        desired_data_value = self.MODEL_DATA_VALUE_BY_FRIENDLY.get(desired)
        if not desired_data_value:
            Logger.warning(f"GLM Chat: unknown configured model '{desired}'.")
            return

        current_label = await self._read_current_glm_model_label()
        if self._normalize_model_label(current_label) == self._normalize_model_label(desired):
            return

        if wait_until_ready:
            if not await self._wait_and_open_glm_model_dropdown(timeout_ms=self.MODEL_SELECTOR_READY_TIMEOUT_MS):
                Logger.warning("GLM Chat: model selector did not become openable in time.")
                return
        else:
            if not await self._is_glm_model_selector_ready():
                Logger.debug("GLM Chat: model selector is not ready for interaction.")
                return
            if not await self._open_glm_model_dropdown(timeout_ms=5000):
                return

        try:
            clicked = await self._click_glm_model_option(desired_data_value, friendly_name=desired)
            if not clicked:
                Logger.error(
                    f"GLM Chat: desired model '{desired}' not found in dropdown (data-value '{desired_data_value}'). "
                    "Selecting the first available model instead."
                )
                fallback_value = await self._click_first_glm_model_option()
                if fallback_value:
                    Logger.warning(f"GLM Chat: selected fallback model data-value '{fallback_value}'.")
        finally:
            await self._close_glm_model_dropdown()

    async def _chat_page_contains_sign_in(self) -> bool:
        if not self.page:
            return False

        # Prefer a real element match over brittle body-text scans
        # (Body innerText may not be fully populated during loading screens)
        try:
            sign_in_controls = self.page.locator(
                "button:has-text('Sign in'), a:has-text('Sign in'), [role='button']:has-text('Sign in')"
            )
            count = await sign_in_controls.count()
            if count > 0:
                # Only treat as auth-needed if at least one is actually visible.
                for idx in range(min(count, 10)):
                    try:
                        if await sign_in_controls.nth(idx).is_visible():
                            return True
                    except Exception:
                        continue
        except Exception:
            pass

        try:
            # js fallback 1
            return bool(
                await self.page.evaluate(
                    "() => {"
                    "  const el = document.body;"
                    "  if (!el) return false;"
                    "  const text = (el.innerText || '');"
                    "  return text.includes('Sign in');"
                    "}"
                )
            )
        except Exception:
            return False

    async def _wait_for_chat_ready(self, timeout_ms: int | None = None) -> None:
        if not self.page:
            return

        timeout = 0 if timeout_ms is None else int(timeout_ms)
        await self.page.wait_for_selector("textarea#chat-input, #chat-input", timeout=timeout, state="visible")
        await self._dismiss_dialog_close_buttons()

    async def _wait_for_chat_shell_ready(self, timeout_ms: int | None = None) -> None:
        """
        Wait for GLM's initial loading screen to resolve into a stable UI.

        GLM can show a splash/loading view where auth text isn't present yet.
        We treat the page as "ready for auth detection" once either:
        - the chat composer appears, or
        - a visible Sign in control suddenly appears, confirming all your fears.
        """
        if not self.page:
            return

        shell_selector = (
            "textarea#chat-input, #chat-input, "
            "button:has-text('Sign in'), a:has-text('Sign in'), [role='button']:has-text('Sign in')"
        )
        combined_selector = f"{shell_selector}, button[data-dialog-close]"
        deadline = None if timeout_ms is None else time.time() + max(0.0, int(timeout_ms) / 1000.0)

        while True:
            timeout = 0
            if deadline is not None:
                remaining_ms = int(max(1.0, (deadline - time.time()) * 1000.0))
                timeout = remaining_ms

            await self.page.wait_for_selector(
                combined_selector,
                timeout=timeout,
                state="visible",
            )
            await self._dismiss_dialog_close_buttons()

            shell_controls = self.page.locator(shell_selector)
            count = await shell_controls.count()
            for idx in range(min(count, 10)):
                try:
                    if await shell_controls.nth(idx).is_visible():
                        return
                except Exception:
                    continue

            if deadline is not None and time.time() >= deadline:
                raise TimeoutError("GLM Chat shell did not become ready before timeout.")

    async def login(self) -> None:
        """
        GLM Chat does not auto-redirect to the auth page, so we must detect auth state.
        """
        if not self.page:
            return

        try:
            await self.page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        await self._dismiss_dialog_close_buttons()

        # GLM shows an initial loading screen; auth UI is unreliable to detect until the
        # app transitions into its stable shell. Wait for composer/sign-in UI before checking auth
        try:
            await self._wait_for_chat_shell_ready(timeout_ms=60000)
        except Exception as e:
            # If the composer never appears (UI change / slow load), fall back to best-effort auth detection
            Logger.debug(f"GLM Chat: chat composer not detected before auth check: {e}")
        await self._dismiss_dialog_close_buttons()

        needs_auth = await self._chat_page_contains_sign_in()
        if not needs_auth:
            Logger.info("GLM Chat: already signed in (or Sign in not detected).")
            self._mark_active_ece_pair_used()
            return

        auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        if not auto_login:
            Logger.info("GLM Chat: Auto-login disabled. Waiting for manual login...")
            await self.page.goto(self.AUTH_URL)
            await self._wait_for_chat_ready(timeout_ms=None)
            Logger.success("GLM Chat: manual login detected.")
            return

        email = ""
        password = ""

        pair = self.ece_active_pair()
        if not pair:
            Logger.warning(
                "GLM Chat: Auto-login is enabled but no accounts are configured in Credential Manager. "
                "Waiting for manual login..."
            )
            await self.page.goto(self.AUTH_URL)
            await self._wait_for_chat_ready(timeout_ms=None)
            Logger.success("GLM Chat: manual login detected.")
            return

        email = pair.email
        password = pair.password

        if not email or not password:
            Logger.error("GLM Chat account is missing an email or password.")
            return

        Logger.info("GLM Chat: Auto-login enabled. Attempting login...")
        await self.page.goto(self.AUTH_URL)

        try:
            await self.page.wait_for_selector(".loginFormUni", timeout=30000)
        except Exception as e:
            Logger.error(f"GLM Chat: login form not found: {e}")
            return

        form_root = self.page.locator(".loginFormUni")
        if await form_root.count() == 0:
            form_root = self.page.locator("body")

        # Select the email login button: second <button> under loginFormUni
        try:
            email_button = form_root.first.locator("button").nth(1)
            if await email_button.count() > 0:
                await email_button.click()
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to click email login button: {e}")

        # Fill credentials
        try:
            email_input = self.page.locator("input[type='email']")
            password_input = self.page.locator("input[type='password']")
            await email_input.first.fill(str(email))
            await password_input.first.fill(str(password))
        except Exception as e:
            Logger.error(f"GLM Chat: failed to fill credentials: {e}")
            return

        # Submit
        try:
            submit_button = self.page.locator("button[type='submit']")
            await submit_button.first.click()
        except Exception as e:
            Logger.error(f"GLM Chat: failed to click submit: {e}")
            return

        # CAPTCHA is required and must be solved manually.
        self.notify_user(
            "GLM Chat Login",
            "Please solve the CAPTCHA in the GLM Chat browser window, then click Sign in.",
            level="warning",
        )
        Logger.warning("GLM Chat: waiting for CAPTCHA / manual confirmation...")

        await self._wait_for_chat_ready(timeout_ms=None)
        Logger.success("GLM Chat: chat ready.")

        self.ece_mark_used(email)

    def _resolve_deepthink_flags(self, model: str) -> tuple[bool, bool]:
        """Resolve Deep Think from the user's configured GLM settings.

        The model behavior mode must not silently override the user's Deep Think
        selection. In particular, MODE_CHAT previously forced Deep Think off,
        which caused the UI setting to be turned back off on every new request.
        Model-specific capability is handled later by the actual UI toggle path.
        """
        del model  # Kept in the signature for compatibility with existing callers.

        enable_deepthink = bool(self.config_manager.get_setting("glm_behavior", "enable_deepthink"))
        send_deepthink = bool(self.config_manager.get_setting("glm_behavior", "send_deepthink"))
        return enable_deepthink, send_deepthink

    def _glm_tools_supported_for_model(self, model_friendly: str) -> bool:
        return self._normalize_model_label(model_friendly) == self._normalize_model_label(
            self.TOOLS_SUPPORTED_MODEL_FRIENDLY
        )

    def _resolve_glm_deepthink_effort(
        self,
        ui_model_label: str,
        *,
        deepthink_enabled: bool,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not (deepthink_enabled and self._glm_uses_deepthink_effort_controls(ui_model_label)):
            return ""

        override = (overrides or {}).get("deepthink_effort")
        value = override if override is not None else self._get_configured_glm_deepthink_effort()
        return self._normalize_glm_deepthink_effort(value)

    def _resolve_glm_request_settings(self, model: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resolved_model = (model or "").strip() or "glm-auto"
        ui_model_label = self._get_glm_model_label_for_request(resolved_model)
        deepthink_enabled, send_deepthink = self._resolve_deepthink_flags(resolved_model)
        deepthink_effort = self._resolve_glm_deepthink_effort(
            ui_model_label,
            deepthink_enabled=deepthink_enabled,
            overrides=overrides,
        )
        enable_search = bool(self.config_manager.get_setting("glm_behavior", "enable_search"))
        enable_advanced_search = bool(
            self.config_manager.get_setting("glm_behavior", "enable_advanced_search")
        )
        enable_tools = bool(self.config_manager.get_setting("glm_behavior", "enable_tools"))
        send_as_text_file = bool(self.config_manager.get_setting("glm_behavior", "send_as_text_file"))
        tools_supported = self._glm_tools_supported_for_model(ui_model_label)

        settings = {
            "model_label": ui_model_label,
            "deepthink_enabled": bool(deepthink_enabled),
            "deepthink_effort": deepthink_effort,
            "send_deepthink": bool(send_deepthink),
            "search_enabled": bool(enable_search),
            "advanced_search_enabled": bool(enable_advanced_search),
            "tools_enabled": bool(enable_tools),
            "send_as_text_file": bool(send_as_text_file),
        }

        if overrides:
            for key in (
                "deepthink_enabled",
                "send_deepthink",
                "deepthink_effort",
                "search_enabled",
                "advanced_search_enabled",
                "tools_enabled",
                "send_as_text_file",
            ):
                if key == "deepthink_effort":
                    if key in overrides:
                        settings[key] = self._resolve_glm_deepthink_effort(
                            ui_model_label,
                            deepthink_enabled=bool(settings["deepthink_enabled"]),
                            overrides=overrides,
                        )
                    continue
                if key in overrides:
                    settings[key] = bool(overrides[key])

        if not tools_supported:
            settings["tools_enabled"] = False
        if settings["deepthink_enabled"] and self._glm_uses_deepthink_effort_controls(ui_model_label):
            if not settings["deepthink_effort"]:
                settings["deepthink_effort"] = self._get_configured_glm_deepthink_effort()
        else:
            settings["deepthink_effort"] = ""

        if settings["advanced_search_enabled"] and not (
            settings["deepthink_enabled"] and settings["search_enabled"]
        ):
            Logger.warning(
                "GLM Chat: Advanced Search requires Deep Think and Search. "
                "Ignoring Advanced Search for this request."
            )
            settings["advanced_search_enabled"] = False

        return settings

    def _extract_glm_macros_from_text(self, text: str) -> tuple[str, Dict[str, bool]]:
        return extract_macro_overrides(text, macro_actions=GLM_REQUEST_MACRO_ACTIONS)

    def _strip_glm_macros_from_messages(self, messages: List[Any]) -> tuple[List[Any], Dict[str, bool]]:
        return strip_macros_from_messages(messages, macro_actions=GLM_REQUEST_MACRO_ACTIONS)

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, Any]]:
        raw = self.cache_manager.read_cache(self.clean_regen_state_cache_key)
        if raw is None:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            Logger.warning("Clean Regeneration (GLM): Cached state is invalid JSON, ignoring.")
            return None

        return self._normalize_clean_regeneration_state(data)

    def _write_clean_regeneration_state(self, state: Dict[str, Any]) -> None:
        payload = self._normalize_clean_regeneration_state(state)
        if payload is None:
            return
        self.cache_manager.write_cache(
            self.clean_regen_state_cache_key,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )

    def _normalize_clean_regeneration_state(self, state: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(state, dict):
            return None

        optional_keys = {"advanced_search_enabled", "deepthink_effort"}
        required_keys = [key for key in self.CLEAN_REGEN_STATE_KEYS if key not in optional_keys]
        if not all(key in state for key in required_keys):
            return None

        ui_model = str(state.get("ui_model") or "").strip()
        deepthink_enabled = bool(state.get("deepthink_enabled"))
        deepthink_effort = ""
        if deepthink_enabled and self._glm_uses_deepthink_effort_controls(ui_model):
            deepthink_effort = self._normalize_glm_deepthink_effort(
                state.get("deepthink_effort"),
                default=self.DEFAULT_GLM_52_DEEPTHINK_EFFORT,
            )

        return {
            "deepthink_enabled": deepthink_enabled,
            "deepthink_effort": deepthink_effort,
            "search_enabled": bool(state.get("search_enabled")),
            "advanced_search_enabled": bool(state.get("advanced_search_enabled", False)),
            "tools_enabled": bool(state.get("tools_enabled")),
            "send_as_text_file": bool(state.get("send_as_text_file")),
            "ui_model": ui_model,
        }

    def _build_multi_slot_cache_state(
        self,
        *,
        effective_deepthink: bool,
        deepthink_effort: str = "",
        enable_search: bool,
        enable_advanced_search: bool,
        enable_tools: bool,
        send_as_text_file: bool,
        ui_model_label: str | None = None,
    ) -> Dict[str, Any]:
        normalized_ui_model = str(ui_model_label or self._get_glm_model_label_for_request(self.current_model))
        normalized_effort = ""
        if effective_deepthink and self._glm_uses_deepthink_effort_controls(normalized_ui_model):
            normalized_effort = self._normalize_glm_deepthink_effort(deepthink_effort)
        return {
            "deepthink_enabled": bool(effective_deepthink),
            "deepthink_effort": normalized_effort,
            "search_enabled": bool(enable_search),
            "advanced_search_enabled": bool(enable_advanced_search),
            "tools_enabled": bool(enable_tools),
            "send_as_text_file": bool(send_as_text_file),
            "ui_model": normalized_ui_model,
        }

    async def _prepare_new_chat_request_ui(
        self,
        *,
        effective_deepthink: bool,
        deepthink_effort: str = "",
        enable_search: bool,
        enable_advanced_search: bool,
        enable_tools: bool,
        ui_model_label: str | None = None,
        log_label: str = "GLM Chat: preparing new chat session...",
    ) -> None:
        Logger.info(log_label)
        await self._dismiss_dialog_close_buttons()
        await self.click_new_chat(source="auto")
        await asyncio.sleep(self._post_delay_s)

        await self.apply_configured_model(model=self.current_model, wait_until_ready=True)
        await self.set_tools_state(bool(enable_tools), model_label=ui_model_label)
        await self.set_deepthink_state(
            bool(effective_deepthink),
            effort=deepthink_effort,
            model_label=ui_model_label,
        )
        await self.set_search_state(bool(enable_search))
        await self.set_advanced_search_state(bool(enable_advanced_search))
        await asyncio.sleep(self._post_delay_s)

    async def _send_text_request(
        self,
        message: str,
        *,
        send_timeout: int | None,
        arm_event: asyncio.Event | None = None,
        log_label: str = "GLM Chat: sending request...",
    ) -> None:
        await self._enter_message(message)
        await asyncio.sleep(self._post_delay_s)
        Logger.info(log_label)
        await self._send_message(timeout=send_timeout, arm_event=arm_event)

    async def _send_text_request_with_capacity_guard(
        self,
        message: str,
        *,
        send_timeout: int | None,
        log_label: str,
    ) -> str | None:
        if not self.page:
            await self._send_text_request(
                message,
                send_timeout=send_timeout,
                log_label=log_label,
            )
            return None

        def response_matches(response: Any) -> bool:
            try:
                url = str(response.url or "")
            except Exception:
                url = ""
            if not any(path in url for path in ("/api/chat/completions", "/api/v2/chat/completions")):
                return False

            try:
                method = str(response.request.method or "").upper()
            except Exception:
                method = ""
            return method == "POST"

        try:
            completion_timeout_ms = int(self._completion_request_timeout_s * 1000)
            async with self.page.expect_response(
                response_matches,
                timeout=completion_timeout_ms,
            ) as response_info:
                await self._send_text_request(
                    message,
                    send_timeout=send_timeout,
                    log_label=log_label,
                )
            response = await response_info.value
        except Exception as e:
            Logger.debug(f"GLM Chat: no completion response observed during guarded send: {e}")
            return None

        try:
            await asyncio.wait_for(response.finished(), timeout=3.0)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            Logger.debug(f"GLM Chat: failed waiting for guarded completion response to finish: {e}")
            return None

        try:
            response_text = await asyncio.wait_for(response.text(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            Logger.debug(f"GLM Chat: failed reading guarded completion response body: {e}")
            return None

        capacity_error_message = self._extract_model_capacity_error_from_text(response_text)
        if not capacity_error_message:
            stream_error_message = self._extract_stream_error_from_text(response_text)
            if stream_error_message:
                Logger.warning(stream_error_message)
                return stream_error_message

            if self._glm_frontend_would_see_sse_data_event(response_text):
                return None

            empty_stream_message = self._build_empty_completion_stream_error_message()
            Logger.warning(empty_stream_message)
            await self._reload_chat_page(
                f"empty completion stream (GLM Error code: {self.EMPTY_COMPLETION_STREAM_ERROR_CODE})"
            )
            return empty_stream_message

        Logger.warning(capacity_error_message)
        await self._refresh_page_after_capacity_error()
        return capacity_error_message

    async def _run_repetition_buster(
        self,
        *,
        effective_deepthink: bool,
        deepthink_effort: str = "",
        enable_search: bool,
        enable_advanced_search: bool,
        enable_tools: bool,
        ui_model_label: str | None = None,
        auto_delete_after_send: bool = False,
    ) -> str | None:
        Logger.info(
            "Repetition Buster (GLM): duplicate prompt detected. Sending a 128-character "
            "cache-buster prompt in a throwaway chat first..."
        )
        await self._prepare_new_chat_request_ui(
            effective_deepthink=effective_deepthink,
            deepthink_effort=deepthink_effort,
            enable_search=enable_search,
            enable_advanced_search=enable_advanced_search,
            enable_tools=enable_tools,
            ui_model_label=ui_model_label,
            log_label="Repetition Buster (GLM): opening throwaway chat...",
        )
        capacity_error_message = await self._send_text_request_with_capacity_guard(
            self._generate_repetition_buster_text(128),
            send_timeout=self._msg_send_timeout,
            log_label="Repetition Buster (GLM): sending cache-buster prompt...",
        )
        if capacity_error_message:
            return capacity_error_message
        await asyncio.sleep(self._post_delay_s)
        if auto_delete_after_send:
            await self._auto_delete_current_chat(log_context="Repetition Buster (GLM)")
        return None

    def _parse_conversation_info_from_url(self, url: str) -> Optional[Dict[str, str]]:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return None

        match = self.CONVERSATION_URL_RE.match(normalized_url)
        if not match:
            return None

        conversation_id = str(match.group(1) or "").strip()
        if not conversation_id:
            return None

        return {
            "conversation_id": conversation_id,
            "conversation_url": f"https://chat.z.ai/c/{conversation_id}",
        }

    async def _get_current_conversation_info(self) -> Optional[Dict[str, str]]:
        if not self.page:
            return None

        try:
            current_url = str(self.page.url or "")
        except Exception:
            current_url = ""

        return self._parse_conversation_info_from_url(current_url)

    async def _wait_for_current_conversation_info(
        self,
        timeout_ms: int = 6000,
        poll_interval_s: float = 0.2,
    ) -> Optional[Dict[str, str]]:
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            info = await self._get_current_conversation_info()
            if info is not None:
                return info

            if time.time() >= deadline:
                return None

            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _delete_conversation_by_id(self, conversation_id: str) -> bool:
        normalized_id = str(conversation_id or "").strip()
        if not normalized_id:
            return False

        result = await self._run_browser_request(
            method="DELETE",
            url=f"https://chat.z.ai/api/v1/chats/{normalized_id}",
            referrer="https://chat.z.ai/",
        )
        if bool(result.get("ok")):
            return True

        detail = str(result.get("error") or result.get("text") or "").strip()
        status = int(result.get("status") or 0)
        suffix = f" ({detail[:180]})" if detail else ""
        Logger.warning(
            f"GLM Chat: failed to auto-delete chat {normalized_id} (status={status}){suffix}"
        )
        return False

    async def _auto_delete_current_chat(self, *, log_context: str = "GLM Chat") -> bool:
        current_info = await self._get_current_conversation_info()
        if current_info is None:
            Logger.debug(f"{log_context}: auto-delete skipped because the current chat ID was not available.")
            return False

        conversation_id = str(current_info.get("conversation_id") or "").strip()
        if not conversation_id:
            Logger.debug(f"{log_context}: auto-delete skipped because the current chat ID was empty.")
            return False

        try:
            await self.click_new_chat(source="auto")
            await asyncio.sleep(self._post_delay_s)
        except Exception as e:
            Logger.warning(
                f"{log_context}: auto-delete skipped because a replacement chat could not be prepared: {e}"
            )
            return False

        if await self._delete_conversation_by_id(conversation_id):
            Logger.info(f"{log_context}: auto-deleted the completed chat.")
            return True

        return False

    async def _open_cached_conversation(self, conversation_url: str) -> bool:
        if not self.page:
            return False

        target_url = str(conversation_url or "").strip()
        if not target_url:
            return False

        try:
            await self.page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            Logger.warning(f"Multi-Slot Cache (GLM): failed to open cached chat URL: {e}")
            return False

        try:
            await self._wait_for_chat_shell_ready(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"Multi-Slot Cache (GLM): chat shell did not become ready: {e}")
            return False

        try:
            if await self._chat_page_contains_sign_in():
                Logger.warning("Multi-Slot Cache (GLM): cached chat URL requires sign-in.")
                return False
        except Exception:
            pass

        return True

    async def _try_multi_slot_regeneration(
        self,
        *,
        formatted_message: str,
        multi_slot_state: Dict[str, Any],
        completion_armed: asyncio.Event,
        completion_started: asyncio.Event,
    ) -> bool:
        account_key = self._get_multi_slot_cache_account_key()
        payload = read_multi_slot_cache_payload(
            self.cache_manager,
            self.multi_slot_cache_key,
            log_label="Multi-Slot Cache (GLM)",
        )
        entry = find_multi_slot_cache_entry(payload, account_key, formatted_message, multi_slot_state)
        if entry is None:
            return False

        current_info = await self._get_current_conversation_info()
        if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
            Logger.info("Multi-Slot Cache (GLM): opening cached conversation for regeneration...")
            opened = await self._open_cached_conversation(entry["conversation_url"])
            if not opened:
                return False
            current_info = await self._get_current_conversation_info()
            if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
                Logger.warning(
                    "Multi-Slot Cache (GLM): cached conversation URL opened, but the expected "
                    "chat ID was not available. Falling back to a new chat."
                )
                return False

        try:
            await self.set_tools_state(
                bool(multi_slot_state.get("tools_enabled")),
                model_label=str(multi_slot_state.get("ui_model") or ""),
            )
            await self.set_deepthink_state(
                bool(multi_slot_state.get("deepthink_enabled")),
                effort=str(multi_slot_state.get("deepthink_effort") or ""),
                model_label=str(multi_slot_state.get("ui_model") or ""),
            )
            await self.set_search_state(bool(multi_slot_state.get("search_enabled")))
            await self.set_advanced_search_state(bool(multi_slot_state.get("advanced_search_enabled")))
            await asyncio.sleep(self._post_delay_s)
        except Exception:
            pass

        regen_timeout = max(int(getattr(self, "_ui_timeout", 3000)), 5000)
        Logger.info("Multi-Slot Cache (GLM): cached prompt match found. Attempting to regenerate...")
        if not await self._click_regenerate(timeout_ms=regen_timeout, arm_event=completion_armed):
            Logger.warning(
                "Multi-Slot Cache (GLM): regenerate button unavailable. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (GLM)",
            )
            return False

        try:
            await asyncio.wait_for(
                completion_started.wait(),
                timeout=self._completion_request_timeout_s,
            )
        except asyncio.TimeoutError:
            Logger.warning(
                "Multi-Slot Cache (GLM): completion request not observed after clicking "
                "Regenerate. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (GLM)",
            )
            return False

        return True

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        return format_request_messages(self.config_manager, messages)

    async def set_sidebar_status(self, open: bool) -> None:
        if not self.page:
            return

        sidebar = self.page.locator("#sidebar")
        if await sidebar.count() == 0:
            Logger.warning("GLM Chat: sidebar container not found.")
            return

        state = (await sidebar.first.get_attribute("data-state")) or ""
        is_open = state.strip().lower() == "true"

        if is_open == open:
            return

        if open:
            toggle = self.page.locator("button#sidebar-toggle-button")
        else:
            toggle = (
                sidebar.first.locator("div").nth(0).locator("div").nth(0).locator("button").nth(0)
            )

        if await toggle.count() == 0:
            Logger.warning("GLM Chat: sidebar toggle button not found.")
            return

        try:
            await toggle.first.click(timeout=self._ui_timeout)
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to click sidebar toggle: {e}")
            return

        # Wait for state to flip
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                state_now = (await sidebar.first.get_attribute("data-state")) or ""
                open_now = state_now.strip().lower() == "true"
                if open_now == open:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)

    async def click_new_chat(self, source: str = "auto") -> None:
        if not self.page:
            return

        await self._dismiss_dialog_close_buttons()

        # Also dismiss any modal overlays that might intercept clicks
        try:
            await self.page.evaluate(
                """() => {
                    const overlays = document.querySelectorAll('[data-dialog-overlay][data-state="open"]');
                    for (const overlay of overlays) {
                        const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
                        document.dispatchEvent(event);
                    }
                }"""
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to dismiss modal overlays via Escape: {e}")

        sidebar = self.page.locator("#sidebar")
        is_open = False
        try:
            state = (await sidebar.first.get_attribute("data-state")) or ""
            is_open = state.strip().lower() == "true"
        except Exception:
            is_open = False

        if source == "sidebar":
            is_open = True
        elif source == "simple":
            is_open = False
        elif source != "auto":
            Logger.warning(f"GLM Chat: unknown new chat source '{source}'.")

        selectors = (
            [
                self.SIDEBAR_NEW_CHAT_BUTTON_SELECTOR,
                self.QUICK_NEW_CHAT_BUTTON_SELECTOR,
                self.LEGACY_QUICK_NEW_CHAT_BUTTON_SELECTOR,
            ]
            if is_open
            else [
                self.QUICK_NEW_CHAT_BUTTON_SELECTOR,
                self.LEGACY_QUICK_NEW_CHAT_BUTTON_SELECTOR,
                self.SIDEBAR_NEW_CHAT_BUTTON_SELECTOR,
            ]
        )

        btn = None
        for selector in selectors:
            locator = self.page.locator(selector)
            count = await locator.count()
            for idx in range(min(count, 5)):
                candidate = locator.nth(idx)
                try:
                    if await candidate.is_visible():
                        btn = candidate
                        break
                except Exception:
                    continue
            if btn is not None:
                break

        if btn is None:
            Logger.warning("GLM Chat: New Chat button not found.")
            return

        try:
            await btn.click(timeout=self._ui_timeout)
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to click New Chat: {e}")

    async def _find_composer_toggle_button(self, label_prefix: str, *, state_attr: str | None = None):
        """Find a visible composer toggle by its aria-label wrapper.

        GLM's footer toggles now live inside wrappers like
        ``div[aria-label="Web search enabled"]``. Some models also insert a
        separate Tools button between Search and Deep Think, so position-based
        lookup is no longer reliable.
        """
        if not self.page:
            return None

        try:
            selector = f"div[aria-label^='{label_prefix}'] button"
            if state_attr:
                selector = f"{selector}[{state_attr}]"
            candidates = self.page.locator(selector)
            count = await candidates.count()
        except Exception:
            return None

        for idx in range(min(count, 25)):
            cand = candidates.nth(idx)
            try:
                if await cand.is_visible():
                    return cand
            except Exception:
                pass

        return candidates.first if count > 0 else None

    async def _find_compact_search_button(self):
        """Find Search in GLM's compact non-Tools composer layout.

        Non-5V models no longer expose a Web Search aria-label wrapper. In that
        layout, Search is the unlabeled globe button immediately before the Deep
        Think toggle, while GLM-5V still has a separate Tools button in between.
        GLM-5.2 can replace the old Deep Think wrapper with an effort menu, so
        keep a composer-local ``button[data-active]`` fallback for that layout.
        """
        if not self.page:
            return None

        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const isVisible = (element) => {
                        if (!element) return false;
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };

                    const skipSibling = (element) => (
                        element?.classList?.contains('flagsContainer') ||
                        element?.getAttribute?.('aria-hidden') === 'true'
                    );

                    const deepThinkWrappers = Array.from(
                        document.querySelectorAll('div[aria-label^="Deep think"]')
                    );
                    for (const wrapper of deepThinkWrappers) {
                        const deepThinkButton = wrapper.querySelector('button[data-autothink]');
                        if (!isVisible(deepThinkButton)) continue;

                        let searchRoot = wrapper.previousElementSibling;
                        while (searchRoot && skipSibling(searchRoot)) {
                            searchRoot = searchRoot.previousElementSibling;
                        }
                        if (!searchRoot) continue;

                        const candidates = [];
                        if (searchRoot.matches?.('button')) {
                            candidates.push(searchRoot);
                        }
                        candidates.push(...searchRoot.querySelectorAll('button'));

                        for (const button of candidates.reverse()) {
                            if (
                                button.id === 'upload-file-button' ||
                                button.id === 'send-message-button' ||
                                button.hasAttribute('data-autothink') ||
                                button.closest('div[aria-label^="Deep think"]')
                            ) {
                                continue;
                            }
                            if (isVisible(button)) {
                                return button;
                            }
                        }
                    }

                    return null;
                }"""
            )
            button = handle.as_element()
            if button:
                return button
        except Exception as e:
            Logger.debug(f"GLM Chat: compact Search button lookup failed: {e}")

        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const isVisible = (element) => {
                        if (!element) return false;
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };

                    const isExcludedButton = (button) => (
                        button.id === 'upload-file-button' ||
                        button.id === 'send-message-button' ||
                        button.hasAttribute('data-autothink') ||
                        button.closest('div[aria-label^="Deep think"]') ||
                        button.closest('[aria-label="Send Message"]') ||
                        button.closest('[aria-label^="Up to "]')
                    );

                    const inputs = Array.from(
                        document.querySelectorAll('textarea#chat-input, #chat-input, textarea')
                    ).filter(isVisible);

                    for (const input of inputs) {
                        const roots = [];
                        const addRoot = (node) => {
                            if (node && !roots.includes(node)) {
                                roots.push(node);
                            }
                        };

                        addRoot(input.closest('form'));
                        let parent = input.parentElement;
                        for (let depth = 0; parent && depth < 5; depth += 1) {
                            addRoot(parent);
                            parent = parent.parentElement;
                        }

                        const inputRect = input.getBoundingClientRect();
                        for (const root of roots) {
                            const candidates = Array.from(root.querySelectorAll('button[data-active]'))
                                .filter((button) => {
                                    if (!isVisible(button) || isExcludedButton(button)) {
                                        return false;
                                    }
                                    const rect = button.getBoundingClientRect();
                                    return rect.top >= inputRect.bottom - 16;
                                })
                                .sort((left, right) => {
                                    const leftRect = left.getBoundingClientRect();
                                    const rightRect = right.getBoundingClientRect();
                                    return leftRect.left - rightRect.left;
                                });

                            if (candidates.length > 0) {
                                return candidates[0];
                            }
                        }
                    }

                    return null;
                }"""
            )
            button = handle.as_element()
            if button:
                return button
        except Exception as e:
            Logger.debug(f"GLM Chat: composer Search data-active lookup failed: {e}")

        return None

    async def _read_composer_toggle_enabled(
        self,
        button: Any,
        *,
        state_attr: str,
        fallback_state_attrs: tuple[str, ...] = (),
        enabled_label_prefix: str,
        enabled_class_markers: tuple[str, ...] = (),
    ) -> bool:
        """Read a composer toggle's enabled state across old and compact DOMs."""
        state_attrs = tuple(dict.fromkeys((state_attr, *fallback_state_attrs)))
        try:
            return bool(
                await button.evaluate(
                    """(button, options) => {
                        const nodes = [];
                        const addNode = (node) => {
                            if (node && !nodes.includes(node)) {
                                nodes.push(node);
                            }
                        };

                        addNode(button);
                        addNode(button.closest?.('button'));
                        addNode(button.closest?.('[aria-label]'));

                        let parent = button.parentElement;
                        for (let depth = 0; parent && depth < 3; depth += 1) {
                            addNode(parent);
                            parent = parent.parentElement;
                        }

                        for (const node of nodes) {
                            for (const attr of options.stateAttrs || []) {
                                if (node.hasAttribute?.(attr)) {
                                    return String(node.getAttribute(attr) || '')
                                        .trim()
                                        .toLowerCase() === 'true';
                                }
                            }
                        }

                        const enabledPrefix = String(options.enabledLabelPrefix || '')
                            .trim()
                            .toLowerCase();
                        if (enabledPrefix) {
                            for (const node of nodes) {
                                const label = String(node.getAttribute?.('aria-label') || '')
                                    .trim()
                                    .toLowerCase();
                                if (label.startsWith(enabledPrefix)) {
                                    return true;
                                }
                            }
                        }

                        const classText = nodes
                            .map((node) => {
                                if (typeof node.className === 'string') {
                                    return node.className;
                                }
                                if (node.className?.baseVal) {
                                    return node.className.baseVal;
                                }
                                return '';
                            })
                            .join(' ');

                        return (options.enabledClassMarkers || []).some(
                            (marker) => classText.includes(marker)
                        );
                    }""",
                    {
                        "stateAttrs": list(state_attrs),
                        "enabledLabelPrefix": enabled_label_prefix,
                        "enabledClassMarkers": list(enabled_class_markers),
                    },
                )
            )
        except Exception:
            pass

        for attr_name in state_attrs:
            try:
                attr = await button.get_attribute(attr_name)
                if attr is not None:
                    return str(attr or "").strip().lower() == "true"
            except Exception:
                continue
        return False

    async def _find_deepthink_button(self):
        """Find the Deep Think button by its aria-label wrapper."""
        return await self._find_composer_toggle_button("Deep think", state_attr="data-autothink")

    async def _find_glm_52_deepthink_trigger(self):
        """Find GLM-5.2's combined Deep Think effort menu trigger."""
        if not self.page:
            return None

        try:
            candidates = self.page.locator(
                "div[aria-expanded][data-state][type='button']"
            ).filter(has_text="Deep Think")
            count = await candidates.count()
        except Exception:
            return None

        for idx in range(min(count, 10)):
            cand = candidates.nth(idx)
            try:
                if await cand.is_visible():
                    return cand
            except Exception:
                pass

        return candidates.first if count > 0 else None

    async def _read_glm_52_deepthink_trigger_state(self, trigger: Any | None = None) -> dict[str, Any]:
        button = trigger or await self._find_glm_52_deepthink_trigger()
        if not button:
            return {"exists": False, "enabled": False, "effort": ""}

        try:
            text = await button.inner_text()
        except Exception:
            text = ""

        normalized = self._normalize_model_label(text)
        enabled = "off" not in normalized
        effort = ""
        if enabled:
            if "high" in normalized:
                effort = "high"
            elif "max" in normalized:
                effort = "max"

        return {
            "exists": True,
            "enabled": enabled,
            "effort": effort,
            "text": str(text or "").strip(),
        }

    async def _open_glm_52_deepthink_menu(self) -> bool:
        if not self.page:
            return False

        trigger = await self._find_glm_52_deepthink_trigger()
        if not trigger:
            Logger.warning("GLM Chat: GLM-5.2 Deep Think effort menu not found.")
            return False

        try:
            expanded = str((await trigger.get_attribute("aria-expanded")) or "").strip().lower()
        except Exception:
            expanded = ""

        if expanded != "true":
            await self._ensure_glm_pointer_events_ready(
                context="GLM Chat: before opening GLM-5.2 Deep Think menu",
                timeout_ms=min(self._ui_timeout, 1000),
            )
            try:
                await trigger.click(timeout=self._ui_timeout)
            except Exception as e:
                Logger.warning(f"GLM Chat: failed to open GLM-5.2 Deep Think menu: {e}")
                return False

        try:
            await self.page.wait_for_selector(
                "div[role='menu'][data-state='open'] button[role='switch'][aria-checked]",
                timeout=self._ui_timeout,
                state="visible",
            )
            return True
        except Exception:
            Logger.warning("GLM Chat: GLM-5.2 Deep Think menu did not appear.")
            return False

    async def _close_glm_52_deepthink_menu(self) -> None:
        if not self.page:
            return
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        if await self._ensure_glm_pointer_events_ready(
            context="GLM Chat: closing GLM-5.2 Deep Think menu",
            timeout_ms=self._ui_timeout,
        ):
            return

        trigger = await self._find_glm_52_deepthink_trigger()
        if not trigger:
            return

        try:
            await trigger.evaluate(
                """(element) => {
                    if (!element) return;
                    if (String(element.getAttribute('aria-expanded') || '').toLowerCase() === 'true') {
                        element.click();
                    }
                }"""
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: DOM close for GLM-5.2 Deep Think menu failed: {e}")

        await self._ensure_glm_pointer_events_ready(
            context="GLM Chat: after DOM-closing GLM-5.2 Deep Think menu",
            timeout_ms=self._ui_timeout,
        )

    async def _read_glm_52_deepthink_switch_enabled(self) -> bool:
        if not self.page:
            return False
        switch = self.page.locator(
            "div[role='menu'][data-state='open'] button[role='switch'][aria-checked]"
        )
        if await switch.count() == 0:
            return False
        try:
            checked = await switch.first.get_attribute("aria-checked")
            return str(checked or "").strip().lower() == "true"
        except Exception:
            return False

    async def _click_glm_52_deepthink_switch(self) -> bool:
        if not self.page:
            return False
        switch = self.page.locator(
            "div[role='menu'][data-state='open'] button[role='switch'][aria-checked]"
        )
        if await switch.count() == 0:
            return False
        try:
            await switch.first.click(timeout=self._ui_timeout)
            return True
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to toggle GLM-5.2 Deep Think switch: {e}")
            return False

    async def _select_glm_52_deepthink_effort(self, effort: str) -> bool:
        if not self.page:
            return False

        desired = self._normalize_glm_deepthink_effort(effort)
        label = "Max" if desired == "max" else "High"
        menu = self.page.locator("div[role='menu'][data-state='open']")
        option = menu.locator("button[type='button'][data-selected]").filter(has_text=label)
        count = await option.count()
        if count == 0:
            Logger.warning(f"GLM Chat: GLM-5.2 Deep Think effort option '{label}' not found.")
            return False

        for idx in range(min(count, 5)):
            cand = option.nth(idx)
            try:
                if await cand.is_visible():
                    selected = str((await cand.get_attribute("data-selected")) or "").strip().lower()
                    if selected == "true":
                        return True
                    await cand.click(timeout=self._ui_timeout)
                    await asyncio.sleep(0.1)
                    return True
            except Exception:
                continue

        return False

    async def _set_glm_52_deepthink_state(self, state: bool, effort: str | None = None) -> None:
        desired_enabled = bool(state)
        desired_effort = self._normalize_glm_deepthink_effort(
            effort or self._get_configured_glm_deepthink_effort()
        )

        trigger = await self._find_glm_52_deepthink_trigger()
        current = await self._read_glm_52_deepthink_trigger_state(trigger)
        if not current.get("exists"):
            Logger.warning("GLM Chat: GLM-5.2 Deep Think control not found.")
            return

        if not desired_enabled:
            if not current.get("enabled"):
                return
        elif current.get("enabled") and current.get("effort") == desired_effort:
            return

        if not await self._open_glm_52_deepthink_menu():
            return

        try:
            if desired_enabled:
                if current.get("effort") != desired_effort:
                    await self._select_glm_52_deepthink_effort(desired_effort)
                    if not await self._open_glm_52_deepthink_menu():
                        return
                if not await self._read_glm_52_deepthink_switch_enabled():
                    await self._click_glm_52_deepthink_switch()
            else:
                if await self._read_glm_52_deepthink_switch_enabled():
                    await self._click_glm_52_deepthink_switch()
        finally:
            await self._close_glm_52_deepthink_menu()

    async def _find_search_button(self):
        """Find the Web Search button in the active GLM composer layout."""
        if self._glm_tools_supported_for_model(self._get_configured_glm_model_friendly()):
            button = await self._find_composer_toggle_button("Web search", state_attr="data-active")
            if button:
                return button
            button = await self._find_composer_toggle_button("Web search", state_attr="data-selected")
            if button:
                return button
        return await self._find_compact_search_button()

    async def _find_tools_button(self):
        """Find the Tools button by its aria-label wrapper."""
        return await self._find_composer_toggle_button("Tools", state_attr="data-selected")

    async def set_deepthink_state(
        self,
        state: bool,
        *,
        effort: str | None = None,
        model_label: str | None = None,
    ) -> None:
        if not self.page:
            return

        await self._dismiss_dialog_close_buttons()
        await self._close_glm_model_dropdown()

        effective_model_label = str(model_label or "").strip()
        if not effective_model_label:
            effective_model_label = await self._read_current_glm_model_label()
        if not effective_model_label:
            effective_model_label = self._get_configured_glm_model_friendly()

        if self._glm_uses_deepthink_effort_controls(effective_model_label):
            await self._set_glm_52_deepthink_state(state, effort=effort)
            return

        button = await self._find_deepthink_button()
        if not button:
            Logger.warning("GLM Chat: Deep Think button not found.")
            return

        try:
            attr = await button.get_attribute("data-autothink")
            is_enabled = str(attr or "").strip().lower() == "true"
        except Exception:
            is_enabled = False

        if is_enabled == state:
            return

        if not await self._click_glm_control(button, label="Deep Think"):
            Logger.warning("GLM Chat: failed to toggle Deep Think.")

    async def set_search_state(self, state: bool) -> None:
        if not self.page:
            return

        await self._dismiss_dialog_close_buttons()
        await self._close_glm_model_dropdown()

        button = await self._find_search_button()
        if not button:
            Logger.warning("GLM Chat: Search button not found.")
            return

        is_enabled = await self._read_composer_toggle_enabled(
            button,
            state_attr="data-active",
            fallback_state_attrs=("data-selected",),
            enabled_label_prefix="web search enabled",
            enabled_class_markers=("text-[#0881F0]", "bg-[#F0F7FE]"),
        )

        if is_enabled == state:
            return

        if not await self._click_glm_control(button, label="Search"):
            Logger.warning("GLM Chat: failed to toggle Search.")

    async def _find_advanced_search_switch(self, search_button: Any | None = None):
        """Find the Advanced Search switch that appears while Search is hovered."""
        if not self.page:
            return None

        await self._close_glm_model_dropdown()
        await self._ensure_glm_pointer_events_ready(
            context="GLM Chat: before hovering Search for Advanced Search",
            timeout_ms=min(self._ui_timeout, 1000),
        )

        button = search_button or await self._find_search_button()
        if not button:
            return None

        try:
            await button.hover(timeout=self._ui_timeout)
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to hover Search button for Advanced Search: {e}")
            return None

        for _attempt in range(4):
            try:
                handle = await self.page.evaluate_handle(
                    """() => {
                        const requiredClasses = [
                            'font-medium',
                            'leading-6',
                            'flex',
                            'justify-between',
                            'items-center',
                        ];

                        const isVisible = (element) => {
                            if (!element) return false;
                            const style = window.getComputedStyle(element);
                            const rect = element.getBoundingClientRect();
                            return (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        };

                        const normalize = (value) => String(value || '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                            .toLowerCase();

                        const rows = Array.from(
                            document.querySelectorAll(
                                'div.font-medium.leading-6.flex.justify-between.items-center'
                            )
                        );

                        for (const row of rows) {
                            if (!requiredClasses.every((cls) => row.classList.contains(cls))) {
                                continue;
                            }
                            if (!isVisible(row)) {
                                continue;
                            }

                            const span = Array.from(row.children).find(
                                (child) => child.tagName === 'SPAN'
                            ) || row.querySelector('span');
                            if (!span || normalize(span.textContent) !== 'advanced search') {
                                continue;
                            }

                            const switchButton = Array.from(row.children).find(
                                (child) => child.tagName === 'BUTTON'
                            ) || row.querySelector('button[aria-checked]');
                            if (isVisible(switchButton)) {
                                return switchButton;
                            }
                        }

                        const findSwitchNear = (label) => {
                            let node = label;
                            for (let depth = 0; node && depth < 6; depth += 1) {
                                if (node.matches?.('button[aria-checked]') && isVisible(node)) {
                                    return node;
                                }

                                const switchButton = Array.from(
                                    node.querySelectorAll?.('button[aria-checked]') || []
                                ).find(isVisible);
                                if (switchButton) {
                                    return switchButton;
                                }

                                node = node.parentElement;
                            }

                            return null;
                        };

                        const labels = Array.from(
                            document.querySelectorAll('span, div, p, button')
                        ).filter((node) => (
                            isVisible(node) &&
                            normalize(node.textContent) === 'advanced search'
                        ));

                        for (const label of labels) {
                            const switchButton = findSwitchNear(label);
                            if (switchButton) {
                                return switchButton;
                            }
                        }

                        return null;
                    }"""
                )
                switch_button = handle.as_element()
                if switch_button:
                    return switch_button
            except Exception as e:
                Logger.debug(f"GLM Chat: Advanced Search switch lookup failed: {e}")

            await asyncio.sleep(0.15)

        return None

    async def set_advanced_search_state(self, state: bool) -> None:
        if not self.page:
            return

        await self._dismiss_dialog_close_buttons()
        switch_button = await self._find_advanced_search_switch()
        if not switch_button:
            if state:
                Logger.warning("GLM Chat: Advanced Search switch not found.")
            return

        try:
            attr = await switch_button.get_attribute("aria-checked")
            is_enabled = str(attr or "").strip().lower() == "true"
        except Exception:
            is_enabled = False

        if is_enabled == state:
            return

        try:
            await switch_button.evaluate("(button) => button.click()")
        except Exception as e:
            Logger.warning(f"GLM Chat: failed to toggle Advanced Search: {e}")
            return

        try:
            await asyncio.sleep(0.1)
            attr = await switch_button.get_attribute("aria-checked")
            checked_after = str(attr or "").strip().lower() == "true"
            if checked_after != state:
                Logger.warning("GLM Chat: Advanced Search did not settle to the requested state.")
        except Exception as e:
            Logger.debug(f"GLM Chat: failed to verify Advanced Search state: {e}")

    async def set_tools_state(self, state: bool, *, model_label: str | None = None) -> None:
        if not self.page:
            return

        await self._dismiss_dialog_close_buttons()
        await self._close_glm_model_dropdown()

        effective_model_label = str(model_label or "").strip() or self._get_configured_glm_model_friendly()
        supported = self._glm_tools_supported_for_model(effective_model_label)
        wanted = bool(state) and supported

        button = await self._find_tools_button()
        if not button:
            if wanted:
                Logger.warning("GLM Chat: Tools button not found.")
            return

        is_enabled = await self._read_composer_toggle_enabled(
            button,
            state_attr="data-selected",
            enabled_label_prefix="tools enabled",
        )

        if is_enabled == wanted:
            return

        if not await self._click_glm_control(button, label="Tools"):
            Logger.warning("GLM Chat: failed to toggle Tools.")

    async def upload_file(self, file_spec: Any) -> None:
        await self._upload_file(file_spec)

    async def _upload_file(self, file_spec: Any) -> None:
        if not self.page:
            return

        file_input = self.page.locator("input[type='file']")
        if await file_input.count() == 0:
            Logger.warning("GLM Chat: file input not found.")
            return

        await file_input.first.set_input_files(file_spec)
        await asyncio.sleep(0.5)

    async def enter_message(self, message: str) -> None:
        await self._enter_message(message)

    async def _enter_message(self, message: str) -> None:
        if not self.page:
            return

        textarea = self.page.locator("textarea#chat-input")
        if await textarea.count() == 0:
            Logger.warning("GLM Chat: message textarea not found.")
            return

        # Use JS to set the value through the native property setter and dispatch
        # events so that Svelte's reactive bindings pick up the change. This is
        # more reliable than Playwright's .fill() for large inputs on slow machines.
        try:
            await self.page.evaluate(
                """(msg) => {
                    const ta = document.querySelector('textarea#chat-input');
                    if (!ta) return;
                    ta.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(ta, msg);
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                message,
            )
        except Exception as e:
            Logger.debug(f"GLM Chat: JS text entry failed ({e}), falling back to .fill()")
            await textarea.first.fill(message)

    async def send_message(self, timeout: int | None = None) -> None:
        await self._send_message(timeout=timeout)

    async def _verify_send_clicked(self, poll_timeout: float = 1.5) -> bool:
        """
        Verify that the send button click actually registered.

        After a successful send, the send button's parent container changes its
        aria-label from "Send Message" to "Stop" (and the send button itself
        disappears). Poll for this state change to confirm the click went through.
        """
        if not self.page:
            return False

        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            try:
                result = await self.page.evaluate(
                    "() => {"
                    "  const btn = document.querySelector('button#send-message-button');"
                    "  if (!btn) return 'gone';"
                    "  const parent = btn.parentElement;"
                    "  if (!parent) return 'unknown';"
                    "  return (parent.getAttribute('aria-label') || '').trim().toLowerCase();"
                    "}"
                )
                if result == "gone" or result == "stop":
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.15)

        return False

    async def _click_stop_button(self) -> bool:
        if not self.page:
            return False

        stop_targets = (
            self.page.locator("div[aria-label='Stop']")
        )

        for target in stop_targets:
            try:
                if await target.count() <= 0:
                    continue
                button = target.first
                if not await button.is_visible():
                    continue
                try:
                    await button.scroll_into_view_if_needed(timeout=self._ui_timeout)
                except Exception:
                    pass
                await button.click(timeout=self._ui_timeout)
                Logger.debug("GLM Chat: Stop button clicked.")
                return True
            except Exception:
                continue

        try:
            clicked = await self.page.evaluate(
                """() => {
                    const match = document.querySelector("div[aria-label='Stop']");
                    if (!match) return false;
                    match.click();
                    return true;
                }"""
            )
            if clicked:
                Logger.debug("GLM Chat: Stop button clicked via JS fallback.")
                return True
        except Exception as e:
            Logger.debug(f"GLM Chat: JS stop click failed: {e}")

        Logger.debug("GLM Chat: Stop button not found.")
        return False

    @classmethod
    def _build_model_capacity_error_message(cls) -> str:
        return (
            "GLM Chat is currently at capacity. "
            "Please try again later or switch to another model."
        )

    @classmethod
    def _build_empty_completion_stream_error_message(cls) -> str:
        return (
            "GLM Chat returned an empty completion stream "
            f"(the condition GLM shows as Error code: {cls.EMPTY_COMPLETION_STREAM_ERROR_CODE}). "
            "IntenseRP attempted to refresh the page; please retry the request."
        )

    @staticmethod
    def _glm_frontend_would_see_sse_data_event(body: bytes | bytearray | str | None) -> bool:
        """
        Mirror GLM's own fallback check for Error code 20001.

        GLM's frontend splits the decoded stream on LF/LF and only clears its
        empty-stream fallback when a complete event block starts with "data:".
        A trailing partial event is ignored by that code path, so we ignore it too.
        """
        if body is None:
            return False
        if isinstance(body, str):
            raw = body.encode("utf-8", errors="ignore")
        else:
            raw = bytes(body)
        if not raw:
            return False

        complete_events = raw.split(b"\n\n")[:-1]
        return any(event.startswith(b"data:") for event in complete_events)

    @classmethod
    def _extract_model_capacity_error_from_data(cls, data: Any) -> str | None:
        if not isinstance(data, dict):
            return None

        raw_error = data.get("error")
        code = ""
        detail = ""
        if isinstance(raw_error, dict):
            code = str(raw_error.get("code") or "").strip().upper()
            detail = str(raw_error.get("detail") or raw_error.get("message") or "").strip()
        else:
            detail = str(raw_error or "").strip()

        normalized_detail = re.sub(r"\s+", " ", detail).strip().lower()
        if code == cls.MODEL_CONCURRENCY_LIMIT_CODE:
            return cls._build_model_capacity_error_message()
        if normalized_detail and any(marker in normalized_detail for marker in cls.MODEL_CAPACITY_TEXT_MARKERS):
            return cls._build_model_capacity_error_message()
        return None

    @classmethod
    def _extract_model_capacity_error_from_text(cls, text: str | None) -> str | None:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        if not normalized:
            return None

        if any(marker in normalized for marker in cls.MODEL_CAPACITY_TEXT_MARKERS):
            return cls._build_model_capacity_error_message()

        return None

    @classmethod
    def _describe_stream_error(cls, code: str, detail: str) -> str:
        friendly = cls.STREAM_ERROR_MESSAGES_BY_CODE.get(code)
        if friendly:
            if detail and code == cls.RISK_CONTROL_BLOCKED_ERROR_CODE:
                return f"{friendly} (server said: {detail})"
            return friendly
        suffix = f" (code: {code})" if code else ""
        if detail:
            return f"GLM Chat returned an error{suffix}: {detail}"
        return f"GLM Chat returned an error{suffix}."

    @classmethod
    def _extract_stream_error_parts(cls, data: Any) -> tuple[str, str] | None:
        """Return (code, detail) for an error carried in a chat:completion payload."""
        if not isinstance(data, dict):
            return None

        raw_error = data.get("error")
        if not raw_error:
            return None

        if isinstance(raw_error, dict):
            code = str(raw_error.get("code") or "").strip().upper()
            detail = str(raw_error.get("detail") or raw_error.get("message") or "").strip()
        else:
            code = ""
            detail = str(raw_error).strip()

        return code, detail

    @classmethod
    def _extract_stream_error_from_data(cls, data: Any) -> str | None:
        """Surface moderation/risk/captcha errors carried inside chat:completion frames.

        GLM flags prompts with codes like SENSITIVE / RISK_CONTROL_BLOCKED /
        FRONTEND_CAPTCHA_REQUIRED via data.error instead of streaming content.
        Without this, the driver would report a generic empty stream and the user
        would never learn why.
        """
        parts = cls._extract_stream_error_parts(data)
        if not parts:
            return None
        code, detail = parts
        return cls._describe_stream_error(code, detail)

    @classmethod
    def _extract_stream_error_from_text(cls, text: str | None) -> str | None:
        """Extract an error from a completion body (JSON blob or SSE lines)."""
        raw = str(text or "").strip()
        if not raw:
            return None

        # Whole-body JSON (HTTP-style error replay)
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parts = cls._extract_stream_error_parts(parsed)
            if parts:
                return cls._describe_stream_error(*parts)
            inner = parsed.get("data")
            if isinstance(inner, dict):
                parts = cls._extract_stream_error_parts(inner)
                if parts:
                    return cls._describe_stream_error(*parts)
            detail = str(
                parsed.get("detail") or parsed.get("message") or ""
            ).strip()
            if detail:
                code = str(parsed.get("code") or "").strip().upper()
                return cls._describe_stream_error(code, detail)

        # SSE lines: scan every data: frame for an error payload regardless of
        # its declared type, in case GLM introduces new frame wrappers.
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:") :].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                payload = json.loads(data_str)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            candidates = [payload]
            inner = payload.get("data")
            if isinstance(inner, dict):
                candidates.append(inner)
            for candidate in candidates:
                parts = cls._extract_stream_error_parts(candidate)
                if parts:
                    code, detail = parts
                    if code or detail:
                        return cls._describe_stream_error(code, detail)
        return None

    async def _send_message(
        self, timeout: int | None = None, arm_event: asyncio.Event | None = None
    ) -> None:
        if not self.page:
            return

        await self._close_glm_model_dropdown()

        send_button = self.page.locator("button#send-message-button")
        if await send_button.count() == 0:
            Logger.warning("GLM Chat: send button not found.")
            return

        if timeout and timeout > 0:
            start = time.time()
            while time.time() - start < timeout:
                try:
                    if not await send_button.first.is_disabled():
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.25)

        try:
            if await send_button.first.is_disabled():
                Logger.warning("GLM Chat: send button is disabled. Cannot send message.")
                return
        except Exception:
            pass

        # Brief stabilization delay to let the UI settle before clicking,
        # helps with long RPs where the button may not be fully interactive yet
        await asyncio.sleep(0.3)

        toaster = self.page.locator("ol[data-sonner-toaster]")
        max_retries = 5

        if arm_event:
            try:
                arm_event.set()
            except Exception:
                pass

        for attempt in range(max_retries):
            clicked = await self._click_glm_control(
                send_button.first,
                label="send button",
                timeout_ms=max(int(getattr(self, "_ui_timeout", 3000)), 3000),
            )
            if not clicked:
                Logger.debug("GLM Chat: send button click failed.")
                await asyncio.sleep(0.4)
                continue

            # Check if a "still uploading" toast appeared
            await asyncio.sleep(0.8)
            try:
                if await toaster.count() > 0:
                    toast_text = (await toaster.first.inner_text() or "").lower()
                    if "still uploading" in toast_text:
                        Logger.info(
                            f"GLM Chat: file still uploading (attempt {attempt + 1}/{max_retries}), "
                            f"waiting 3s before retry..."
                        )
                        await asyncio.sleep(3)
                        continue
            except Exception:
                pass

            # Verify the click actually registered (parent aria-label changes to "Stop")
            if await self._verify_send_clicked(poll_timeout=1.5):
                return

            # Playwright click didn't register -> try a direct JS click as fallback
            Logger.debug(
                f"GLM Chat: send click may not have registered (attempt {attempt + 1}/{max_retries}), "
                f"retrying with JS click..."
            )
            try:
                await self.page.evaluate(
                    "() => {"
                    "  const btn = document.querySelector('button#send-message-button');"
                    "  if (btn) btn.click();"
                    "}"
                )
            except Exception as e:
                Logger.debug(f"GLM Chat: JS click fallback failed: {e}")

            await asyncio.sleep(0.5)
            if await self._verify_send_clicked(poll_timeout=1.5):
                return

            Logger.debug(f"GLM Chat: send click not confirmed (attempt {attempt + 1}/{max_retries}).")
            await asyncio.sleep(0.3)

        Logger.warning("GLM Chat: send button click did not register after all retry attempts, giving up.")

    async def _click_regenerate(
        self,
        timeout_ms: int | None = None,
        arm_event: asyncio.Event | None = None,
    ) -> bool:
        if not self.page:
            return False

        timeout = self._ui_timeout if timeout_ms is None else max(int(timeout_ms), 0)
        deadline = time.time() + (float(timeout) / 1000.0 if timeout > 0 else 0.0)

        regen = self.page.locator("div[aria-label='Regenerate'] button")
        if await regen.count() == 0:
            regen = self.page.locator("[aria-label='Regenerate'] button")

        last_error: Exception | None = None

        while True:
            try:
                if await regen.count() > 0 and await regen.first.is_visible():
                    try:
                        aria_disabled = (await regen.first.get_attribute("aria-disabled") or "").strip().lower()
                        if aria_disabled == "true":
                            return False
                    except Exception:
                        pass

                    if arm_event:
                        try:
                            arm_event.set()
                        except Exception:
                            pass

                    try:
                        await regen.first.scroll_into_view_if_needed(timeout=self._ui_timeout)
                    except Exception:
                        pass

                    try:
                        await regen.first.click(timeout=self._ui_timeout)
                        return True
                    except Exception as e:
                        last_error = e

                    # Fallback: JS click (Playwright click can be swallowed by GLM's UI)
                    try:
                        clicked = await self.page.evaluate(
                            "() => {"
                            "  const btn = document.querySelector(\"div[aria-label='Regenerate'] button, [aria-label='Regenerate'] button\");"
                            "  if (!btn) return false;"
                            "  btn.click();"
                            "  return true;"
                            "}"
                        )
                        if clicked:
                            return True
                    except Exception as e:
                        last_error = e
            except Exception as e:
                last_error = e

            if timeout <= 0 or time.time() >= deadline:
                if last_error:
                    Logger.debug(f"GLM Chat: regenerate click failed: {last_error}")
                return False

            await asyncio.sleep(0.25)

    @staticmethod
    def _strip_details_tags(text: str) -> str:
        if not text:
            return ""
        # Remove opening <details ...> and closing </details> tags if present
        text = re.sub(r"<details[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"</details>", "", text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def _strip_glm_blocks_from_stream_chunk(text: str, in_glm_block: bool) -> tuple[str, bool]:
        """
        Strip GLM's internal <glm_block ...>...</glm_block> tool payloads from the stream.

        These blocks contain tool/search metadata and results and should never be forwarded to the client.

        Returns (cleaned_text, new_in_glm_block_state).
        """
        if not text:
            return "", bool(in_glm_block)

        start_token = "<glm_block"
        end_token = "</glm_block>"

        out: list[str] = []
        i = 0
        active = bool(in_glm_block)

        while i < len(text):
            if active:
                end = text.find(end_token, i)
                if end == -1:
                    return "".join(out), True
                i = end + len(end_token)
                active = False
                continue

            start = text.find(start_token, i)
            if start == -1:
                # If we see a closing tag without a visible opening tag in this chunk,
                # assume we're still inside a block (e.g. the opening tag was split across
                # chunks) and drop everything up to and including the closing tag
                end = text.find(end_token, i)
                if end != -1:
                    i = end + len(end_token)
                    active = False
                    continue
                out.append(text[i:])
                return "".join(out), False

            out.append(text[i:start])
            end = text.find(end_token, start)
            if end == -1:
                return "".join(out), True
            i = end + len(end_token)
            active = False

        return "".join(out), active

    @staticmethod
    def _strip_partial_details_opening_tail(text: str) -> str:
        """
        GLM sometimes emits an edit_content patch that starts mid-way through the opening
        <details ...> tag (e.g. starts with: 'true" duration="5" ...>').

        In that case, strip the remaining tag attributes up to and including the first
        '>' so we don't leak raw HTML attributes into <think> output.
        """
        if not text:
            return ""

        preview = text[:200].lower()
        if "<details" in preview:
            return text

        # Heuristic: only strip when it looks like a details tag attribute tail
        if any(token in preview for token in ("done=", "duration=", "last_tool_call", "view=")):
            gt_idx = text.find(">")
            if 0 <= gt_idx <= 200:
                return text[gt_idx + 1 :].lstrip("\n\r")

        return text

    @classmethod
    def _extract_reasoning_from_edit_content(cls, text: str) -> str:
        if not text:
            return ""

        marker = "</details>"
        idx = text.find(marker)
        if idx == -1:
            return ""

        reasoning = text[:idx]
        reasoning = cls._strip_details_tags(reasoning)
        reasoning = cls._strip_partial_details_opening_tail(reasoning)
        return reasoning

    @staticmethod
    def _extract_answer_tail_from_edit_content(text: str) -> str:
        if not text:
            return ""
        marker = "</details>"
        idx = text.rfind(marker)
        if idx == -1:
            return ""
        return text[idx + len(marker) :].lstrip("\n\r")

    @staticmethod
    def _compute_missing_suffix(emitted: str, candidate: str) -> str:
        """
        Compute the text suffix that exists in candidate but has not yet been emitted.

        GLM can resend (or patch) the full reasoning/answer content via edit_content. We
        want to forward only the newly-added tail to avoid duplicate chunks.
        """
        return compute_missing_suffix(emitted, candidate)

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "glm-auto",
        stream: bool = False,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        abort_event: asyncio.Event | None = None,
    ):
        _ = max_tokens
        response_queue: asyncio.Queue = asyncio.Queue()
        completion_armed = asyncio.Event()
        completion_started = asyncio.Event()
        completion_claim_lock = asyncio.Lock()
        completion_claimed = False
        intercepted_activity_count = 0
        intercepted_response: httpx.Response | None = None
        intercepted_request_abort = asyncio.Event()
        intercepted_request_finished = asyncio.Event()
        request_capture_mode = self._get_request_capture_mode()
        use_cdp_teeing = request_capture_mode == self.REQUEST_CAPTURE_MODE_CDP_TEEING
        route_handlers_registered = False
        cdp_session: Any = None
        cdp_listeners_registered = False
        cdp_tasks: set[asyncio.Task] = set()
        request_methods: dict[str, str] = {}
        cdp_pending_response_urls: dict[str, str] = {}
        cdp_active_request_id: Optional[str] = None
        cdp_stream_started = False
        cdp_stream_finished = False
        cdp_had_data = False
        cdp_setup_done = asyncio.Event()

        def get_intercepted_activity_count() -> int:
            return intercepted_activity_count

        async def abort_intercepted_request() -> None:
            intercepted_request_abort.set()
            response = intercepted_response
            if response is not None:
                try:
                    await response.aclose()
                except Exception as e:
                    Logger.debug(f"GLM Chat: failed to close intercepted response: {e}")
            try:
                await self._click_stop_button()
            except Exception as e:
                Logger.debug(f"GLM Chat: failed to click Stop during timeout handling: {e}")

        await self._await_pending_refresh_after_generation(abort_event=abort_event)

        await self.require_english_ui()
        self._refresh_quirks()

        # Reset state for new generation
        self.thinking_active = False
        self.abort_requested = False
        self.current_abort_event = abort_event
        resolved_model = (model or "").strip() or "glm-auto"
        self.current_model = resolved_model

        macros_overrides: Dict[str, bool] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = self._strip_glm_macros_from_messages(message)
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = self._extract_glm_macros_from_text(message)

        if macros_overrides:
            Logger.debug(f"GLM macros applied: {macros_overrides}")

        request_overrides = dict(getattr(self, "_pending_request_overrides", {}) or {})
        request_overrides.update(macros_overrides)
        effective_settings = self._resolve_glm_request_settings(resolved_model, overrides=request_overrides)
        effective_deepthink = effective_settings["deepthink_enabled"]
        deepthink_effort = str(effective_settings.get("deepthink_effort") or "").strip()
        effective_send_deepthink = effective_settings["send_deepthink"]
        enable_search = effective_settings["search_enabled"]
        enable_advanced_search = effective_settings["advanced_search_enabled"]
        enable_tools = effective_settings["tools_enabled"]
        send_as_text_file = effective_settings["send_as_text_file"]
        ui_model_label = str(
            effective_settings.get("model_label") or self._get_glm_model_label_for_request(resolved_model)
        )
        self.current_send_deepthink = effective_send_deepthink

        formatted_message = self._format_messages(message_for_formatting)
        glm_extra_prompt_texts: Dict[str, str] = {}
        if send_as_text_file:
            try:
                glm_text_file_filler = str(
                    self.config_manager.get_setting("glm_behavior", "text_file_filler") or "."
                )
            except Exception:
                glm_text_file_filler = "."
            if glm_text_file_filler.strip():
                glm_extra_prompt_texts["text_file_filler"] = glm_text_file_filler
        self._capture_diagnostics_prompt_snapshot(
            formatted_message,
            extra_prompt_texts=glm_extra_prompt_texts or None,
            metadata={
                "model": resolved_model,
                "ui_model": ui_model_label,
                "deepthink_enabled": bool(effective_deepthink),
                "deepthink_effort": deepthink_effort,
                "send_deepthink": bool(effective_send_deepthink),
                "search_enabled": bool(enable_search),
                "advanced_search_enabled": bool(enable_advanced_search),
                "tools_enabled": bool(enable_tools),
                "send_as_text_file": bool(send_as_text_file),
            },
        )

        try:
            repetition_buster_enabled = bool(
                self.config_manager.get_setting("glm_behavior", "repetition_buster")
            )
        except Exception:
            repetition_buster_enabled = False
        try:
            clean_regeneration_requested = bool(
                self.config_manager.get_setting("glm_behavior", "clean_regeneration")
            )
        except Exception:
            clean_regeneration_requested = False
        try:
            multi_slot_cache_requested = bool(
                self.config_manager.get_setting("glm_behavior", "multi_slot_cache")
            )
        except Exception:
            multi_slot_cache_requested = False
        try:
            auto_delete_requested = bool(
                self.config_manager.get_setting("glm_behavior", "auto_delete_chats")
            )
        except Exception:
            auto_delete_requested = False

        if repetition_buster_enabled and clean_regeneration_requested:
            Logger.debug(
                "Repetition Buster (GLM): Clean Regeneration is also enabled in config, "
                "but Repetition Buster takes priority."
            )

        clean_regeneration = bool((not repetition_buster_enabled) and clean_regeneration_requested)
        multi_slot_cache_enabled = bool(clean_regeneration and multi_slot_cache_requested)
        auto_delete_enabled = bool(auto_delete_requested and (not clean_regeneration))
        if auto_delete_requested and clean_regeneration:
            Logger.warning(
                "GLM Chat: Delete Chat After Reply is skipped for this request because "
                "Reuse Matching Chat is enabled."
            )
        prompt_matches_last = False
        if repetition_buster_enabled:
            prompt_matches_last = self._account_scoped_cached_prompt_matches(
                self.cache_manager,
                self.repetition_buster_cache_key,
                formatted_message,
            )
        elif clean_regeneration:
            prompt_matches_last = self._cached_prompt_matches(
                self.cache_manager,
                self.clean_regen_message_cache_key,
                formatted_message,
            )

        if repetition_buster_enabled and prompt_matches_last:
            repetition_buster_error = await self._run_repetition_buster(
                effective_deepthink=bool(effective_deepthink),
                deepthink_effort=deepthink_effort,
                enable_search=bool(enable_search),
                enable_advanced_search=bool(enable_advanced_search),
                enable_tools=bool(enable_tools),
                ui_model_label=ui_model_label,
                auto_delete_after_send=auto_delete_enabled,
            )
            if repetition_buster_error:
                self._reset_generation_state()
                self._pending_request_overrides = {}
                yield f"data: {json.dumps({'error': repetition_buster_error})}\n\n"
                return

        full_response_body = bytearray()
        text_buffer = bytearray()
        text_buffer_pos = 0
        thinking_emitted = IncrementalTextAccumulator()
        answer_emitted = False
        glm_block_active = False
        emitted_openai_chunk = False
        content_emitted = False
        openai_usage: dict[str, Any] | None = None
        openai_usage_emitted = False
        openai_finish_emitted = False
        capacity_error_message: str | None = None
        stream_error_message: str | None = None
        stream_error_code: str | None = None

        try:
            count_tokens_setting = self.config_manager.get_setting("glm_behavior", "count_tokens")
        except Exception:
            count_tokens_setting = None
        # Default to enabled, even if the setting isn't present (older configs).
        count_tokens_enabled = True if count_tokens_setting is None else bool(count_tokens_setting)

        def request_aborted() -> bool:
            return bool(
                intercepted_request_abort.is_set()
                or self.abort_requested
                or (abort_event and abort_event.is_set())
            )

        def _normalize_openai_usage(raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None

            def _to_int(value: Any) -> int | None:
                try:
                    return int(value)
                except Exception:
                    return None

            prompt_tokens = _to_int(raw.get("prompt_tokens"))
            completion_tokens = _to_int(raw.get("completion_tokens"))
            total_tokens = _to_int(raw.get("total_tokens"))

            if prompt_tokens is None and completion_tokens is None and total_tokens is None:
                return None

            prompt_tokens = 0 if prompt_tokens is None else max(prompt_tokens, 0)
            completion_tokens = 0 if completion_tokens is None else max(completion_tokens, 0)
            if total_tokens is None:
                total_tokens = prompt_tokens + completion_tokens
            else:
                total_tokens = max(total_tokens, 0)

            usage: dict[str, Any] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }

            prompt_details = raw.get("prompt_tokens_details")
            if isinstance(prompt_details, dict):
                usage["prompt_tokens_details"] = prompt_details

            completion_details = raw.get("completion_tokens_details")
            if isinstance(completion_details, dict):
                usage["completion_tokens_details"] = completion_details

            return usage

        def enqueue_openai_delta(content: str, finish_reason: str | None = None) -> None:
            nonlocal emitted_openai_chunk, content_emitted
            if (not content) and (not finish_reason):
                return
            model_name = self.current_model or "glm-auto"
            response_queue.put_nowait(
                make_openai_delta_sse(
                    model_name,
                    content,
                    finish_reason=finish_reason,
                )
            )
            emitted_openai_chunk = True
            if content:
                content_emitted = True

        def enqueue_openai_usage(usage: dict[str, Any]) -> None:
            nonlocal emitted_openai_chunk, openai_usage_emitted
            if openai_usage_emitted:
                return

            model_name = self.current_model or "glm-auto"
            response_queue.put_nowait(make_openai_usage_sse(model_name, usage))
            emitted_openai_chunk = True
            openai_usage_emitted = True

        def process_sse_line(line: str) -> None:
            nonlocal thinking_emitted, answer_emitted, glm_block_active, openai_usage
            nonlocal openai_finish_emitted, capacity_error_message
            nonlocal stream_error_message, stream_error_code
            line = line.strip()
            if not line.startswith("data:"):
                return

            data_str = line[len("data:") :].strip()
            if not data_str or data_str == "[DONE]":
                return

            try:
                payload = json.loads(data_str)
            except Exception:
                return

            if not isinstance(payload, dict):
                return

            payload_type = str(payload.get("type") or "").strip().lower()
            # Regeneration can use the same endpoint with slightly different type labels.
            if not payload_type.startswith("chat:completion"):
                return

            data = payload.get("data")
            if not isinstance(data, dict):
                return

            capacity_error_message = self._extract_model_capacity_error_from_data(data)
            if capacity_error_message:
                return

            error_parts = self._extract_stream_error_parts(data)
            if error_parts:
                code, detail = error_parts
                stream_error_code = code or None
                stream_error_message = self._describe_stream_error(code, detail)
                return

            phase = str(data.get("phase") or "").strip().lower()

            if count_tokens_enabled:
                normalized_usage = _normalize_openai_usage(data.get("usage"))
                if normalized_usage:
                    openai_usage = normalized_usage

            if phase == "done" and bool(data.get("done")):
                if not openai_finish_emitted:
                    if self.thinking_active and self.current_send_deepthink:
                        enqueue_openai_delta("</think>")
                        self.thinking_active = False
                    enqueue_openai_delta("", finish_reason="stop")
                    openai_finish_emitted = True
                return

            delta_content = data.get("delta_content")
            edit_content = data.get("edit_content")

            if isinstance(delta_content, str):
                if phase == "thinking":
                    if not self.current_send_deepthink:
                        return
                    if not self.thinking_active:
                        enqueue_openai_delta("<think>")
                        self.thinking_active = True
                    stripped = self._strip_details_tags(delta_content)
                    if stripped:
                        enqueue_openai_delta(stripped)
                        thinking_emitted.append(stripped)
                    return

                if phase == "answer":
                    if self.thinking_active and self.current_send_deepthink:
                        enqueue_openai_delta("</think>")
                        self.thinking_active = False
                    enqueue_openai_delta(delta_content)
                    answer_emitted = True
                    return

                return

            if isinstance(edit_content, str):
                edit_content, glm_block_active = self._strip_glm_blocks_from_stream_chunk(
                    edit_content, glm_block_active
                )

                # When thinking is enabled, GLM often sends a huge edit_content that includes the full
                # <details> reasoning plus the first token(s) of the answer. Extract only the answer tail.
                if phase == "answer":
                    if self.current_send_deepthink and (self.thinking_active or not answer_emitted):
                        reasoning = self._extract_reasoning_from_edit_content(edit_content)
                        if reasoning:
                            missing = thinking_emitted.missing_suffix(reasoning)
                            if missing:
                                if not self.thinking_active:
                                    enqueue_openai_delta("<think>")
                                    self.thinking_active = True
                                enqueue_openai_delta(missing)
                                thinking_emitted.append(missing)

                    if self.thinking_active and self.current_send_deepthink:
                        enqueue_openai_delta("</think>")
                        self.thinking_active = False
                    tail = self._extract_answer_tail_from_edit_content(edit_content)
                    if tail:
                        enqueue_openai_delta(tail)
                        answer_emitted = True
                    return

                # GLM sometimes finalizes the answer with an "other" edit_content frame (tail append).
                if phase == "other":
                    if self.thinking_active and self.current_send_deepthink:
                        enqueue_openai_delta("</think>")
                        self.thinking_active = False
                    if edit_content:
                        enqueue_openai_delta(edit_content)
                        answer_emitted = True
                    return

                # At this point answer content may already be streaming via delta_content; only
                # treat tool_call edit_content as answer tail if we've already emitted answer.
                if phase == "tool_call":
                    if self.thinking_active and self.current_send_deepthink:
                        enqueue_openai_delta("</think>")
                        self.thinking_active = False
                    if answer_emitted and edit_content:
                        enqueue_openai_delta(edit_content)
                        answer_emitted = True
                    return

        def process_glm_stream_chunk(chunk: bytes) -> None:
            nonlocal intercepted_activity_count, text_buffer_pos
            if not chunk:
                return

            intercepted_activity_count += 1
            full_response_body.extend(chunk)
            if capacity_error_message or stream_error_message:
                return

            text_buffer.extend(chunk)

            while True:
                newline_idx = text_buffer.find(b"\n", text_buffer_pos)
                if newline_idx == -1:
                    break

                line_bytes = text_buffer[text_buffer_pos:newline_idx]
                text_buffer_pos = newline_idx + 1
                try:
                    process_sse_line(bytes(line_bytes).decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                if capacity_error_message or stream_error_message:
                    break

            if text_buffer_pos > 8192:
                del text_buffer[:text_buffer_pos]
                text_buffer_pos = 0

        def finalize_glm_stream_processing(*, aborted: bool = False) -> None:
            nonlocal text_buffer_pos
            if not capacity_error_message and not stream_error_message:
                tail = bytes(text_buffer[text_buffer_pos:])
                if tail.strip():
                    try:
                        process_sse_line(tail.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
            text_buffer.clear()
            text_buffer_pos = 0

            if (
                (not aborted)
                and (not request_aborted())
                and (not capacity_error_message)
                and (not stream_error_message)
                and count_tokens_enabled
                and (openai_usage is not None)
                and (not openai_usage_emitted)
            ):
                enqueue_openai_usage(openai_usage)

        async def finish_glm_stream_result(
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            if aborted or request_aborted():
                Logger.warning("GLM Chat generation was aborted before completion.")

            should_report_empty_stream_error = bool(
                (not aborted)
                and (not encountered_error)
                and (not request_aborted())
                and (not capacity_error_message)
                and (not stream_error_message)
                and (not content_emitted)
            )

            if capacity_error_message:
                Logger.warning(capacity_error_message)
                await self._refresh_page_after_capacity_error()
                response_queue.put_nowait(
                    f"data: {json.dumps({'error': capacity_error_message})}\n\n"
                )
            elif stream_error_message:
                Logger.warning(stream_error_message)
                if stream_error_code in self.CAPTCHA_ERROR_CODES:
                    self.notify_user(
                        "GLM Chat Verification Required",
                        "GLM Chat is asking for a CAPTCHA. Solve it in the GLM Chat "
                        "browser window, then retry the request.",
                        level="warning",
                    )
                    if not self.abort_requested and not (abort_event and abort_event.is_set()):
                        await self._reload_chat_page("CAPTCHA verification required")
                response_queue.put_nowait(
                    f"data: {json.dumps({'error': stream_error_message})}\n\n"
                )
            elif should_report_empty_stream_error:
                body_text = full_response_body.decode("utf-8", errors="ignore")
                msg = self._extract_model_capacity_error_from_text(body_text)
                if msg:
                    await self._refresh_page_after_capacity_error()
                if not msg:
                    msg = self._extract_stream_error_from_text(body_text)
                captcha_recent = (time.time() - getattr(self, "_last_captcha_notice_ts", 0.0)) < 90.0
                if not msg:
                    if self._glm_frontend_would_see_sse_data_event(full_response_body):
                        # SSE frames arrived but nothing was streamable. If the
                        # frames carry no content fields at all, this matches
                        # GLM's silent-refusal signature exactly (a lone
                        # phase:"done" frame) - surface moderation, not an API
                        # change warning.
                        if (
                            "delta_content" not in body_text
                            and "edit_content" not in body_text
                            and "content" not in body_text
                        ):
                            msg = (
                                "GLM Chat silently refused this prompt - the server "
                                "returned no content (shadow censorship). Try "
                                "rephrasing the prompt or trimming the conversation "
                                "history."
                            )
                        else:
                            msg = (
                                "GLM Chat: intercepted completion produced no streamable output. "
                                "This may indicate a GLM API / frontend change."
                            )
                    else:
                        msg = self._build_empty_completion_stream_error_message()
                        if captcha_recent:
                            msg += (
                                " FeiLin CAPTCHA activity was detected during this request - "
                                "check the GLM browser window for a verification popup."
                            )
                        await self._reload_chat_page(
                            "empty completion stream "
                            f"(GLM Error code: {self.EMPTY_COMPLETION_STREAM_ERROR_CODE})"
                        )
                Logger.warning(msg)
                Logger.debug(
                    "GLM Chat: raw completion body head for diagnosis: "
                    + repr(body_text[:800])
                )
                response_queue.put_nowait(f"data: {json.dumps({'error': msg})}\n\n")

            await response_queue.put(None)
            intercepted_request_finished.set()
            if (
                not aborted
                and (not encountered_error)
                and (not request_aborted())
                and (not capacity_error_message)
                and (not stream_error_message)
            ):
                if content_emitted:
                    Logger.success("GLM Chat response streaming completed.")
                else:
                    Logger.warning(
                        "GLM Chat: completion finished without any streamable content "
                        "(metadata-only response - likely a silent refusal)."
                    )

        async def handle_route(route):
            nonlocal completion_claimed, intercepted_response
            request = route.request

            # Ignore preflight and any requests we aren't actively expecting to stream
            # GLM can sometimes fire background requests to the same endpoint
            try:
                method = str(request.method or "").upper()
            except Exception:
                method = ""
            if method == "OPTIONS":
                await route.continue_()
                return

            if not completion_armed.is_set():
                await route.continue_()
                return

            async with completion_claim_lock:
                if completion_claimed:
                    await route.continue_()
                    return
                completion_claimed = True
                completion_started.set()

            Logger.info("Intercepting GLM Chat API request...")
            Logger.debug(f"Intercepted request to: {request.url}")

            headers = await request.all_headers()
            headers.pop("content-length", None)
            headers.pop("host", None)

            cookies = await self.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            response_headers: Dict[str, str] = {}
            aborted = False
            encountered_error = False

            try:
                json_body = None
                raw_post_data = request.post_data
                if raw_post_data:
                    try:
                        json_body = request.post_data_json
                    except Exception:
                        json_body = None

                client = await self._get_http_client()
                try:
                    request_kwargs: Dict[str, Any] = {
                        "headers": headers,
                        "cookies": cookie_dict,
                        "timeout": 60.0,
                    }
                    if json_body is not None:
                        request_kwargs["json"] = json_body
                    elif raw_post_data:
                        request_kwargs["content"] = str(raw_post_data).encode("utf-8")

                    async with client.stream(
                        request.method,
                        request.url,
                        **request_kwargs,
                    ) as response:
                        intercepted_response = response
                        for k, v in response.headers.items():
                            response_headers[k] = v

                        async for chunk in response.aiter_bytes():
                            if request_aborted():
                                Logger.debug("Abort detected during GLM streaming, stopping...")
                                aborted = True
                                break

                            process_glm_stream_chunk(chunk)
                            if capacity_error_message or stream_error_message:
                                break

                        if not capacity_error_message and not stream_error_message:
                            finalize_glm_stream_processing(aborted=aborted)
                except httpx.ReadError as e:
                    if not aborted and not request_aborted():
                        encountered_error = True
                        Logger.error(f"Read error during GLM intercepted request: {e}")
                        response_queue.put_nowait(f"data: {json.dumps({'error': str(e)})}\n\n")
                except Exception as e:
                    if not aborted and not request_aborted():
                        encountered_error = True
                        Logger.error(f"Error during GLM intercepted request: {e}")
                        response_queue.put_nowait(f"data: {json.dumps({'error': str(e)})}\n\n")
            except RuntimeError as e:
                if "async generator" in str(e) or "cancel scope" in str(e):
                    Logger.debug(f"Ignored expected error during abort: {e}")
                else:
                    raise
            finally:
                intercepted_response = None

            if aborted or request_aborted():
                try:
                    await route.abort()
                except Exception as e:
                    Logger.error(f"GLM Chat: error finalizing route: {e}")
            else:
                try:
                    await route.fulfill(
                        body=bytes(full_response_body),
                        status=200,
                        headers=response_headers,
                    )
                except Exception as e:
                    Logger.error(f"GLM Chat: error finalizing route: {e}")

            await finish_glm_stream_result(
                aborted=aborted,
                encountered_error=encountered_error,
            )

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"GLM Chat: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"GLM Chat: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        async def finish_cdp_stream(
            request_id: str,
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            nonlocal cdp_stream_finished
            if request_id != cdp_active_request_id or cdp_stream_finished:
                return

            # Don't finalize while stream setup (or body recovery) is still in
            # flight - otherwise loadingFinished could race ahead and drop the
            # first buffered chunk.
            if not cdp_setup_done.is_set():
                try:
                    await asyncio.wait_for(cdp_setup_done.wait(), timeout=5.0)
                except Exception:
                    pass
                if request_id != cdp_active_request_id or cdp_stream_finished:
                    return

            cdp_stream_finished = True
            if not aborted and not encountered_error:
                finalize_glm_stream_processing(aborted=False)

            await finish_glm_stream_result(
                aborted=aborted,
                encountered_error=encountered_error,
            )
            request_methods.pop(request_id, None)
            cdp_pending_response_urls.pop(request_id, None)

        async def feed_cdp_stream_chunk(request_id: str, data: bytes) -> None:
            nonlocal cdp_had_data
            if request_id != cdp_active_request_id or not data or cdp_stream_finished:
                return

            cdp_had_data = True
            if request_aborted():
                await finish_cdp_stream(request_id, aborted=True)
                return

            process_glm_stream_chunk(data)
            if request_aborted():
                await finish_cdp_stream(request_id, aborted=True)

        async def feed_base64_cdp_stream_chunk(request_id: str, encoded_data: Any) -> None:
            if not encoded_data:
                return
            encoded_text = str(encoded_data)
            try:
                data = base64.b64decode(encoded_text, validate=True)
            except Exception:
                data = encoded_text.encode("utf-8", errors="ignore")
            await feed_cdp_stream_chunk(request_id, data)

        async def start_cdp_stream(request_id: str, url: str) -> None:
            nonlocal cdp_stream_started
            if (
                request_id != cdp_active_request_id
                or not cdp_session
                or cdp_stream_started
                or cdp_stream_finished
            ):
                return

            cdp_stream_started = True
            Logger.info("Teeing GLM Chat API response via CDP...")
            Logger.debug(f"Teeing request to: {url}")

            async def recover_finished_cdp_body() -> None:
                # Fast-failing responses (moderation/captcha refusals) can finish
                # loading before we attach streaming. Recover the buffered body so
                # the real error reaches the client instead of an empty stream.
                Logger.debug(
                    "GLM Chat CDP: response finished before streaming attached; "
                    "recovering buffered body..."
                )
                try:
                    result = await cdp_session.send(
                        "Network.getResponseBody",
                        {"requestId": request_id},
                    )
                except Exception as exc:
                    raise RuntimeError(f"body recovery failed: {exc}") from exc

                if isinstance(result, dict):
                    if result.get("base64Encoded"):
                        encoded_text = str(result.get("body") or "")
                        try:
                            recovered = base64.b64decode(encoded_text, validate=True)
                        except Exception:
                            recovered = encoded_text.encode("utf-8", errors="ignore")
                        Logger.debug(
                            "GLM Chat CDP: recovered buffered body "
                            f"({len(recovered)} bytes, base64)."
                        )
                        await feed_cdp_stream_chunk(request_id, recovered)
                    else:
                        body_text = str(result.get("body") or "")
                        Logger.debug(
                            "GLM Chat CDP: recovered buffered body "
                            f"({len(body_text)} chars, text)."
                        )
                        await feed_cdp_stream_chunk(
                            request_id,
                            body_text.encode("utf-8", errors="ignore"),
                        )

            fatal_message: str | None = None
            try:
                try:
                    result = await cdp_session.send(
                        "Network.streamResourceContent",
                        {"requestId": request_id},
                    )
                except Exception as exc:
                    if "already finished loading" in str(exc):
                        await recover_finished_cdp_body()
                        return
                    raise

                if isinstance(result, dict):
                    await feed_base64_cdp_stream_chunk(request_id, result.get("bufferedData"))
            except Exception as exc:
                fatal_message = f"GLM Chat CDP response streaming failed: {exc}"
            finally:
                cdp_setup_done.set()

            if fatal_message:
                Logger.error(fatal_message)
                await response_queue.put({"error": fatal_message})
                await finish_cdp_stream(request_id, encountered_error=True)

        async def handle_cdp_request_will_be_sent(params: Any) -> None:
            nonlocal completion_claimed, cdp_active_request_id
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request = params.get("request")
            if not request_id or not isinstance(request, dict):
                return

            method = str(request.get("method") or "").upper()
            request_methods[request_id] = method
            url = str(request.get("url") or "")
            if method != "POST" or not self._is_completion_request_url(url):
                return
            if not completion_armed.is_set():
                return

            async with completion_claim_lock:
                if completion_claimed:
                    return
                completion_claimed = True
                cdp_active_request_id = request_id
                completion_started.set()

            Logger.info("Observing GLM Chat API request via CDP...")
            Logger.debug(f"Observed request to: {url}")
            pending_url = cdp_pending_response_urls.get(request_id)
            if pending_url:
                await start_cdp_stream(request_id, pending_url)

        async def handle_cdp_response_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            response = params.get("response")
            if not request_id or not isinstance(response, dict):
                return
            url = str(response.get("url") or "")
            if not self._is_completion_request_url(url):
                return
            method = request_methods.get(request_id, "").upper()
            if method and method != "POST":
                return
            cdp_pending_response_urls[request_id] = url
            if request_id != cdp_active_request_id:
                return
            await start_cdp_stream(request_id, url)

        async def handle_cdp_data_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            if request_id == cdp_active_request_id:
                await feed_base64_cdp_stream_chunk(request_id, params.get("data"))

        async def handle_cdp_loading_finished(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            if request_id == cdp_active_request_id:
                await finish_cdp_stream(request_id)
            else:
                request_methods.pop(request_id, None)
                cdp_pending_response_urls.pop(request_id, None)

        async def handle_cdp_loading_failed(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            if request_id != cdp_active_request_id:
                request_methods.pop(request_id, None)
                cdp_pending_response_urls.pop(request_id, None)
                return

            if request_aborted():
                await finish_cdp_stream(request_id, aborted=True)
                return

            error_text = str(params.get("errorText") or "network loading failed").strip()
            if "ERR_ABORTED" in error_text.upper() and cdp_had_data:
                Logger.debug(
                    "GLM Chat CDP stream ended with net::ERR_ABORTED after data arrived; "
                    "treating it as complete."
                )
                await finish_cdp_stream(request_id)
                return

            message_text = f"GLM Chat CDP stream failed: {error_text}"
            Logger.error(message_text)
            await response_queue.put({"error": message_text})
            await finish_cdp_stream(request_id, encountered_error=True)

        def on_cdp_request_will_be_sent(params: Any) -> None:
            _schedule_cdp_task(handle_cdp_request_will_be_sent(params), "requestWillBeSent")

        def on_cdp_response_received(params: Any) -> None:
            _schedule_cdp_task(handle_cdp_response_received(params), "responseReceived")

        def on_cdp_data_received(params: Any) -> None:
            _schedule_cdp_task(handle_cdp_data_received(params), "dataReceived")

        def on_cdp_loading_finished(params: Any) -> None:
            _schedule_cdp_task(handle_cdp_loading_finished(params), "loadingFinished")

        def on_cdp_loading_failed(params: Any) -> None:
            _schedule_cdp_task(handle_cdp_loading_failed(params), "loadingFailed")

        route_owner = self.context or self.page
        if not route_owner:
            raise RuntimeError("GLM Chat: browser context is not available.")

        try:
            if use_cdp_teeing:
                if not self.context or not self.page:
                    message_text = "GLM Chat CDP setup failed: browser context is not available."
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
                try:
                    cdp_session = await self.context.new_cdp_session(self.page)
                    await cdp_session.send("Network.enable", {})
                    cdp_session.on("Network.requestWillBeSent", on_cdp_request_will_be_sent)
                    cdp_session.on("Network.responseReceived", on_cdp_response_received)
                    cdp_session.on("Network.dataReceived", on_cdp_data_received)
                    cdp_session.on("Network.loadingFinished", on_cdp_loading_finished)
                    cdp_session.on("Network.loadingFailed", on_cdp_loading_failed)
                    cdp_listeners_registered = True
                    Logger.info("GLM Chat Request Capture Mode: CDP Teeing.")
                except Exception as exc:
                    message_text = f"GLM Chat CDP setup failed: {exc}"
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
            else:
                await route_owner.route(self.COMPLETION_ROUTE_GLOB, handle_route)
                route_handlers_registered = True
                Logger.info("GLM Chat Request Capture Mode: Replay.")

            regenerated = False
            clean_regen_state: Dict[str, Any] | None = None
            multi_slot_state: Dict[str, Any] | None = None
            current_cache_matched = False
            should_record_multi_slot = False

            if clean_regeneration:
                clean_regen_state = {
                    "deepthink_enabled": bool(effective_deepthink),
                    "deepthink_effort": deepthink_effort,
                    "search_enabled": bool(enable_search),
                    "advanced_search_enabled": bool(enable_advanced_search),
                    "tools_enabled": bool(enable_tools),
                    "send_as_text_file": bool(send_as_text_file),
                    "ui_model": ui_model_label,
                }
                multi_slot_state = self._build_multi_slot_cache_state(
                    effective_deepthink=bool(effective_deepthink),
                    deepthink_effort=deepthink_effort,
                    enable_search=bool(enable_search),
                    enable_advanced_search=bool(enable_advanced_search),
                    enable_tools=bool(enable_tools),
                    send_as_text_file=bool(send_as_text_file),
                    ui_model_label=ui_model_label,
                )

                last_state = self._read_clean_regeneration_state()

                message_matches = prompt_matches_last
                state_matches = last_state == clean_regen_state

                if message_matches and state_matches:
                    current_cache_matched = True
                    Logger.info(
                        "Clean Regeneration (GLM): Message and settings match cache. Attempting to regenerate..."
                    )

                    #  toggles must match before regenerating (GLM UI can reset them on refresh)
                    try:
                        await self.set_tools_state(enable_tools, model_label=ui_model_label)
                        await self.set_deepthink_state(
                            effective_deepthink,
                            effort=deepthink_effort,
                            model_label=ui_model_label,
                        )
                        await self.set_search_state(enable_search)
                        await self.set_advanced_search_state(enable_advanced_search)
                        await asyncio.sleep(self._post_delay_s)
                    except Exception:
                        pass

                    regen_timeout = max(int(getattr(self, "_ui_timeout", 3000)), 5000)
                    if await self._click_regenerate(timeout_ms=regen_timeout, arm_event=completion_armed):
                        Logger.info("Clean Regeneration (GLM): Regenerate clicked. Regenerating...")
                        try:
                            await asyncio.wait_for(
                                completion_started.wait(),
                                timeout=self._completion_request_timeout_s,
                            )
                        except asyncio.TimeoutError:
                            Logger.warning(
                                "Clean Regeneration (GLM): completion request not observed after clicking "
                                "Regenerate. Falling back to new chat."
                            )
                        else:
                            regenerated = True
                            self._write_cached_prompt(
                                self.cache_manager,
                                self.clean_regen_message_cache_key,
                                formatted_message,
                            )
                            self._write_clean_regeneration_state(clean_regen_state)
                    else:
                        Logger.warning(
                            "Clean Regeneration (GLM): Regenerate button not found/visible. Falling back to new chat."
                        )
                elif message_matches and not state_matches:
                    Logger.info(
                        "Clean Regeneration (GLM): Message matches cache but settings changed. Creating new chat."
                    )
                else:
                    Logger.debug("Clean Regeneration (GLM): Message differs from cache. Creating new chat.")

            if (
                (not regenerated)
                and multi_slot_cache_enabled
                and multi_slot_state
                and (not current_cache_matched)
            ):
                regenerated = await self._try_multi_slot_regeneration(
                    formatted_message=formatted_message,
                    multi_slot_state=multi_slot_state,
                    completion_armed=completion_armed,
                    completion_started=completion_started,
                )
                if regenerated and clean_regen_state:
                    self._write_cached_prompt(
                        self.cache_manager,
                        self.clean_regen_message_cache_key,
                        formatted_message,
                    )
                    self._write_clean_regeneration_state(clean_regen_state)

            if not regenerated:
                await self._prepare_new_chat_request_ui(
                    effective_deepthink=bool(effective_deepthink),
                    deepthink_effort=deepthink_effort,
                    enable_search=bool(enable_search),
                    enable_advanced_search=bool(enable_advanced_search),
                    enable_tools=bool(enable_tools),
                    ui_model_label=ui_model_label,
                )

                if send_as_text_file:
                    Logger.info("GLM Chat: sending message as text file...")
                    file_payload = build_prompt_text_file_payload(formatted_message)
                    await self._upload_file(file_payload)

                    # GLM requires some text alongside the file to enable the send button
                    filler = self.config_manager.get_setting("glm_behavior", "text_file_filler") or "."
                    await self._enter_message(str(filler))
                    # It Just Works™
                    # Copyright © ONCE IN A LIFETIME Bethesda Softworks LLC

                    upload_timeout = int(self.config_manager.get_setting("glm_behavior", "file_upload_timeout") or 15)
                    Logger.info("GLM Chat: sending request...")
                    await self._send_message(timeout=upload_timeout, arm_event=completion_armed)
                else:
                    await self._send_text_request(
                        formatted_message,
                        send_timeout=self._msg_send_timeout,
                        arm_event=completion_armed,
                    )

                if clean_regeneration and clean_regen_state:
                    self._write_cached_prompt(
                        self.cache_manager,
                        self.clean_regen_message_cache_key,
                        formatted_message,
                    )
                    self._write_clean_regeneration_state(clean_regen_state)
                    should_record_multi_slot = bool(multi_slot_cache_enabled and multi_slot_state)
                elif repetition_buster_enabled:
                    self._write_account_scoped_cached_prompt(
                        self.cache_manager,
                        self.repetition_buster_cache_key,
                        formatted_message,
                    )

            if not completion_started.is_set():
                try:
                    await asyncio.wait_for(
                        completion_started.wait(),
                        timeout=self._completion_request_timeout_s,
                    )
                except asyncio.TimeoutError:
                    Logger.error(
                        "GLM Chat: completion request was not observed. "
                        "The UI may have swallowed the click or the endpoint changed."
                    )
                    yield f"data: {json.dumps({'error': 'GLM: completion request not observed'})}\n\n"
                    return

            stream_completed = True
            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self._first_chunk_timeout_s,
                idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                on_timeout=abort_intercepted_request,
                activity_counter=get_intercepted_activity_count,
            ):
                if isinstance(item, dict) and "error" in item:
                    stream_completed = False
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                yield item

            if should_record_multi_slot and stream_completed and not self.abort_requested and not (abort_event and abort_event.is_set()):
                conversation_info = await self._wait_for_current_conversation_info(timeout_ms=6000)
                if conversation_info is None:
                    Logger.debug(
                        "Multi-Slot Cache (GLM): could not resolve conversation URL after generation; "
                        "skipping cache save."
                    )
                else:
                    upsert_multi_slot_cache_entry(
                        self.cache_manager,
                        self.multi_slot_cache_key,
                        self._get_multi_slot_cache_account_key(),
                        {
                            "conversation_id": conversation_info["conversation_id"],
                            "conversation_url": conversation_info["conversation_url"],
                            "prompt": formatted_message,
                            "state": multi_slot_state,
                        },
                        log_label="Multi-Slot Cache (GLM)",
                    )

            if stream_completed and not self.abort_requested and not (abort_event and abort_event.is_set()):
                self._schedule_refresh_after_generation()

            if (
                auto_delete_enabled
                and stream_completed
                and not self.abort_requested
                and not (abort_event and abort_event.is_set())
            ):
                await self._auto_delete_current_chat()

        finally:
            if completion_started.is_set() and not intercepted_request_finished.is_set():
                try:
                    await asyncio.wait_for(intercepted_request_finished.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    Logger.debug("GLM Chat: timed out waiting for intercepted request cleanup.")
            self._reset_generation_state()
            self._pending_request_overrides = {}
            if route_handlers_registered:
                try:
                    await route_owner.unroute(self.COMPLETION_ROUTE_GLOB, handle_route)
                except Exception:
                    try:
                        await route_owner.unroute(self.COMPLETION_ROUTE_GLOB)
                    except Exception:
                        pass
            if cdp_session and cdp_listeners_registered:
                for event_name, listener in (
                    ("Network.requestWillBeSent", on_cdp_request_will_be_sent),
                    ("Network.responseReceived", on_cdp_response_received),
                    ("Network.dataReceived", on_cdp_data_received),
                    ("Network.loadingFinished", on_cdp_loading_finished),
                    ("Network.loadingFailed", on_cdp_loading_failed),
                ):
                    try:
                        cdp_session.remove_listener(event_name, listener)
                    except Exception:
                        pass
            for task in list(cdp_tasks):
                if not task.done():
                    task.cancel()
            tasks_to_wait = set(cdp_tasks)
            if tasks_to_wait:
                try:
                    await asyncio.wait(tasks_to_wait, timeout=1.0)
                except Exception:
                    pass
            if cdp_session:
                try:
                    await cdp_session.detach()
                except Exception as exc:
                    Logger.debug(f"GLM Chat: CDP detach failed: {exc}")
