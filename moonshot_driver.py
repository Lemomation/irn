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
    build_prompt_text_file_payload,
    clear_clean_regeneration_cache,
    extract_macro_overrides,
    find_multi_slot_cache_entry,
    format_request_messages,
    make_openai_delta_sse,
    read_clean_regeneration_state,
    read_multi_slot_cache_payload,
    remove_multi_slot_cache_entry,
    strip_macros_from_messages,
    upsert_multi_slot_cache_entry,
    write_clean_regeneration_state,
)
from utils.cache_manager import CacheManager
from utils.logger import Logger
from utils.model_ids import (
    MODE_CHAT,
    MODE_REASONER,
    resolve_behavior_mode,
    resolve_real_model_label_from_model_id,
)

load_dotenv()


class MoonshotDriver(BaseDriver):
    REQUEST_CAPTURE_MODE_REPLAY = "replay"
    REQUEST_CAPTURE_MODE_CDP_TEEING = "cdp_teeing"
    CHAT_ROUTE_GLOB = "**/apiv2/kimi.gateway.chat.v1.ChatService/Chat*"
    REGEN_ROUTE_GLOB = "**/apiv2/kimi.gateway.chat.v1.ChatService/RegenerateMessage*"
    COMPLETION_URL_PATH_PREFIXES = (
        "/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
        "/apiv2/kimi.gateway.chat.v1.ChatService/RegenerateMessage",
    )
    USER_SETTINGS_ROUTE_GLOB = "**/apiv2/kimi.usersetting.v1.UserSettingService/GetUserSetting*"
    # Kimi runs the same app on two regional domains: kimi.com (CN) and
    # kimi.ai (overseas). New sessions start on the overseas origin, but every
    # host check accepts both so existing CN sessions keep working.
    SITE_ORIGIN = "https://www.kimi.ai"
    SITE_ORIGIN_PEER = "https://www.kimi.com"
    SITE_HOSTS = {"www.kimi.com", "kimi.com", "www.kimi.ai", "kimi.ai"}
    USER_SETTINGS_UPDATE_PATH = "/apiv2/kimi.usersetting.v1.UserSettingService/UpdateUserSetting"
    NEW_CHAT_URL = "https://www.kimi.ai/?chat_enter_method=new_chat"
    AUTH_HOST_MARKER = "accounts.google.com"
    MEMORY_DISABLE_UPDATE_PAYLOAD = {
        "user_setting": {"memory": {}},
        "update_mask": "memory.useSemanticMemory",
    }
    SETTINGS_REQUEST_HEADER_ALLOWLIST = {
        "authorization",
        "accept",
        "accept-language",
        "dnt",
        "x-language",
        "x-msh-device-id",
        "x-msh-platform",
        "x-msh-session-id",
        "x-msh-version",
        "x-traffic-id",
    }
    CONNECT_MAX_FRAME_BYTES = 8 * 1024 * 1024
    RATE_LIMIT_TEXT_MARKERS = (
        "too many people are chatting with kimi",
        "dedicated priority queue",
        "rate limit",
        "rate_limit",
        "ratelimit",
        "too many requests",
    )

    @classmethod
    def _build_capacity_error_message(cls) -> str:
        return (
            "Kimi is currently at capacity (too many concurrent chats). "
            "Retry shortly, switch models, or subscribe to Kimi for the priority queue."
        )

    @classmethod
    def _extract_rate_limit_error_from_text(cls, text: Any) -> str | None:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        if not normalized:
            return None
        # The exact Kimi popup phrase first - distinctive enough to scan raw
        # response bytes safely - then generic markers for structured fields.
        if "too many people are chatting with kimi" in normalized:
            return cls._build_capacity_error_message()
        if any(marker in normalized for marker in cls.RATE_LIMIT_TEXT_MARKERS):
            return cls._build_capacity_error_message()
        return None

    def _scan_chunk_for_rate_limit(self, data: bytes, error_sink) -> bool:
        """Scan raw response bytes for rate-limit signals.

        Rate-limit responses may arrive outside the connect protocol (plain
        JSON errors, HTML fragments), so notification parsing alone is not
        enough. The primary marker phrase is specific to Kimi's throttle popup.
        """
        if error_sink is None:
            return False
        try:
            text = bytes(data).decode("utf-8", errors="ignore")
        except Exception:
            return False
        if "too many people are chatting with kimi" not in text.lower():
            return False
        message_text = self._extract_rate_limit_error_from_text(text)
        if message_text:
            error_sink(message_text)
            return True
        return False

    MODEL_INSTANT = "Instant"
    MODEL_THINKING = "K2.6 Thinking"
    MODEL_K28 = "K2.8"
    MODEL_K3 = "Kimi K3"
    MODEL_K3_SWARM = "Kimi K3 Swarm"
    # Picker labels change between Kimi rollouts ("K2.6 Instant" -> "Instant"),
    # so each known model maps to accepted normalized-name aliases.
    # "Instant" and "K2.8" are separate picker entries: K2.8 must select the
    # K2.8 entry, not be treated as a stand-in for Instant.
    MODEL_MATCH_ALIASES = {
        "instant": ("instant",),
        "k2.6 instant": ("instant",),
        "kimi instant": ("instant",),
        "thinking": ("thinking",),
        "k2.6 thinking": ("thinking",),
        "kimi thinking": ("thinking",),
        "k2.8": ("k2.8", "k2.8 preview", "kimi k2.8"),
        "k2.8 preview": ("k2.8", "k2.8 preview", "kimi k2.8"),
        "kimi k2.8": ("k2.8", "k2.8 preview", "kimi k2.8"),
        "kimi k3": ("kimi k3", "k3"),
        "k3": ("k3",),
        "kimi k3 swarm": ("kimi k3 swarm", "k3 swarm"),
        "k3 swarm": ("k3 swarm",),
    }
    MODEL_CHAT_API = "moonshot-chat"
    MODEL_REASONER_API = "moonshot-reasoner"
    INTERCEPT_FIRST_CHUNK_TIMEOUT_S = 45.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    COMPLETION_REQUEST_TIMEOUT_S = 20.0
    AUTH_STATE_SETTLE_TIMEOUT_MS = 12000
    AUTH_STATE_STABLE_SIGNED_OUT_MS = 1800
    GOOGLE_AUTO_LOGIN_TIMEOUT_MS = 20000
    LOGIN_LABEL_ALIASES = {"log in", "login", "sign in", "signin"}
    LEGACY_PLACEHOLDER_EMAILS = {"test1@notanemail.notanemail"}
    GOOGLE_EMAIL_SELECTORS = [
        "input#identifierId",
        "input[type='email']",
        "input[autocomplete='username']",
        "input[name='identifier']",
    ]
    GOOGLE_PASSWORD_SELECTORS = [
        "input[type='password']",
        "input[name='Passwd']",
        "input[autocomplete='current-password']",
    ]
    GOOGLE_CONTINUE_SELECTORS = [
        "button:has-text('Continue')",
        "[role='button']:has-text('Continue')",
        "div[role='button']:has-text('Continue')",
        "button:has-text('Continue as')",
        "[role='button']:has-text('Continue as')",
        "button:has-text('Allow')",
        "[role='button']:has-text('Allow')",
    ]
    GOOGLE_BUTTON_FALLBACK_SELECTOR = "button.VfPpkd-LgbsSe, div[role='button'].VfPpkd-LgbsSe"
    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.MOONSHOT)
        self.cache_manager = CacheManager()

        self.current_model = None
        self.current_ui_model = ""
        self.current_send_deepthink = None
        self.thinking_active = False
        self._search_ui_unavailable_warned = False

        self.clean_regen_message_cache_key = "moonshot_last_message.txt"
        self.clean_regen_state_cache_key = "moonshot_last_message_state.json"
        self.multi_slot_cache_key = "moonshot_multi_slot_cache.json"

        self._connect_buffer = bytearray()
        self._search_and_think_warned = False
        self._degrade_notice_logged = False
        self._memory_settings_disable_lock = asyncio.Lock()
        self._memory_settings_last_attempt_ts: float = 0.0
        self._last_followup_request_headers: Dict[str, str] = {}

    def get_start_url(self) -> str:
        return f"{self._get_configured_site_origin()}/"

    def _get_configured_site_origin(self) -> str:
        """Return the Kimi origin preferred by the user's region setting."""
        try:
            region = str(
                self.config_manager.get_setting("moonshot_behavior", "site_region") or ""
            ).strip().lower()
        except Exception:
            region = ""
        if region == "cn":
            return self.SITE_ORIGIN_PEER
        return self.SITE_ORIGIN

    def _get_site_origin(self) -> str:
        """Return the origin the live session currently lives on.

        Kimi migrates sessions between kimi.com (CN) and kimi.ai (overseas),
        so API calls must target whichever host the page actually uses.
        Falls back to the user's configured region when unknown.
        """
        if self.page:
            try:
                parsed = urlsplit(str(self.page.url or ""))
                netloc = str(parsed.netloc or "").strip().lower()
                if netloc in self.SITE_HOSTS and parsed.scheme:
                    return f"{parsed.scheme}://{netloc}"
            except Exception:
                pass
        return self._get_configured_site_origin()

    async def before_initial_navigation(self) -> None:
        if not self.page:
            return

        async def handle_user_settings_route(route):
            request = route.request
            try:
                method = str(request.method or "").upper()
            except Exception:
                method = ""

            if method != "POST":
                await route.continue_()
                return

            request_headers: Dict[str, str] = {}
            request_body: bytes | None = None
            request_url = ""
            try:
                request_url = str(request.url or "")
                request_headers = await request.all_headers()
                request_body = self._extract_request_body_bytes(request)
                response = await route.fetch()
                body = await response.body()
            except Exception as e:
                Logger.debug(f"Moonshot: failed to inspect user settings response: {e}")
                await route.continue_()
                return

            try:
                await route.fulfill(response=response, body=body)
            except Exception as e:
                Logger.warning(f"Moonshot: failed to fulfill user settings route: {e}")
                return

            try:
                await self._maybe_disable_memory_from_settings_body(
                    body,
                    request_headers,
                    {
                        "url": request_url,
                        "method": method,
                        "body": request_body,
                    },
                )
            except Exception as e:
                Logger.debug(f"Moonshot: memory guardrail failed while reading settings response: {e}")

        await self.page.route(self.USER_SETTINGS_ROUTE_GLOB, handle_user_settings_route)

    async def after_start(self, status_callback: Optional[Callable[[str], None]] = None) -> None:
        await self.check_ui_language(status_callback=status_callback)
        clear_clean_regeneration_cache(
            self.cache_manager,
            self.clean_regen_message_cache_key,
            self.clean_regen_state_cache_key,
        )
        try:
            await self._remember_send_control_signature(self.page.locator("div.send-button-container"))
        except Exception:
            pass
        try:
            await self._dismiss_common_notice_dialog()
        except Exception as e:
            Logger.debug(f"Moonshot: common notice dialog check failed: {e}")

    async def _dismiss_common_notice_dialog(self) -> bool:
        if not self.page:
            return False

        # Kimi can throw a generic notice modal on startup that blocks all clicks
        # until its plain-styled dismiss button is pressed.
        await asyncio.sleep(0.4)

        dialog = await self._find_first_visible(
            ["div.content.common-notice-dialog"],
            timeout_ms=1200,
            poll_interval_s=0.1,
        )
        if dialog is None:
            return False

        try:
            close_button = dialog.locator("button.kimi-button.plain").first
            if await close_button.count() == 0:
                Logger.warning("Moonshot: common notice dialog was visible, but no plain close button was found.")
                return False
            await close_button.click(timeout=2000)
            Logger.info("Moonshot: dismissed startup common notice dialog.")
            return True
        except Exception as e:
            Logger.warning(f"Moonshot: failed to dismiss startup common notice dialog: {e}")
            return False

    def _ece_requires_auto_login(self) -> bool:
        return True

    @staticmethod
    def _flag_is_true(value: Any) -> bool:
        if value is True:
            return True
        if value is False or value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return False

    @classmethod
    def _memory_is_enabled(cls, memory: Any) -> bool:
        if not isinstance(memory, dict):
            return False
        return cls._flag_is_true(memory.get("useSemanticMemory")) or cls._flag_is_true(
            memory.get("useEpisodicMemory")
        )

    @classmethod
    def _build_settings_request_headers(cls, source_headers: Any) -> Dict[str, str]:
        if not isinstance(source_headers, dict):
            return {}

        forwarded: Dict[str, str] = {}
        for key, value in source_headers.items():
            try:
                name = str(key or "").strip().lower()
                text = str(value or "").strip()
            except Exception:
                continue
            if not name or not text:
                continue
            if name in cls.SETTINGS_REQUEST_HEADER_ALLOWLIST:
                forwarded[name] = text
        return forwarded

    @staticmethod
    def _decode_request_body_text(body: bytes | None) -> str | None:
        if not body:
            return None
        try:
            return body.decode("utf-8")
        except Exception:
            decoded = body.decode("utf-8", errors="ignore")
            return decoded or None

    async def _maybe_disable_memory_from_settings_body(
        self,
        body: bytes,
        source_headers: Any = None,
        refresh_request: Any = None,
    ) -> bool:
        try:
            decoded = body.decode("utf-8", errors="ignore")
            payload = json.loads(decoded)
        except Exception as e:
            Logger.debug(f"Moonshot: could not parse user settings response: {e}")
            return False

        if not isinstance(payload, dict):
            Logger.debug("Moonshot: user settings response was not a JSON object.")
            return False

        user_setting = payload.get("userSetting")
        if not isinstance(user_setting, dict):
            Logger.debug("Moonshot: user settings response did not contain a userSetting object.")
            return False

        memory = user_setting.get("memory")
        if not isinstance(memory, dict):
            Logger.debug("Moonshot: user settings response did not contain a memory section.")
            return False

        if not self._memory_is_enabled(memory):
            Logger.debug("Moonshot: memory already looks disabled.")
            return False

        now = time.time()
        last_attempt = float(getattr(self, "_memory_settings_last_attempt_ts", 0.0) or 0.0)
        if (now - last_attempt) < 15.0:
            Logger.debug("Moonshot: memory auto-disable was already attempted recently. Skipping duplicate request.")
            return False

        async with self._memory_settings_disable_lock:
            now = time.time()
            last_attempt = float(getattr(self, "_memory_settings_last_attempt_ts", 0.0) or 0.0)
            if (now - last_attempt) < 15.0:
                return False
            self._memory_settings_last_attempt_ts = now

            if not self.page:
                return False

            forwarded_headers = self._build_settings_request_headers(source_headers)
            if "authorization" not in forwarded_headers:
                Logger.warning("Moonshot: memory is enabled, but no Authorization header was found on GetUserSetting.")
                return False

            refresh_args = None
            if isinstance(refresh_request, dict):
                refresh_url = str(refresh_request.get("url") or "").strip()
                refresh_method = str(refresh_request.get("method") or "POST").strip().upper() or "POST"
                refresh_body = self._decode_request_body_text(refresh_request.get("body"))
                if refresh_url:
                    refresh_args = {
                        "url": refresh_url,
                        "method": refresh_method,
                        "headers": dict(forwarded_headers),
                        "body": refresh_body,
                    }

            Logger.info("Moonshot: Kimi memory is enabled. Disabling it from the browser context...")

            try:
                result = await self.page.evaluate(
                    """async (args) => {
                        const runRequest = async (request) => {
                            const out = { ok: false, status: 0, text: "" };
                            try {
                                const resp = await fetch(request.url, {
                                    method: request.method || "POST",
                                    credentials: "include",
                                    referrer: request.referrer || (window.location.origin + "/settings"),
                                    headers: {
                                        ...(request.headers || {}),
                                        "content-type": "application/json",
                                    },
                                    body: request.body ?? undefined,
                                });
                                out.ok = !!resp.ok;
                                out.status = resp.status || 0;
                                try {
                                    out.text = await resp.text();
                                } catch (e) {
                                    out.text = "";
                                }
                            } catch (e) {
                                out.error = String(e);
                            }
                            return out;
                        };

                        const out = await runRequest({
                            url: args.url,
                            method: "POST",
                            headers: args.headers,
                            body: JSON.stringify(args.body),
                        });

                        if (out.ok && args.refresh) {
                            await new Promise((resolve) => setTimeout(resolve, 200));
                            out.refresh = await runRequest(args.refresh);
                        }

                        return out;
                    }""",
                    {
                        "url": f"{self._get_site_origin()}{self.USER_SETTINGS_UPDATE_PATH}",
                        "body": self.MEMORY_DISABLE_UPDATE_PAYLOAD,
                        "headers": forwarded_headers,
                        "refresh": refresh_args,
                    },
                )
            except Exception as e:
                Logger.warning(f"Moonshot: failed to disable Kimi memory from browser context: {e}")
                return False

            if isinstance(result, dict) and result.get("ok") is True:
                refresh_result = result.get("refresh")
                if isinstance(refresh_result, dict):
                    if refresh_result.get("ok") is True:
                        Logger.debug("Moonshot: refreshed Kimi settings after disabling memory.")
                    else:
                        try:
                            refresh_status = int(refresh_result.get("status") or 0)
                        except Exception:
                            refresh_status = 0
                        refresh_detail = str(
                            refresh_result.get("error") or refresh_result.get("text") or ""
                        ).strip()
                        if refresh_detail:
                            Logger.warning(
                                f"Moonshot: memory was disabled, but settings refresh failed (status={refresh_status}): "
                                f"{refresh_detail[:200]}"
                            )
                        else:
                            Logger.warning(
                                f"Moonshot: memory was disabled, but settings refresh failed (status={refresh_status})."
                            )
                Logger.success("Moonshot: disabled Kimi memory.")
                return True

            status = 0
            detail = ""
            if isinstance(result, dict):
                try:
                    status = int(result.get("status") or 0)
                except Exception:
                    status = 0

                error = str(result.get("error") or "").strip()
                if error:
                    detail = error
                else:
                    text = str(result.get("text") or "").strip()
                    if text:
                        detail = text[:200]

            if detail:
                Logger.warning(f"Moonshot: failed to disable Kimi memory (status={status}): {detail}")
            else:
                Logger.warning(f"Moonshot: failed to disable Kimi memory (status={status}).")
            return False

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    async def _read_user_name(self) -> str:
        if not self.page:
            return ""

        selectors = [
            "div.user-info span.user-name",
            "div.user-info-container span.user-name",
            "span.user-name",
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                if count == 0:
                    continue
            except Exception:
                continue

            fallback_text = ""
            for idx in range(min(count, 10)):
                item = locator.nth(idx)
                try:
                    text = (await item.inner_text(timeout=1500) or "").strip()
                except Exception:
                    continue
                if not text:
                    continue
                if not fallback_text:
                    fallback_text = text
                try:
                    if await item.is_visible():
                        return text
                except Exception:
                    continue
            if fallback_text:
                return fallback_text

        return ""

    def _classify_user_name(self, user_name: str) -> str:
        normalized = self._normalize_text(user_name)
        if not normalized:
            return "unknown"
        if normalized in self.LOGIN_LABEL_ALIASES:
            return "signed_out"
        return "signed_in"

    async def _login_modal_visible(self) -> bool:
        if not self.page:
            return False

        modal = await self._find_first_visible(
            [
                "div.login-modal-content",
                "div.login-modal-content div.google-login-btn",
                "div.google-login-btn",
            ],
            timeout_ms=0,
        )
        return modal is not None

    async def _read_auth_state(self) -> str:
        if await self._login_modal_visible():
            return "signed_out"

        user_state = self._classify_user_name(await self._read_user_name())
        if user_state != "unknown":
            return user_state

        # Kimi can render the chat shell/editor before the account footer settles.
        # Treat span.user-name as the auth source of truth so "Log In" can't be
        # hidden by a premature editor-visible check.
        return "unknown"

    async def _is_logged_in(self) -> bool:
        return (await self._read_auth_state()) == "signed_in"

    async def _wait_for_auth_state(
        self,
        timeout_ms: int = 0,
        stable_signed_out_ms: int | None = None,
    ) -> str:
        timeout_s = 0.0 if timeout_ms <= 0 else (float(timeout_ms) / 1000.0)
        deadline = time.monotonic() + timeout_s if timeout_s > 0.0 else None
        stable_signed_out_s = max(
            0.0,
            float(
                self.AUTH_STATE_STABLE_SIGNED_OUT_MS
                if stable_signed_out_ms is None
                else stable_signed_out_ms
            )
            / 1000.0,
        )
        signed_out_since: float | None = None
        last_state = "unknown"

        while True:
            state = await self._read_auth_state()
            last_state = state

            if state == "signed_in":
                return state

            if state == "signed_out":
                now = time.monotonic()
                if signed_out_since is None:
                    signed_out_since = now
                if stable_signed_out_s <= 0.0 or (now - signed_out_since) >= stable_signed_out_s:
                    return state
            else:
                signed_out_since = None

            if deadline is not None and time.monotonic() >= deadline:
                return last_state

            await asyncio.sleep(0.25)

    async def _wait_until_logged_in(self, timeout_ms: int = 0) -> bool:
        start = time.time()
        timeout_s = 0.0 if timeout_ms <= 0 else (timeout_ms / 1000.0)

        while True:
            if await self._is_logged_in():
                return True
            if timeout_s > 0.0 and (time.time() - start) >= timeout_s:
                return False
            await asyncio.sleep(0.5)

    async def _human_delay(self, delay_s: float = 0.8) -> None:
        await asyncio.sleep(max(0.0, float(delay_s)))

    async def _click_with_fallbacks(
        self,
        locator,
        *,
        timeout_ms: int = 3000,
        evaluate_fallback: bool = True,
    ) -> bool:
        try:
            await locator.scroll_into_view_if_needed(timeout=int(timeout_ms))
        except Exception:
            pass

        try:
            await locator.click(timeout=int(timeout_ms))
            return True
        except Exception as e:
            Logger.debug(f"Moonshot: normal click failed; trying fallbacks: {e}")

        try:
            await locator.click(timeout=int(timeout_ms), force=True)
            return True
        except Exception as e:
            Logger.debug(f"Moonshot: forced click failed: {e}")

        if not evaluate_fallback:
            return False

        try:
            await locator.evaluate("(el) => el.click()")
            return True
        except Exception as e:
            Logger.debug(f"Moonshot: DOM click fallback failed: {e}")
            return False

    async def _kimi_sidebar_mask_visible(self) -> bool:
        if not self.page:
            return False

        try:
            visible = await self.page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        return (
                            style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && style.pointerEvents !== 'none'
                            && Number(style.opacity || '1') !== 0
                        );
                    };
                    return Array.from(document.querySelectorAll(
                        'div.sidebar-slot.is-mobile-expanded div.mask, ' +
                        'div.sidebar-slot.sidebar-slot--interactive.is-mobile-expanded div.mask'
                    )).some(isVisible);
                }"""
            )
            return bool(visible)
        except Exception:
            return False

    async def _wait_for_kimi_sidebar_mask_hidden(self, timeout_ms: int = 1500) -> bool:
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            if not await self._kimi_sidebar_mask_visible():
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(0.08)

    async def _disable_kimi_sidebar_mask_pointer_events(self) -> None:
        if not self.page:
            return

        try:
            await self.page.evaluate(
                """() => {
                    for (const mask of document.querySelectorAll(
                        'div.sidebar-slot.is-mobile-expanded div.mask, ' +
                        'div.sidebar-slot.sidebar-slot--interactive.is-mobile-expanded div.mask'
                    )) {
                        mask.style.pointerEvents = 'none';
                    }
                }"""
            )
        except Exception:
            pass

    async def _dismiss_kimi_sidebar_overlay(self) -> bool:
        if not self.page:
            return False

        if not await self._kimi_sidebar_mask_visible():
            return True

        masks = self.page.locator(
            "div.sidebar-slot.is-mobile-expanded div.mask, "
            "div.sidebar-slot.sidebar-slot--interactive.is-mobile-expanded div.mask"
        )
        try:
            count = await masks.count()
        except Exception:
            count = 0

        for idx in range(min(count, 5)):
            mask = masks.nth(idx)
            try:
                if not await mask.is_visible():
                    continue
            except Exception:
                continue

            if await self._click_with_fallbacks(mask, timeout_ms=1000):
                if await self._wait_for_kimi_sidebar_mask_hidden(timeout_ms=1200):
                    return True

        try:
            await self.page.keyboard.press("Escape")
            if await self._wait_for_kimi_sidebar_mask_hidden(timeout_ms=800):
                return True
        except Exception:
            pass

        close_button = await self._find_first_visible(
            [
                "aside.sidebar div.sidebar-header div.expand-btn:not(.icon-button)",
                "aside.sidebar div.sidebar-header .expand-btn",
                "div.sidebar-header div.expand-btn:not(.icon-button)",
            ],
            timeout_ms=600,
            poll_interval_s=0.08,
        )
        if close_button is not None:
            if await self._click_with_fallbacks(close_button, timeout_ms=1000):
                if await self._wait_for_kimi_sidebar_mask_hidden(timeout_ms=1200):
                    return True

        Logger.debug("Moonshot: sidebar mask stayed visible; disabling its pointer events.")
        await self._disable_kimi_sidebar_mask_pointer_events()
        return not await self._kimi_sidebar_mask_visible()

    async def _open_kimi_toolkit_menu(self, timeout_ms: int = 4000) -> bool:
        if not self.page:
            return False

        if await self._find_first_visible(["div.toolkit-container"], timeout_ms=0) is not None:
            return True

        await self._dismiss_kimi_sidebar_overlay()
        toolkit_button = await self._find_first_visible(["div.toolkit-trigger-btn"], timeout_ms=timeout_ms)
        if toolkit_button is None:
            Logger.warning("Moonshot: toolkit trigger button not found.")
            return False

        if not await self._click_with_fallbacks(toolkit_button, timeout_ms=3000):
            Logger.warning("Moonshot: toolkit trigger button could not be clicked.")
            return False

        try:
            await self.page.wait_for_selector(
                "div.toolkit-container",
                timeout=max(1000, int(timeout_ms)),
                state="visible",
            )
            return True
        except Exception as e:
            Logger.warning(f"Moonshot: toolkit menu did not open: {e}")
            return False

    async def _find_first_visible_on_page(
        self,
        target_page,
        selectors: List[str],
        timeout_ms: int = 0,
        poll_interval_s: float = 0.15,
    ):
        if not target_page:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            try:
                if target_page.is_closed():
                    return None
            except Exception:
                pass

            for selector in selectors:
                locator = target_page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0
                for idx in range(min(count, 10)):
                    item = locator.nth(idx)
                    try:
                        if await item.is_visible():
                            return item
                    except Exception:
                        continue

            if timeout_ms <= 0 or time.time() >= deadline:
                return None

            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _find_google_auth_page(self):
        candidates = []
        if self.page:
            candidates.append(self.page)

        context = getattr(self, "context", None)
        if context:
            try:
                for candidate in context.pages:
                    if candidate not in candidates:
                        candidates.append(candidate)
            except Exception:
                pass

        for candidate in candidates:
            try:
                if candidate.is_closed():
                    continue
            except Exception:
                pass

            try:
                current_url = str(candidate.url or "")
            except Exception:
                current_url = ""

            if self.AUTH_HOST_MARKER in current_url:
                return candidate

            auth_field = await self._find_first_visible_on_page(
                candidate,
                self.GOOGLE_EMAIL_SELECTORS + self.GOOGLE_PASSWORD_SELECTORS,
                timeout_ms=0,
            )
            if auth_field is not None:
                return candidate

        return None

    async def _wait_for_google_auth_page(self, timeout_ms: int = 15000):
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            auth_page = await self._find_google_auth_page()
            if auth_page is not None:
                return auth_page
            if timeout_ms <= 0 or time.time() >= deadline:
                return None
            await asyncio.sleep(0.25)

    @classmethod
    def _is_legacy_placeholder_pair(cls, email: str, password: str) -> bool:
        normalized_email = str(email or "").strip().lower()
        if normalized_email in cls.LEGACY_PLACEHOLDER_EMAILS:
            return True

        normalized_password = str(password or "").strip()
        if (
            normalized_email.endswith("@notanemail.notanemail")
            and len(normalized_password) >= 20
            and normalized_password.isalnum()
        ):
            return True

        return False

    async def _enter_google_login_value_on_page(self, target_page, selectors: List[str], value: str) -> bool:
        field = await self._find_first_visible_on_page(target_page, selectors, timeout_ms=15000)
        if field is None:
            return False

        try:
            await target_page.bring_to_front()
        except Exception:
            pass

        try:
            await field.click(timeout=3000)
        except Exception:
            pass

        await self._human_delay(0.55)

        try:
            await target_page.keyboard.press("Control+A")
        except Exception:
            pass
        try:
            await target_page.keyboard.press("Backspace")
        except Exception:
            pass

        try:
            await target_page.keyboard.insert_text(str(value or ""))
        except Exception:
            try:
                await field.fill(str(value or ""))
            except Exception:
                return False

        await self._human_delay(0.75)

        try:
            await target_page.keyboard.press("Enter")
        except Exception:
            return False

        return True

    async def _click_locator_with_enter_fallback(self, target_page, locator) -> bool:
        try:
            await target_page.bring_to_front()
        except Exception:
            pass

        try:
            await locator.click(timeout=3000)
            return True
        except Exception:
            pass

        try:
            await locator.focus()
        except Exception:
            pass

        try:
            await locator.press("Enter", timeout=3000)
            return True
        except Exception:
            pass

        try:
            await locator.evaluate(
                "(el) => { if (el && typeof el.click === 'function') { el.click(); return true; } return false; }"
            )
            return True
        except Exception:
            return False

    async def _read_locator_text(self, locator, timeout_ms: int = 1500) -> str:
        try:
            return str(await locator.inner_text(timeout=timeout_ms) or "").strip()
        except Exception:
            pass

        try:
            return str(await locator.text_content(timeout=timeout_ms) or "").strip()
        except Exception:
            pass

        try:
            return str(await locator.get_attribute("aria-label", timeout=timeout_ms) or "").strip()
        except Exception:
            return ""

    async def _google_continue_step_visible(self, target_page) -> bool:
        try:
            body_text = await target_page.locator("body").inner_text()
        except Exception:
            body_text = ""

        normalized = self._normalize_text(body_text)
        return any(
            phrase in normalized
            for phrase in (
                "sign in to",
                "continue to",
                "wants to access",
                "wants additional access",
                "continue as",
            )
        )

    async def _find_google_continue_button(self, target_page):
        direct = await self._find_first_visible_on_page(target_page, self.GOOGLE_CONTINUE_SELECTORS, timeout_ms=0)
        if direct is not None:
            return direct

        candidates = target_page.locator("button, [role='button']")
        try:
            count = await candidates.count()
        except Exception:
            count = 0

        for idx in range(min(count, 25)):
            candidate = candidates.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                continue

            label = self._normalize_text(await self._read_locator_text(candidate))
            if not label:
                continue
            if any(token in label for token in ("continue", "continue as", "allow")):
                if "cancel" in label or "back" in label:
                    continue
                return candidate

        if not await self._google_continue_step_visible(target_page):
            return None

        google_buttons = target_page.locator(self.GOOGLE_BUTTON_FALLBACK_SELECTOR)
        try:
            google_count = await google_buttons.count()
        except Exception:
            google_count = 0

        visible_buttons = []
        for idx in range(min(google_count, 10)):
            candidate = google_buttons.nth(idx)
            try:
                if await candidate.is_visible():
                    visible_buttons.append(candidate)
            except Exception:
                continue

        if len(visible_buttons) >= 2:
            return visible_buttons[1]
        if visible_buttons:
            return visible_buttons[0]
        return None

    async def _click_google_continue_if_present(self, target_page) -> bool:
        button = await self._find_google_continue_button(target_page)
        if button is None:
            return False

        label = self._normalize_text(await self._read_locator_text(button))
        if label and ("cancel" in label or "back" in label):
            return False

        clicked = await self._click_locator_with_enter_fallback(target_page, button)
        if clicked:
            Logger.info("Moonshot: handled Google Continue/consent step.")
            await self._human_delay(0.9)
        return clicked

    async def _perform_google_auto_login(self, email: str, password: str):
        popup = await self._click_google_login_and_get_popup()
        auth_page = popup
        if auth_page is None:
            auth_page = await self._wait_for_google_auth_page(timeout_ms=12000)
        if auth_page is None:
            return False, popup

        try:
            await auth_page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        email_entered = False
        password_entered = False
        continue_clicked = False
        deadline = time.time() + 45.0

        while time.time() < deadline:
            if await self._is_logged_in():
                return True, popup

            try:
                if auth_page.is_closed():
                    break
            except Exception:
                pass

            email_field = await self._find_first_visible_on_page(auth_page, self.GOOGLE_EMAIL_SELECTORS, timeout_ms=0)
            if email_field is not None and (not email_entered):
                email_entered = await self._enter_google_login_value_on_page(auth_page, self.GOOGLE_EMAIL_SELECTORS, email)
                if email_entered:
                    await self._human_delay(1.0)
                    continue

            password_field = await self._find_first_visible_on_page(auth_page, self.GOOGLE_PASSWORD_SELECTORS, timeout_ms=0)
            if password_field is not None and (not password_entered):
                password_entered = await self._enter_google_login_value_on_page(
                    auth_page,
                    self.GOOGLE_PASSWORD_SELECTORS,
                    password,
                )
                if password_entered:
                    await self._human_delay(1.0)
                    continue

            if await self._click_google_continue_if_present(auth_page):
                continue_clicked = True
                continue

            await asyncio.sleep(0.25)

        if await self._is_logged_in():
            return True, popup

        if email_entered or password_entered or continue_clicked:
            ok = await self._wait_until_logged_in(timeout_ms=self.GOOGLE_AUTO_LOGIN_TIMEOUT_MS)
            return ok, popup

        return False, popup

    async def _open_kimi_login_modal(self) -> bool:
        if not self.page:
            return False

        if await self._login_modal_visible():
            return True

        try:
            clicked = await self.page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };

                    const userNames = Array.from(document.querySelectorAll('span.user-name'));
                    const loginName = userNames.find((span) => normalize(span.textContent) === 'log in');
                    const candidates = [];

                    if (loginName) {
                        const userInfo = loginName.closest('div.user-info');
                        if (userInfo) candidates.push(userInfo);
                        const container = loginName.closest('div.user-info-container');
                        if (container) candidates.push(container);
                    }

                    candidates.push(...Array.from(document.querySelectorAll('div.user-info')));
                    candidates.push(...Array.from(document.querySelectorAll('div.user-info-container')));

                    const seen = new Set();
                    const ordered = candidates.filter((el) => {
                        if (!el || seen.has(el)) return false;
                        seen.add(el);
                        return true;
                    });

                    const target = ordered.find(isVisible) || ordered[0] || null;
                    if (!target || typeof target.click !== 'function') {
                        return false;
                    }
                    target.click();
                    return true;
                }"""
            )
            if clicked:
                try:
                    await self.page.wait_for_selector(
                        "div.login-modal-content, div.google-login-btn",
                        timeout=5000,
                    )
                    return True
                except Exception:
                    if await self._login_modal_visible():
                        return True
                    if await self._is_logged_in():
                        return True
        except Exception as e:
            Logger.debug(f"Moonshot: JS user-info login click failed: {e}")

        await self.set_sidebar_status(open=True)
        user_info = await self._find_first_visible(
            [
                "div.user-info",
                "div.user-info-container",
            ],
            timeout_ms=3000,
        )
        if user_info is None:
            return False

        if not await self._click_with_fallbacks(user_info, timeout_ms=2000):
            return False

        try:
            await self.page.wait_for_selector("div.login-modal-content, div.google-login-btn", timeout=5000)
            return True
        except Exception:
            return await self._login_modal_visible() or await self._is_logged_in()

    async def _click_google_login_and_get_popup(self):
        if not self.page:
            return None

        google_button = await self._find_first_visible(
            [
                "div.login-modal-content div.google-login-btn",
                "div.google-login-btn",
            ],
            timeout_ms=8000,
        )
        if google_button is None:
            Logger.warning("Moonshot: Google login button not found.")
            return None

        popup_task = None
        popup = None
        if self.context:
            try:
                popup_task = asyncio.create_task(self.context.wait_for_event("page", timeout=10000))
            except Exception:
                popup_task = None

        try:
            clicked = await self._click_with_fallbacks(google_button, timeout_ms=3000)
            if not clicked:
                raise RuntimeError("Google login button was not clickable")
        except Exception as e:
            Logger.warning(f"Moonshot: failed to click Google login button: {e}")
            if popup_task and (not popup_task.done()):
                popup_task.cancel()
            return None

        if popup_task:
            try:
                popup = await popup_task
            except Exception:
                popup = None

        return popup

    async def login(self):
        if not self.page:
            return

        settled_auth_state = await self._wait_for_auth_state(timeout_ms=self.AUTH_STATE_SETTLE_TIMEOUT_MS)
        if settled_auth_state == "signed_in":
            Logger.info("Moonshot: already signed in.")
            self._mark_active_ece_pair_used()
            return
        if settled_auth_state == "unknown":
            Logger.debug(
                "Moonshot: auth state did not fully settle before login check. "
                "Proceeding with best-effort login detection."
            )

        auto_login = False
        try:
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        except Exception:
            auto_login = False

        opened_login_modal = await self._open_kimi_login_modal()
        if not opened_login_modal:
            Logger.warning("Moonshot: user-info login control not found. Waiting for manual login...")
            await self._wait_until_logged_in(timeout_ms=0)
            return

        post_click_auth_state = await self._wait_for_auth_state(timeout_ms=2000, stable_signed_out_ms=1000)
        if post_click_auth_state == "signed_in":
            Logger.info("Moonshot: session restored before login modal interaction.")
            self._mark_active_ece_pair_used()
            return

        try:
            await self.page.wait_for_selector("div.login-modal-content, div.google-login-btn", timeout=8000)
        except Exception:
            if await self._is_logged_in():
                Logger.info("Moonshot: login already completed.")
                return
            Logger.warning("Moonshot: login modal did not appear in time.")

        pre_google_auth_state = await self._wait_for_auth_state(timeout_ms=1500, stable_signed_out_ms=1000)
        if pre_google_auth_state == "signed_in":
            Logger.info("Moonshot: session restored before Google login flow.")
            self._mark_active_ece_pair_used()
            return

        popup = None
        if auto_login:
            pair = self.ece_active_pair()
            if pair and str(pair.email or "").strip() and str(pair.password or "").strip():
                if self._is_legacy_placeholder_pair(pair.email, pair.password):
                    Logger.warning(
                        "Moonshot: the selected account still looks like an old placeholder identity from Quick Setup. "
                        "Auto Login will be skipped until you replace it with real Google credentials in Credential Manager."
                    )
                    self.notify_user(
                        "Moonshot Login",
                        "Your saved Kimi account still looks like an old placeholder identity. "
                        "Replace it with your real Google email/password in Credential Manager, or finish the login manually this time.",
                        level="warning",
                    )
                else:
                    Logger.info("Moonshot: Auto Login enabled. Attempting Google sign-in...")
                    ok, popup = await self._perform_google_auto_login(pair.email, pair.password)
                    if ok:
                        Logger.success("Moonshot: login detected.")
                        self.ece_mark_used(pair.email)

                        popup_still_open = False
                        if popup is not None:
                            try:
                                await self._human_delay(1.0)
                                popup_still_open = not popup.is_closed()
                            except Exception:
                                popup_still_open = False

                        if popup_still_open:
                            self.notify_user(
                                "Moonshot Login",
                                "Kimi login succeeded, but the Google popup is still open. "
                                "If it does not close on its own, you can close it manually.",
                                level="info",
                            )

                        try:
                            await self.page.bring_to_front()
                        except Exception:
                            pass
                        return

                    Logger.warning(
                        "Moonshot: Auto Login could not complete cleanly. Falling back to manual completion in the browser."
                    )
                    self.notify_user(
                        "Moonshot Login",
                        "Auto Login filled what it could in the Google popup/window, but manual completion is still needed. "
                        "Finish the Google flow in the browser, then return to IntenseRP. "
                        "If the popup does not close on its own after login, you can close it manually.",
                        level="warning",
                    )
            else:
                Logger.warning(
                    "Moonshot: Auto Login is enabled but no Moonshot account is configured in Credential Manager. "
                    "Waiting for manual login..."
                )

        if await self._find_google_auth_page() is None:
            popup = await self._click_google_login_and_get_popup()

        Logger.info("Moonshot: waiting for manual Google login...")

        self.notify_user(
            "Moonshot Login",
            "Complete the Google login flow in the browser tab/window, then return to IntenseRP. "
            "If the popup does not close on its own after login, you can close it manually.",
            level="warning",
        )

        await self._wait_until_logged_in(timeout_ms=0)
        Logger.success("Moonshot: login detected.")
        self._mark_active_ece_pair_used()

    def api_real_model_labels(self) -> list[str]:
        return [self.MODEL_INSTANT, self.MODEL_K28, self.MODEL_K3, self.MODEL_K3_SWARM]

    def _get_moonshot_model_label_for_request(self, model: Any = None) -> str:
        """Resolve a picker label from an API model ID (e.g. kimi-k3-reasoner)."""
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        if override:
            return override
        return ""

    def _resolve_deepthink_flags(self, model: str, ui_model_label: str = "") -> tuple[bool, bool]:
        enable_deepthink = bool(self.config_manager.get_setting("moonshot_behavior", "enable_deepthink"))
        send_deepthink = bool(self.config_manager.get_setting("moonshot_behavior", "send_deepthink"))

        mode = resolve_behavior_mode(
            model,
            self.provider,
            real_model_labels=self.api_real_model_labels(),
        )
        if mode == MODE_CHAT:
            return False, False
        if mode == MODE_REASONER:
            # The Thinking toggle only applies to models with Instant/Thinking
            # variants; explicit K3-family labels simply forward reasoning.
            if ui_model_label and not self._label_has_thinking_variant(ui_model_label):
                return False, send_deepthink
            return True, send_deepthink

        return enable_deepthink, send_deepthink

    @classmethod
    def _label_has_thinking_variant(cls, label: str) -> bool:
        norm = cls._normalize_text(label)
        return (
            norm in {
                cls._normalize_text(cls.MODEL_INSTANT),
                cls._normalize_text(cls.MODEL_THINKING),
            }
            or "instant" in norm
            or "thinking" in norm
        )

    def _resolve_moonshot_request_settings(self, model: str, overrides: Optional[Dict[str, bool]] = None) -> Dict[str, bool]:
        _ = (model or "").strip() or "moonshot-auto"
        ui_model_label = self._get_moonshot_model_label_for_request(model)
        if not ui_model_label:
            ui_model_label = self._get_configured_model_friendly()
        deepthink_enabled, send_deepthink = self._resolve_deepthink_flags(model, ui_model_label)
        search_enabled = bool(self.config_manager.get_setting("moonshot_behavior", "enable_search"))
        send_as_text_file = bool(self.config_manager.get_setting("moonshot_behavior", "send_as_text_file"))

        settings = {
            "deepthink_enabled": bool(deepthink_enabled),
            "send_deepthink": bool(send_deepthink),
            "search_enabled": bool(search_enabled),
            "send_as_text_file": bool(send_as_text_file),
            "ui_model": str(ui_model_label or "").strip(),
        }

        if overrides:
            for key in ("deepthink_enabled", "send_deepthink", "search_enabled", "send_as_text_file"):
                if key in overrides:
                    settings[key] = bool(overrides[key])

        if settings["deepthink_enabled"] and settings["search_enabled"] and (not self._search_and_think_warned):
            self._search_and_think_warned = True
            Logger.warning(
                "Moonshot: Search and Thinking are both enabled. "
                "This can produce multi-stage reasoning streams that some clients (including SillyTavern) may not parse cleanly."
            )

        return settings

    def _get_request_capture_mode(self) -> str:
        try:
            mode = str(
                self.config_manager.get_setting("moonshot_behavior", "request_capture_mode")
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
        path = str(parsed.path or "")
        return any(path.startswith(prefix) for prefix in cls.COMPLETION_URL_PATH_PREFIXES)

    def _extract_moonshot_macros_from_text(self, text: str) -> tuple[str, Dict[str, bool]]:
        return extract_macro_overrides(text, macro_actions=COMMON_REQUEST_MACRO_ACTIONS)

    def _strip_moonshot_macros_from_messages(self, messages: List[Any]) -> tuple[List[Any], Dict[str, bool]]:
        return strip_macros_from_messages(messages, macro_actions=COMMON_REQUEST_MACRO_ACTIONS)

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, bool]]:
        return read_clean_regeneration_state(
            self.cache_manager,
            self.clean_regen_state_cache_key,
            log_label="Clean Regeneration (Moonshot)",
        )

    def _write_clean_regeneration_state(self, state: Dict[str, bool]) -> None:
        write_clean_regeneration_state(
            self.cache_manager,
            self.clean_regen_state_cache_key,
            state,
        )

    def _build_multi_slot_cache_state(
        self,
        *,
        effective_deepthink: bool,
        enable_search: bool,
        send_as_text_file: bool,
    ) -> Dict[str, bool]:
        return {
            "deepthink_enabled": bool(effective_deepthink),
            "search_enabled": bool(enable_search),
            "send_as_text_file": bool(send_as_text_file),
        }

    def _parse_conversation_info_from_url(self, url: str) -> Optional[Dict[str, str]]:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return None

        try:
            parsed = urlsplit(normalized_url)
        except Exception:
            return None

        hostname = str(parsed.netloc or "").strip().lower()
        if hostname not in self.SITE_HOSTS:
            return None

        path_parts = [part for part in str(parsed.path or "").split("/") if part]
        if len(path_parts) < 2 or path_parts[0] != "chat":
            return None

        conversation_id = str(path_parts[1] or "").strip()
        if not conversation_id:
            return None

        scheme = str(parsed.scheme or "https").strip().lower() or "https"
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"{scheme}://{hostname}/chat/{conversation_id}",
        }

    async def _get_current_conversation_info(self) -> Optional[Dict[str, str]]:
        if not self.page:
            return None

        try:
            current_url = str(self.page.url or "")
        except Exception:
            current_url = ""

        info = self._parse_conversation_info_from_url(current_url)
        if info is not None:
            return info

        try:
            live_url = await self.page.evaluate("() => window.location.href")
        except Exception:
            live_url = ""
        return self._parse_conversation_info_from_url(str(live_url or ""))

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

        cookies = await self._get_context_cookie_dict()
        headers = dict(getattr(self, "_last_followup_request_headers", {}) or {})
        site_origin = self._get_site_origin()
        headers.setdefault("accept", "application/json, text/plain, */*")
        headers.setdefault("content-type", "application/json")
        headers.setdefault("origin", site_origin)
        headers.setdefault("referer", f"{site_origin}/")

        try:
            client = await self._get_http_client()
            response = await client.post(
                f"{site_origin}/apiv2/kimi.chat.v1.ChatService/DeleteChat",
                headers=headers,
                cookies=cookies,
                json={"chat_id": normalized_id},
                timeout=20.0,
            )
        except Exception as e:
            Logger.warning(f"Moonshot: failed to auto-delete chat {normalized_id}: {e}")
            return False

        if 200 <= int(response.status_code) < 300:
            return True

        detail = str(response.text or "").strip()
        suffix = f" ({detail[:180]})" if detail else ""
        Logger.warning(
            f"Moonshot: failed to auto-delete chat {normalized_id} "
            f"(status={response.status_code}){suffix}"
        )
        return False

    async def _auto_delete_current_chat(self) -> bool:
        current_info = await self._wait_for_current_conversation_info(timeout_ms=3000)
        if current_info is None:
            Logger.debug("Moonshot: auto-delete skipped because the current chat ID was not available.")
            return False

        conversation_id = str(current_info.get("conversation_id") or "").strip()
        if not conversation_id:
            Logger.debug("Moonshot: auto-delete skipped because the current chat ID was empty.")
            return False

        try:
            await self._click_new_chat()
            await asyncio.sleep(0.4)
        except Exception as e:
            Logger.warning(
                f"Moonshot: auto-delete skipped because a replacement chat could not be prepared: {e}"
            )
            return False

        if await self._delete_conversation_by_id(conversation_id):
            Logger.info("Moonshot: auto-deleted the completed chat.")
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
            Logger.warning(f"Multi-Slot Cache (Moonshot): failed to open cached chat URL: {e}")
            return False

        auth_state = await self._wait_for_auth_state(timeout_ms=self.AUTH_STATE_SETTLE_TIMEOUT_MS)
        if auth_state == "signed_out":
            Logger.warning("Multi-Slot Cache (Moonshot): cached chat URL is not available for the active session.")
            return False

        editor = await self._find_first_visible(
            [
                "div.chat-input-editor[contenteditable='true']",
                "div.chat-input-editor[contenteditable]",
            ],
            timeout_ms=60000,
        )
        if editor is None:
            Logger.warning("Multi-Slot Cache (Moonshot): chat editor did not become ready.")
            return False

        return True

    async def _try_multi_slot_regeneration(
        self,
        *,
        formatted_message: str,
        multi_slot_state: Dict[str, Any],
        completion_armed: asyncio.Event | None = None,
        completion_started: asyncio.Event | None = None,
    ) -> bool:
        account_key = self._get_multi_slot_cache_account_key()
        payload = read_multi_slot_cache_payload(
            self.cache_manager,
            self.multi_slot_cache_key,
            log_label="Multi-Slot Cache (Moonshot)",
        )
        entry = find_multi_slot_cache_entry(payload, account_key, formatted_message, multi_slot_state)
        if entry is None:
            return False

        current_info = await self._get_current_conversation_info()
        if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
            Logger.info("Multi-Slot Cache (Moonshot): opening cached conversation for regeneration...")
            opened = await self._open_cached_conversation(entry["conversation_url"])
            if not opened:
                return False
            current_info = await self._get_current_conversation_info()
            if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
                Logger.warning(
                    "Multi-Slot Cache (Moonshot): cached conversation URL opened, but the expected "
                    "chat ID was not available. Falling back to a new chat."
                )
                return False

        try:
            await self.set_deepthink_state(bool(multi_slot_state.get("deepthink_enabled")))
            await self.set_search_state(bool(multi_slot_state.get("search_enabled")))
            await asyncio.sleep(0.2)
        except Exception:
            pass

        Logger.info("Multi-Slot Cache (Moonshot): cached prompt match found. Attempting to regenerate...")
        if completion_armed is not None:
            completion_armed.set()
        if not await self._click_regenerate():
            if completion_armed is not None:
                completion_armed.clear()
            Logger.warning(
                "Multi-Slot Cache (Moonshot): regenerate button unavailable. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (Moonshot)",
            )
            return False

        if completion_started is not None:
            try:
                await asyncio.wait_for(
                    completion_started.wait(),
                    timeout=self.COMPLETION_REQUEST_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                if completion_armed is not None:
                    completion_armed.clear()
                Logger.warning(
                    "Multi-Slot Cache (Moonshot): completion request not observed after clicking "
                    "Regenerate. Falling back to a new chat."
                )
                return False

        return True

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        return format_request_messages(self.config_manager, messages)

    @staticmethod
    def _coerce_request_body_bytes(value: Any) -> bytes | None:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        return None

    def _extract_request_body_bytes(self, request) -> bytes | None:
        for attr_name in ("post_data_buffer", "post_data"):
            try:
                value = getattr(request, attr_name)
            except Exception:
                continue

            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue

            body = self._coerce_request_body_bytes(value)
            if body is not None:
                return body

        try:
            json_payload = request.post_data_json
        except Exception:
            json_payload = None
        if json_payload is not None:
            try:
                return json.dumps(json_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            except Exception:
                return None

        return None

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "moonshot-auto",
        stream: bool = False,
        temperature: float = None,
        top_p: float = None,
        max_tokens: int | None = None,
        abort_event: asyncio.Event = None,
    ):
        _ = (stream, temperature, top_p, max_tokens)
        response_queue = asyncio.Queue()
        completion_armed = asyncio.Event()
        completion_started = asyncio.Event()
        completion_claim_lock = asyncio.Lock()
        completion_claimed = False
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

        await self.require_english_ui()

        self._connect_buffer = bytearray()
        self.thinking_active = False
        self.abort_requested = False
        self.current_abort_event = abort_event
        self._degrade_notice_logged = False
        provider_activity_count = 0
        stream_error_reported = False

        def report_stream_error(message_text: str) -> None:
            nonlocal stream_error_reported
            if stream_error_reported:
                return
            stream_error_reported = True
            Logger.warning(message_text)
            response_queue.put_nowait(f"data: {json.dumps({'error': message_text})}\n\n")

        def get_provider_activity_count() -> int:
            return provider_activity_count

        async def abort_intercepted_request() -> None:
            intercepted_request_abort.set()
            response = intercepted_response
            if response is not None:
                try:
                    await response.aclose()
                except Exception as e:
                    Logger.debug(f"Moonshot: failed to close intercepted response: {e}")
            try:
                await self._click_stop_button()
            except Exception as e:
                Logger.debug(f"Moonshot: failed to click Stop during timeout handling: {e}")

        resolved_model = (model or "").strip() or "moonshot-auto"
        self.current_model = resolved_model

        macros_overrides: Dict[str, bool] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = self._strip_moonshot_macros_from_messages(message)
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = self._extract_moonshot_macros_from_text(message)

        if macros_overrides:
            Logger.debug(f"Moonshot macros applied: {macros_overrides}")

        effective_settings = self._resolve_moonshot_request_settings(resolved_model, overrides=macros_overrides)
        effective_deepthink = effective_settings["deepthink_enabled"]
        effective_send_deepthink = effective_settings["send_deepthink"]
        enable_search = effective_settings["search_enabled"]
        send_as_text_file = effective_settings["send_as_text_file"]
        self.current_ui_model = str(effective_settings.get("ui_model") or "").strip()
        self.current_send_deepthink = effective_send_deepthink

        async def handle_route(route):
            nonlocal completion_claimed, provider_activity_count, intercepted_response
            request = route.request
            try:
                method = str(request.method or "").upper()
            except Exception:
                method = ""
            try:
                request_url = str(request.url or "")
            except Exception:
                request_url = ""

            if (
                method != "POST"
                or not self._is_completion_request_url(request_url)
                or not completion_armed.is_set()
            ):
                await route.continue_()
                return

            async with completion_claim_lock:
                if completion_claimed:
                    await route.continue_()
                    return
                completion_claimed = True
                completion_started.set()

            Logger.info("Intercepting Moonshot API request...")
            Logger.debug(f"Intercepted request to: {request_url}")

            headers = await request.all_headers()
            forwarded_followup_headers = self._build_settings_request_headers(headers)
            if forwarded_followup_headers:
                self._last_followup_request_headers = dict(forwarded_followup_headers)
            headers.pop("content-length", None)
            headers.pop("host", None)

            cookies = await self.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            request_body = self._extract_request_body_bytes(request)

            full_response_body = bytearray()
            response_headers: Dict[str, str] = {}
            response_status = 200
            aborted = False

            try:
                client = await self._get_http_client()
                request_kwargs: Dict[str, Any] = {
                    "headers": headers,
                    "cookies": cookie_dict,
                    "timeout": 90.0,
                }
                if request_body is not None:
                    request_kwargs["content"] = request_body

                async with client.stream(method, request_url, **request_kwargs) as response:
                    intercepted_response = response
                    response_status = int(response.status_code)
                    for k, v in response.headers.items():
                        response_headers[k] = v

                    async for chunk in response.aiter_bytes():
                        if chunk:
                            provider_activity_count += 1
                        if (
                            intercepted_request_abort.is_set()
                            or self.abort_requested
                            or (abort_event and abort_event.is_set())
                        ):
                            Logger.debug("Abort detected during Moonshot streaming, stopping...")
                            aborted = True
                            break

                        full_response_body.extend(chunk)
                        if not stream_error_reported:
                            self._scan_chunk_for_rate_limit(chunk, report_stream_error)
                        await self._process_connect_chunk(
                            chunk,
                            response_queue,
                            anti_censorship=bool(
                                self.config_manager.get_setting("moonshot_behavior", "anti_censorship")
                            ),
                            send_deepthink=bool(effective_send_deepthink),
                            error_sink=report_stream_error,
                        )
                        if stream_error_reported:
                            Logger.debug("Moonshot: stopping replay stream after rate-limit report.")
                            break
            except httpx.ReadError as e:
                if (
                    not aborted
                    and (not intercepted_request_abort.is_set())
                    and not self.abort_requested
                ):
                    Logger.error(f"Read error during Moonshot intercepted request: {e}")
                    await response_queue.put({"error": str(e)})
            except Exception as e:
                if (
                    not aborted
                    and (not intercepted_request_abort.is_set())
                    and not self.abort_requested
                ):
                    Logger.error(f"Error during Moonshot intercepted request: {e}")
                    await response_queue.put({"error": str(e)})
            finally:
                intercepted_response = None

            if aborted or intercepted_request_abort.is_set() or self.abort_requested:
                Logger.warning("Moonshot generation aborted by user.")
                await self._click_stop_button()

            try:
                await route.fulfill(body=bytes(full_response_body), status=response_status, headers=response_headers)
            except Exception as e:
                Logger.error(f"Moonshot: error fulfilling route: {e}")

            await response_queue.put(None)
            intercepted_request_finished.set()
            if (
                not aborted
                and (not intercepted_request_abort.is_set())
                and not self.abort_requested
            ):
                Logger.success("Moonshot response streaming completed.")

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"Moonshot: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"Moonshot: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        def request_aborted() -> bool:
            return bool(
                intercepted_request_abort.is_set()
                or self.abort_requested
                or (abort_event and abort_event.is_set())
            )

        async def finish_cdp_stream(
            request_id: str,
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            nonlocal cdp_stream_finished
            if request_id != cdp_active_request_id or cdp_stream_finished:
                return

            cdp_stream_finished = True
            await response_queue.put(None)
            intercepted_request_finished.set()
            request_methods.pop(request_id, None)
            cdp_pending_response_urls.pop(request_id, None)

            if not aborted and not encountered_error and not request_aborted():
                Logger.success("Moonshot CDP stream completed.")

        async def feed_cdp_stream_chunk(request_id: str, data: bytes) -> None:
            nonlocal provider_activity_count, cdp_had_data
            if request_id != cdp_active_request_id or not data or cdp_stream_finished:
                return
            if stream_error_reported:
                await finish_cdp_stream(request_id, encountered_error=True)
                return

            cdp_had_data = True
            provider_activity_count += 1
            if request_aborted():
                await finish_cdp_stream(request_id, aborted=True)
                return

            self._scan_chunk_for_rate_limit(data, report_stream_error)
            await self._process_connect_chunk(
                data,
                response_queue,
                anti_censorship=bool(
                    self.config_manager.get_setting("moonshot_behavior", "anti_censorship")
                ),
                send_deepthink=bool(effective_send_deepthink),
                error_sink=report_stream_error,
            )
            if stream_error_reported:
                await finish_cdp_stream(request_id, encountered_error=True)
                return
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
            Logger.info("Teeing Moonshot API response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message_text = f"Moonshot CDP response streaming failed: {exc}"
                Logger.error(message_text)
                await response_queue.put({"error": message_text})
                await finish_cdp_stream(request_id, encountered_error=True)
                return

            if isinstance(result, dict):
                await feed_base64_cdp_stream_chunk(request_id, result.get("bufferedData"))

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

            forwarded_followup_headers = self._build_settings_request_headers(
                request.get("headers")
            )
            if forwarded_followup_headers:
                self._last_followup_request_headers = dict(forwarded_followup_headers)
            Logger.info("Observing Moonshot API request via CDP...")
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
            if request_id == cdp_active_request_id:
                try:
                    status = int(response.get("status") or 0)
                except Exception:
                    status = 0
                if status in (429, 503):
                    report_stream_error(self._build_capacity_error_message())
                    await finish_cdp_stream(request_id, encountered_error=True)
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
                    "Moonshot CDP stream ended with net::ERR_ABORTED after data arrived; "
                    "treating it as complete."
                )
                await finish_cdp_stream(request_id)
                return

            message_text = f"Moonshot CDP stream failed: {error_text}"
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

        try:
            if use_cdp_teeing:
                if not self.context or not self.page:
                    message_text = "Moonshot CDP setup failed: browser context is not available."
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
                    Logger.info("Moonshot Request Capture Mode: CDP Teeing.")
                except Exception as exc:
                    message_text = f"Moonshot CDP setup failed: {exc}"
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
            else:
                await self.page.route(self.CHAT_ROUTE_GLOB, handle_route)
                await self.page.route(self.REGEN_ROUTE_GLOB, handle_route)
                route_handlers_registered = True
                Logger.info("Moonshot Request Capture Mode: Replay.")

            formatted_message = self._format_messages(message_for_formatting)
            moonshot_extra_prompt_texts: Dict[str, str] = {}
            if send_as_text_file:
                try:
                    moonshot_text_file_filler = str(
                        self.config_manager.get_setting("moonshot_behavior", "text_file_filler") or "."
                    )
                except Exception:
                    moonshot_text_file_filler = "."
                if moonshot_text_file_filler.strip():
                    moonshot_extra_prompt_texts["text_file_filler"] = moonshot_text_file_filler
            self._capture_diagnostics_prompt_snapshot(
                formatted_message,
                extra_prompt_texts=moonshot_extra_prompt_texts or None,
                metadata={
                    "model": resolved_model,
                    "deepthink_enabled": bool(effective_deepthink),
                    "send_deepthink": bool(effective_send_deepthink),
                    "search_enabled": bool(enable_search),
                    "send_as_text_file": bool(send_as_text_file),
                },
            )
            clean_regeneration = bool(self.config_manager.get_setting("moonshot_behavior", "clean_regeneration"))
            multi_slot_cache_enabled = bool(
                clean_regeneration
                and self.config_manager.get_setting("moonshot_behavior", "multi_slot_cache")
            )
            try:
                auto_delete_requested = bool(
                    self.config_manager.get_setting("moonshot_behavior", "auto_delete_chats")
                )
            except Exception:
                auto_delete_requested = False
            auto_delete_enabled = bool(auto_delete_requested and (not clean_regeneration))
            if auto_delete_requested and clean_regeneration:
                Logger.warning(
                    "Moonshot: Delete Chat After Reply is skipped for this request because "
                    "Reuse Matching Chat is enabled."
                )
            regenerated = False
            current_cache_matched = False
            should_record_multi_slot = False
            clean_regen_state = None
            multi_slot_state = None

            if clean_regeneration:
                clean_regen_state = {
                    "deepthink_enabled": bool(effective_deepthink),
                    "search_enabled": bool(enable_search),
                    "send_as_text_file": bool(send_as_text_file),
                }
                multi_slot_state = self._build_multi_slot_cache_state(
                    effective_deepthink=bool(effective_deepthink),
                    enable_search=bool(enable_search),
                    send_as_text_file=bool(send_as_text_file),
                )

                last_message = self.cache_manager.read_cache(self.clean_regen_message_cache_key)
                last_state = self._read_clean_regeneration_state()

                message_matches = last_message == formatted_message
                state_matches = last_state == clean_regen_state

                if message_matches and state_matches:
                    current_cache_matched = True
                    Logger.info("Clean Regeneration (Moonshot): Message and settings match cache. Attempting to regenerate...")
                    completion_armed.set()
                    if await self._click_regenerate():
                        Logger.info("Clean Regeneration (Moonshot): Button clicked. Regenerating...")
                        try:
                            await asyncio.wait_for(
                                completion_started.wait(),
                                timeout=self.COMPLETION_REQUEST_TIMEOUT_S,
                            )
                        except asyncio.TimeoutError:
                            completion_armed.clear()
                            Logger.warning(
                                "Clean Regeneration (Moonshot): completion request not observed after "
                                "clicking Regenerate. Falling back to new chat."
                            )
                        else:
                            regenerated = True
                            self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                            self._write_clean_regeneration_state(clean_regen_state)
                    else:
                        completion_armed.clear()
                        Logger.warning("Clean Regeneration (Moonshot): Button not found. Falling back to new chat.")

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
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(clean_regen_state)

            if not regenerated:
                Logger.info("Moonshot: preparing new chat session...")
                await self._click_new_chat()
                await asyncio.sleep(0.4)

                await self.set_deepthink_state(effective_deepthink)
                await self.set_search_state(enable_search)
                await asyncio.sleep(0.2)

                if send_as_text_file:
                    Logger.info("Moonshot: sending message as text file...")
                    file_payload = build_prompt_text_file_payload(formatted_message)
                    uploaded = await self._upload_file(file_payload)
                    if uploaded:
                        filler = self.config_manager.get_setting("moonshot_behavior", "text_file_filler") or "."
                        await self._enter_message(str(filler))
                    else:
                        Logger.warning(
                            "Moonshot: text-file upload failed; falling back to normal text entry."
                        )
                        await self._enter_message(formatted_message)
                    upload_timeout = int(
                        self.config_manager.get_setting("moonshot_behavior", "file_upload_timeout") or 15
                    )
                    Logger.info("Moonshot: sending request...")
                    completion_armed.set()
                    await self._send_message(timeout=upload_timeout)
                else:
                    await self._enter_message(formatted_message)
                    Logger.info("Moonshot: sending request...")
                    completion_armed.set()
                    await self._send_message()

                if clean_regeneration:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(clean_regen_state)
                    should_record_multi_slot = bool(multi_slot_cache_enabled and multi_slot_state)

            if not completion_started.is_set():
                try:
                    await asyncio.wait_for(
                        completion_started.wait(),
                        timeout=self.COMPLETION_REQUEST_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    Logger.error(
                        "Moonshot: completion request was not observed. "
                        "The UI may have swallowed the click or the endpoint changed."
                    )
                    yield f"data: {json.dumps({'error': 'Moonshot: completion request not observed'})}\n\n"
                    return

            stream_had_error = False
            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S,
                idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                on_timeout=abort_intercepted_request,
                activity_counter=get_provider_activity_count,
            ):
                if isinstance(item, dict) and "error" in item:
                    stream_had_error = True
                    yield f"data: {json.dumps(item)}\n\n"
                    break

                yield item

            if self.abort_requested or (abort_event and abort_event.is_set()):
                await abort_intercepted_request()

            if should_record_multi_slot and (not stream_had_error) and (not self.abort_requested):
                conversation_info = await self._wait_for_current_conversation_info(timeout_ms=6000)
                if conversation_info is None:
                    Logger.debug(
                        "Multi-Slot Cache (Moonshot): could not resolve conversation URL after "
                        "generation; skipping cache save."
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
                        log_label="Multi-Slot Cache (Moonshot)",
                    )

            if (
                auto_delete_enabled
                and (not stream_had_error)
                and (not self.abort_requested)
                and not (abort_event and abort_event.is_set())
            ):
                await self._auto_delete_current_chat()

        finally:
            if completion_started.is_set() and not intercepted_request_finished.is_set():
                try:
                    await asyncio.wait_for(intercepted_request_finished.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    Logger.debug("Moonshot: timed out waiting for intercepted request cleanup.")
            self.current_abort_event = None
            self.abort_requested = False
            self.current_model = None
            self.current_ui_model = ""
            self.current_send_deepthink = None
            self.thinking_active = False
            self._connect_buffer = bytearray()
            if route_handlers_registered:
                try:
                    await self.page.unroute(self.CHAT_ROUTE_GLOB, handle_route)
                except Exception:
                    try:
                        await self.page.unroute(self.CHAT_ROUTE_GLOB)
                    except Exception:
                        pass
                try:
                    await self.page.unroute(self.REGEN_ROUTE_GLOB, handle_route)
                except Exception:
                    try:
                        await self.page.unroute(self.REGEN_ROUTE_GLOB)
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
                    Logger.debug(f"Moonshot: CDP detach failed: {exc}")

    async def abort_generation(self):
        Logger.info("Moonshot: abort generation requested...")
        self.abort_requested = True
        if self.current_abort_event:
            self.current_abort_event.set()
        await self._click_stop_button()

    def _moonshot_signature_looks_like_stop(self, signature: str | None) -> bool:
        if not signature:
            return False
        lowered = signature.lower()
        return any(token in lowered for token in ("stop", "cancel", "abort", "pause", "square"))

    async def _click_stop_button(self):
        try:
            send_button = self.page.locator("div.send-button-container")
            if await send_button.count() == 0:
                return False
            if not await send_button.first.is_visible():
                return False

            current_signature = await self._read_control_signature(send_button)
            cached_send_signature = getattr(self, "_send_control_signature", None)
            if cached_send_signature and current_signature == cached_send_signature:
                Logger.debug("Moonshot: composer control matches send mode. Skipping stop click.")
                return False
            if (not cached_send_signature) and (not self._moonshot_signature_looks_like_stop(current_signature)):
                Logger.debug("Moonshot: composer control could not be verified as stop mode.")
                return False

            await send_button.first.click(timeout=2000)
            return True
        except Exception as e:
            Logger.debug(f"Moonshot: stop button click failed: {e}")
            return False

    def _iter_connect_payloads(self, chunk: bytes) -> List[bytes]:
        payloads: List[bytes] = []
        if not chunk:
            return payloads

        self._connect_buffer.extend(chunk)

        while True:
            # Kimi Connect envelope appears to be:
            #   1 byte flags/type + 4 bytes big-endian payload length + payload bytes
            if len(self._connect_buffer) < 5:
                break

            frame_len = int.from_bytes(self._connect_buffer[1:5], byteorder="big", signed=False)
            if frame_len == 0:
                del self._connect_buffer[:5]
                continue

            if frame_len > self.CONNECT_MAX_FRAME_BYTES:
                # Best-effort re-sync when frame boundaries are misaligned
                del self._connect_buffer[:1]
                continue

            total_size = 5 + frame_len
            if len(self._connect_buffer) < total_size:
                break

            payload = bytes(self._connect_buffer[5:total_size])
            del self._connect_buffer[:total_size]
            payloads.append(payload)

        return payloads

    @staticmethod
    def _decode_connect_payload(payload: bytes) -> Optional[Dict[str, Any]]:
        if not payload:
            return None

        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            return None

        if (not text.startswith("{")) or (not text.endswith("}")):
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                return None
            text = text[start : end + 1]

        try:
            data = json.loads(text)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _read_block_content(block: Dict[str, Any], key: str) -> str:
        sub = block.get(key)
        if not isinstance(sub, dict):
            return ""
        content = sub.get("content")
        if isinstance(content, str):
            return content
        return ""

    async def _process_connect_chunk(
        self,
        chunk: bytes,
        queue: asyncio.Queue,
        *,
        anti_censorship: bool,
        send_deepthink: bool,
        error_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        for payload in self._iter_connect_payloads(chunk):
            data = self._decode_connect_payload(payload)
            if not data:
                continue

            content_parts: List[str] = []
            finish_reason: Optional[str] = None

            notification = data.get("notification")
            if isinstance(notification, dict):
                note_type = str(notification.get("type") or "")
                note_msg = str(notification.get("message") or "")

                rate_limit_message = (
                    self._extract_rate_limit_error_from_text(note_msg)
                    or self._extract_rate_limit_error_from_text(note_type)
                )
                if rate_limit_message and error_sink is not None:
                    error_sink(rate_limit_message)

                if note_type == "TYPE_MODEL_DEGRADE" and note_msg and (not self._degrade_notice_logged):
                    self._degrade_notice_logged = True
                    Logger.warning(f"Moonshot model degrade notice: {note_msg}")

                if anti_censorship:
                    lowered_type = note_type.strip().lower()
                    lowered_msg = note_msg.strip().lower()
                    is_filter_like = (
                        ("content" in lowered_type and "filter" in lowered_type)
                        or ("beyond my current scope" in lowered_msg)
                    )
                    if is_filter_like:
                        if self.thinking_active:
                            if send_deepthink:
                                content_parts.append("</think>")
                            self.thinking_active = False
                        finish_reason = "stop"

            op = str(data.get("op") or "")
            mask = str(data.get("mask") or "")
            block = data.get("block")
            message = data.get("message")

            if not isinstance(block, dict):
                block = {}
            if not isinstance(message, dict):
                message = {}

            if mask in {"block.think", "block.think.content"}:
                think_text = self._read_block_content(block, "think")
                if not self.thinking_active:
                    if send_deepthink:
                        content_parts.append("<think>")
                    self.thinking_active = True
                if think_text and send_deepthink:
                    content_parts.append(think_text)

            elif mask in {"block.text", "block.text.content"}:
                text_piece = self._read_block_content(block, "text")
                if self.thinking_active:
                    if send_deepthink:
                        content_parts.append("</think>")
                    self.thinking_active = False
                if text_piece:
                    content_parts.append(text_piece)

            elif mask == "message.status":
                status = str(message.get("status") or "")
                if status == "MESSAGE_STATUS_COMPLETED":
                    if self.thinking_active:
                        if send_deepthink:
                            content_parts.append("</think>")
                        self.thinking_active = False
                    finish_reason = "stop"

            if "done" in data:
                if self.thinking_active:
                    if send_deepthink:
                        content_parts.append("</think>")
                    self.thinking_active = False
                finish_reason = "stop"

            if op == "append" and (mask in {"block.think.content", "block.text.content"}):
                pass

            content = "".join(content_parts)
            if content or finish_reason:
                model_name = self.current_model or "moonshot-auto"
                await queue.put(
                    make_openai_delta_sse(
                        model_name,
                        content,
                        finish_reason=finish_reason,
                    )
                )

    async def _read_current_model_name(self) -> str:
        selectors = [
            "div.current-model div.model-name > span:first-child",
            "div.current-model div.model-name span:first-child",
            "div.current-model div.model-name span.name",
            "div.current-model span.name",
            "div.current-model .name",
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                if await locator.count() == 0:
                    continue
                text = (await locator.first.inner_text(timeout=1500) or "").strip()
                if text:
                    return text
            except Exception:
                continue

        return ""

    async def _read_kimi_model_item_name(self, item) -> str:
        selectors = [
            "div.model-item-content div.header div.model-name > span:first-child",
            "div.model-item-content div.header div.model-name span:first-child",
            "div.model-name > span:first-child",
            "div.model-name span:first-child",
            "span.name",
        ]

        for selector in selectors:
            try:
                locator = item.locator(selector)
                if await locator.count() == 0:
                    continue
                text = (await locator.first.inner_text(timeout=1500) or "").strip()
                if text:
                    return text
            except Exception:
                continue

        try:
            raw = (await item.inner_text(timeout=1500) or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return ""

        for line in raw.splitlines():
            text = line.strip()
            if text:
                return text
        return raw

    async def _select_kimi_model(self, target_model: str) -> bool:
        await self._dismiss_kimi_sidebar_overlay()
        trigger = await self._find_first_visible(["div.current-model"], timeout_ms=8000)
        if trigger is None:
            Logger.warning("Moonshot: model selector trigger not found.")
            return False

        try:
            if await self._find_first_visible(["div.models-container"], timeout_ms=0) is None:
                clicked = await self._click_with_fallbacks(trigger, timeout_ms=3000)
                if not clicked:
                    Logger.warning("Moonshot: model selector trigger could not be clicked.")
                    return False
            await self.page.wait_for_selector("div.models-container", timeout=5000, state="visible")
            await self.page.wait_for_selector("div.models-container div.model-item", timeout=5000, state="attached")
        except Exception as e:
            Logger.warning(f"Moonshot: model picker did not open: {e}")
            return False

        target_norm = self._normalize_text(target_model)
        items = self.page.locator("div.models-container div.model-item")
        count = await items.count()
        if count == 0:
            Logger.warning("Moonshot: no model items found in picker.")
            return False

        seen_names: list[str] = []
        for idx in range(min(count, 30)):
            item = items.nth(idx)
            name_text = await self._read_kimi_model_item_name(item)
            name_norm = self._normalize_text(name_text)
            if name_norm and name_norm not in seen_names:
                seen_names.append(name_norm)
            if not self._model_name_matches(name_norm, target_norm):
                continue

            try:
                clicked = await self._click_with_fallbacks(item, timeout_ms=3000)
                if not clicked:
                    raise RuntimeError("model item was not clickable")
            except Exception as e:
                Logger.warning(f"Moonshot: failed to click model '{target_model}': {e}")
                return False

            deadline = time.time() + 5.0
            while time.time() < deadline:
                current = await self._read_current_model_name()
                if self._model_name_matches(self._normalize_text(current), target_norm):
                    return True
                await asyncio.sleep(0.1)

            current_after = await self._read_current_model_name()
            Logger.warning(
                "Moonshot: model selection click finished but "
                f"did not confirm '{target_model}' (current: '{current_after or '<unknown>'}')."
            )
            return False

        Logger.warning(f"Moonshot: target model '{target_model}' not found in picker.")
        if seen_names:
            Logger.warning(
                "Moonshot: picker entries seen: " + ", ".join(repr(n) for n in seen_names)
            )
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    @classmethod
    def _model_name_matches(cls, item_norm: str, target_norm: str) -> bool:
        """Match a picker item name against a target model with alias support."""
        aliases = cls.MODEL_MATCH_ALIASES.get(target_norm)
        if aliases is None:
            return item_norm == target_norm
        return any(
            item_norm == alias or item_norm.endswith(f" {alias}")
            for alias in aliases
        )

    def _get_configured_model_friendly(self) -> str:
        """Return the configured picker model, or '' for Auto mode.

        Sanitizes legacy/broken values: option dicts saved by older builds
        (e.g. "{'label': 'Kimi K3', ...}") and anything unrecognizable are
        treated as Auto instead of being fed to the picker.
        """
        try:
            raw = self.config_manager.get_setting("moonshot_behavior", "model")
        except Exception:
            raw = None

        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("label") or ""

        value = str(raw or "").strip()
        if not value or value.startswith("{") or value.lower().startswith("auto"):
            return ""
        return value

    async def set_deepthink_state(self, state: bool):
        # Priority: API-requested model (kimi-k3-*) > configured dropdown >
        # legacy Auto mode (Thinking toggle flips Instant/Thinking).
        configured_model = self.current_ui_model or self._get_configured_model_friendly()
        if configured_model:
            # Explicit model selection wins over the Thinking toggle.
            current = await self._read_current_model_name()
            Logger.debug(
                f"Moonshot: model selection requested '{configured_model}' "
                f"(picker currently shows '{current or '<nothing>'}')."
            )
            if self._normalize_text(current) != self._normalize_text(configured_model):
                switched = await self._select_kimi_model(configured_model)
                if not switched:
                    Logger.warning(
                        f"Moonshot: failed to select configured model '{configured_model}'."
                    )
            return

        current = await self._read_current_model_name()
        current_norm = self._normalize_text(current)
        instant_norm = self._normalize_text(self.MODEL_INSTANT)
        thinking_norm = self._normalize_text(self.MODEL_THINKING)

        target_model = self.MODEL_THINKING if state else self.MODEL_INSTANT
        target_norm = self._normalize_text(target_model)

        if current_norm == target_norm:
            return

        if (current_norm not in {instant_norm, thinking_norm}) and (not state):
            target_model = self.MODEL_INSTANT

        switched = await self._select_kimi_model(target_model)
        if not switched:
            Logger.warning(
                f"Moonshot: failed to set Thinking mode target model '{target_model}'."
            )

    async def _find_offline_mode_switch(self):
        """Locate kimi.ai's Offline Mode switch (label "Internet off").

        The new composer replaced the old Search toolkit menu with a single
        ToolSwitch: active means internet is off (search disabled). Returns
        (locator, is_offline_active) or (None, None) when the control is not
        rendered for the current account/model/region.
        """
        if not self.page:
            return None, None

        # Hard deadline - this scan runs on every request and must never stall.
        deadline = time.time() + 5.0
        selectors = [
            "div.chat-editor .tool-switch",
            ".tool-switch",
        ]
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
            except Exception:
                continue

            for idx in range(min(count, 20)):
                if time.time() > deadline:
                    return None, None
                item = locator.nth(idx)
                try:
                    if not await item.is_visible():
                        continue
                    text = self._normalize_text(
                        await self._read_locator_text(item, timeout_ms=750)
                    )
                except Exception:
                    continue
                if not text:
                    continue
                if "internet off" in text or "offline" in text:
                    return item, True
                if "auto online" in text or "internet on" in text or "online" in text:
                    return item, False
        return None, None

    async def _is_search_enabled(self) -> bool:
        switch, offline_active = await self._find_offline_mode_switch()
        if switch is not None:
            return not offline_active

        # Legacy kimi.com indicator.
        indicator = self.page.locator(
            "div.chat-editor div.tool-switch.open.showClose",
            has_text="Internet off",
        )
        try:
            count = await indicator.count()
            if count == 0:
                return True
            for idx in range(min(count, 3)):
                item = indicator.nth(idx)
                if await item.is_visible():
                    return False
        except Exception:
            return True
        return True

    async def _wait_for_connect_menu_open(self, timeout_ms: int = 1200) -> bool:
        if not self.page:
            return False

        try:
            await self.page.wait_for_selector("div.connect-container", timeout=int(timeout_ms), state="visible")
        except Exception:
            return False

        connect_items = self.page.locator("div.connect-container div.connect-item")
        return await self._wait_for_locator_count(connect_items, minimum_count=2, timeout_ms=max(500, int(timeout_ms)))

    async def _dispatch_connect_trigger_events(self, tool_item) -> None:
        try:
            await tool_item.evaluate(
                """(el) => {
                    if (!el) return;
                    const rect = el.getBoundingClientRect();
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;

                    const mouseEvents = ['mousemove', 'mouseover', 'mouseenter'];
                    for (const type of mouseEvents) {
                        try {
                            el.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                clientX: cx,
                                clientY: cy,
                                view: window
                            }));
                        } catch (_) {}
                    }

                    const pointerEvents = ['pointerover', 'pointerenter', 'pointermove'];
                    for (const type of pointerEvents) {
                        try {
                            el.dispatchEvent(new PointerEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                pointerType: 'mouse',
                                clientX: cx,
                                clientY: cy
                            }));
                        } catch (_) {}
                    }

                    try { el.focus(); } catch (_) {}
                }"""
            )
        except Exception:
            pass

    async def _toolkit_item_hover_target(self, tool_item):
        try:
            content = tool_item.locator(":scope .toolkit-item-content").first
            if await content.count() > 0 and await content.is_visible():
                return content
        except Exception:
            pass

        return tool_item

    async def _open_search_connect_menu(self, tool_item) -> bool:
        if not self.page:
            return False

        if await self._wait_for_connect_menu_open(timeout_ms=400):
            return True

        hover_target = await self._toolkit_item_hover_target(tool_item)

        try:
            await hover_target.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await hover_target.hover(timeout=1500)
        except Exception:
            try:
                await hover_target.hover(timeout=1500, force=True)
            except Exception:
                pass
        if await self._wait_for_connect_menu_open(timeout_ms=1200):
            return True

        try:
            box = await hover_target.bounding_box()
            if box:
                cx = box["x"] + (box["width"] / 2.0)
                cy = box["y"] + (box["height"] / 2.0)
                await self.page.mouse.move(cx, cy)
                await asyncio.sleep(0.08)
                await self.page.mouse.move(cx + 1, cy + 1)
        except Exception:
            pass
        if await self._wait_for_connect_menu_open(timeout_ms=1000):
            return True

        await self._dispatch_connect_trigger_events(hover_target)
        if await self._wait_for_connect_menu_open(timeout_ms=1000):
            return True

        if hover_target is not tool_item:
            await self._dispatch_connect_trigger_events(tool_item)
        if await self._wait_for_connect_menu_open(timeout_ms=1000):
            return True

        return await self._wait_for_connect_menu_open(timeout_ms=1000)

    async def _find_search_toolkit_item(self):
        if not self.page:
            return None

        selectors = [
            "div.toolkit-container > label.toolkit-item",
            "div.toolkit-container label.toolkit-item",
            "div.toolkit-container > .toolkit-item",
            "div.toolkit-container .toolkit-item",
            "div.toolkit-container .toolkit-item-content",
            "div.toolkit-container > *",
        ]

        candidates = []
        for selector in selectors:
            locator = self.page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                count = 0

            for idx in range(min(count, 40)):
                item = locator.nth(idx)
                try:
                    if not await item.is_visible():
                        continue
                except Exception:
                    continue

                candidates.append(item)

        for item in candidates:
            label = self._normalize_text(await self._read_locator_text(item))
            if any(token in label for token in ("search", "internet", "web", "connect")):
                return item

        if candidates:
            return candidates[-1]

        return None

    def _notice_search_ui_unavailable(self, action: str) -> None:
        """Warn once per session when Kimi's search controls can't be found."""
        if getattr(self, "_search_ui_unavailable_warned", False):
            Logger.debug(
                f"Moonshot: search UI unavailable, skipped {action} (already reported)."
            )
            return
        self._search_ui_unavailable_warned = True
        Logger.warning(
            f"Moonshot: could not {action} Search - the control was not found on "
            "this Kimi rollout. Continuing with Kimi's current search state."
        )

    async def _set_search_state_via_toolkit(self, state: bool) -> bool:
        if not await self._open_kimi_toolkit_menu(timeout_ms=8000):
            return False

        parent_tool = await self._find_search_toolkit_item()
        if parent_tool is None:
            Logger.warning("Moonshot: search toolkit entry not found.")
            return False

        opened = await self._open_search_connect_menu(parent_tool)
        if not opened:
            Logger.warning("Moonshot: search submenu did not appear after hover/click/event fallbacks.")
            return False

        connect_items = self.page.locator("div.connect-container div.connect-item")
        has_connect_items = await self._wait_for_locator_count(connect_items, minimum_count=2, timeout_ms=3000)
        if not has_connect_items:
            Logger.warning("Moonshot: search submenu items not found.")
            return False

        target_index = 0 if state else 1
        try:
            clicked = await self._click_with_fallbacks(connect_items.nth(target_index), timeout_ms=3000)
            if not clicked:
                raise RuntimeError("search state option was not clickable")
            await asyncio.sleep(0.35)
            return True
        except Exception as e:
            Logger.warning(f"Moonshot: failed to click search state option: {e}")
            return False

    @classmethod
    def _model_has_search_control(cls, ui_model_label: str) -> bool:
        """K3-family models don't render the search control in Kimi's composer."""
        norm = cls._normalize_text(ui_model_label)
        return not ("k3" in norm or "swarm" in norm)

    async def set_search_state(self, state: bool):
        active_label = self.current_ui_model or self._get_configured_model_friendly()
        if not self._model_has_search_control(active_label):
            Logger.debug(
                f"Moonshot: skipping Search toggle for '{active_label}' "
                "(no search control on this model)."
            )
            return
        await self._dismiss_kimi_sidebar_overlay()
        current = await self._is_search_enabled()
        if current == state:
            return

        # Preferred path on kimi.ai: toggle the Offline Mode switch directly.
        switch, _offline_active = await self._find_offline_mode_switch()
        if switch is not None:
            try:
                clicked = await self._click_with_fallbacks(switch, timeout_ms=3000)
                if not clicked:
                    raise RuntimeError("offline mode switch was not clickable")
                await asyncio.sleep(0.4)
                after = await self._is_search_enabled()
                if after == state:
                    return
                Logger.warning(
                    f"Moonshot: Offline Mode toggle did not settle to search={'on' if state else 'off'}."
                )
            except Exception as e:
                Logger.warning(f"Moonshot: failed to toggle the Offline Mode switch: {e}")

        if state:
            quick_enable = self.page.locator(
                "div.chat-editor div.tool-switch.open.showClose",
                has_text="Internet off",
            )
            if await quick_enable.count() > 0:
                try:
                    await self._click_with_fallbacks(quick_enable.first, timeout_ms=2000)
                    await asyncio.sleep(0.15)
                    if await self._is_search_enabled():
                        return
                except Exception:
                    pass

            ok = await self._set_search_state_via_toolkit(state)
            if ok:
                await asyncio.sleep(0.4)
                if await self._is_search_enabled():
                    return
            self._notice_search_ui_unavailable("enable")
            return

        ok = await self._set_search_state_via_toolkit(state)
        if not ok:
            self._notice_search_ui_unavailable("disable")
            return

        await asyncio.sleep(0.4)
        after = await self._is_search_enabled()
        if after != state:
            Logger.warning(f"Moonshot: Search state mismatch after toggle (wanted={state}, actual={after}).")

    async def _wait_for_locator_count(
        self,
        locator,
        minimum_count: int = 1,
        timeout_ms: int = 8000,
        poll_interval_s: float = 0.1,
    ) -> bool:
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            try:
                count = await locator.count()
            except Exception:
                count = 0
            if count >= int(minimum_count):
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _wait_for_first_attached(
        self,
        selectors: List[str],
        timeout_ms: int = 8000,
        poll_interval_s: float = 0.15,
    ):
        if not self.page:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0
                if count > 0:
                    return locator.first

            if time.time() >= deadline:
                return None
            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _find_first_visible(
        self,
        selectors: List[str],
        timeout_ms: int = 0,
        poll_interval_s: float = 0.15,
    ):
        if not self.page:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0
                for idx in range(min(count, 10)):
                    item = locator.nth(idx)
                    try:
                        if await item.is_visible():
                            return item
                    except Exception:
                        continue

            if timeout_ms <= 0 or time.time() >= deadline:
                return None

            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _find_nth_visible(
        self,
        selectors: List[str],
        visible_index: int,
        timeout_ms: int = 0,
        poll_interval_s: float = 0.15,
    ):
        if not self.page:
            return None

        target = max(0, int(visible_index))
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)

        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0

                visible_seen = 0
                for idx in range(min(count, 40)):
                    item = locator.nth(idx)
                    try:
                        if not await item.is_visible():
                            continue
                    except Exception:
                        continue

                    if visible_seen == target:
                        return item
                    visible_seen += 1

            if timeout_ms <= 0 or time.time() >= deadline:
                return None

            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _find_last_visible(
        self,
        selectors: List[str],
        timeout_ms: int = 0,
        poll_interval_s: float = 0.15,
    ):
        if not self.page:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)

        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0

                for idx in range(min(count, 40) - 1, -1, -1):
                    item = locator.nth(idx)
                    try:
                        if await item.is_visible():
                            return item
                    except Exception:
                        continue

            if timeout_ms <= 0 or time.time() >= deadline:
                return None

            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def set_sidebar_status(self, open: bool):
        if not self.page:
            return

        if not open:
            await self._dismiss_kimi_sidebar_overlay()
            return

        if await self._kimi_sidebar_mask_visible():
            return

        opener = await self._find_first_visible(
            [
                "div.icon-button.expand-btn",
                "div.expand-btn.icon-button",
                "aside.sidebar div.sidebar-header div.expand-btn.icon-button",
            ],
            timeout_ms=1000,
            poll_interval_s=0.08,
        )
        if opener is None:
            Logger.debug("Moonshot: open-sidebar button not found.")
            return

        await self._click_with_fallbacks(opener, timeout_ms=1500)

    async def click_new_chat(self, source: str = "auto"):
        _ = source
        if not self.page:
            return

        try:
            await self.page.goto(self.NEW_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            Logger.warning(f"Moonshot: failed to open New Chat URL: {e}")
            return

        auth_state = await self._wait_for_auth_state(timeout_ms=self.AUTH_STATE_SETTLE_TIMEOUT_MS)
        if auth_state == "signed_out":
            Logger.warning("Moonshot: New Chat URL is not available for the active session.")
            return

        await self.set_sidebar_status(open=False)

        editor = await self._find_first_visible(
            [
                "div.chat-input-editor[contenteditable='true']",
                "div.chat-input-editor[contenteditable]",
            ],
            timeout_ms=60000,
        )
        if editor is None:
            Logger.warning("Moonshot: new chat editor did not become ready.")

    async def enter_message(self, message: str):
        await self._enter_message(message)

    async def send_message(self, timeout: int = None):
        await self._send_message(timeout=timeout)

    async def _enter_message(self, message: str):
        await self._dismiss_kimi_sidebar_overlay()
        editor = await self._find_first_visible(
            [
                "div.chat-input-editor[contenteditable='true']",
                "div.chat-input-editor[contenteditable]",
            ],
            timeout_ms=10000,
        )
        if editor is None:
            Logger.warning("Moonshot: message editor not found.")
            return

        try:
            clicked = await self._click_with_fallbacks(editor, timeout_ms=3000)
            if not clicked:
                await editor.evaluate("(el) => el.focus()")
            else:
                try:
                    await editor.evaluate("(el) => el.focus()")
                except Exception:
                    pass
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            if message:
                pasted = False
                try:
                    await self.page.keyboard.insert_text(message)
                    pasted = True
                except Exception:
                    pasted = False

                if not pasted:
                    try:
                        await editor.fill(message)
                        pasted = True
                    except Exception:
                        pasted = False

                if not pasted:
                    try:
                        await editor.type(message, delay=0)
                        pasted = True
                    except Exception:
                        pasted = False

                if not pasted:
                    await editor.evaluate(
                        """(el, value) => {
                            el.focus();
                            try {
                                document.execCommand('selectAll', false, null);
                                document.execCommand('insertText', false, String(value || ''));
                            } catch (e) {
                                el.textContent = String(value || '');
                            }
                            el.dispatchEvent(new InputEvent('input', {
                                bubbles: true,
                                cancelable: true,
                                inputType: 'insertText',
                                data: String(value || '')
                            }));
                        }""",
                        message,
                    )
        except Exception as e:
            Logger.warning(f"Moonshot: failed to enter message: {e}")

    async def _send_message(self, timeout: int = None):
        await self._dismiss_kimi_sidebar_overlay()
        send_button = await self._find_first_visible(["div.send-button-container"], timeout_ms=10000)
        if send_button is None:
            Logger.warning("Moonshot: send button not found.")
            return

        await self._remember_send_control_signature(send_button)
        if timeout and timeout > 0:
            deadline = time.time() + float(timeout)
            while time.time() < deadline:
                class_attr = await send_button.get_attribute("class") or ""
                if "disabled" not in class_attr.split():
                    break
                await asyncio.sleep(0.2)

        class_attr = await send_button.get_attribute("class") or ""
        if "disabled" in class_attr.split():
            Logger.warning("Moonshot: send button is disabled. Cannot send message.")
            return

        try:
            clicked = await self._click_with_fallbacks(send_button, timeout_ms=3000)
            if not clicked:
                raise RuntimeError("send button was not clickable")
        except Exception as e:
            Logger.warning(f"Moonshot: failed to click send button: {e}")

    async def _click_new_chat(self):
        await self.click_new_chat(source="auto")

    async def _click_regenerate(self) -> bool:
        await self._dismiss_kimi_sidebar_overlay()
        actions = self.page.locator("div.segment-assistant-actions-content")
        has_actions = await self._wait_for_locator_count(actions, minimum_count=1, timeout_ms=8000)
        if not has_actions:
            Logger.warning("Moonshot: regenerate action container not found.")
            return False
        count = await actions.count()

        async def _click_if_enabled(candidate) -> bool:
            try:
                if not await candidate.is_visible():
                    return False
            except Exception:
                return False

            try:
                class_attr = await candidate.get_attribute("class") or ""
            except Exception:
                class_attr = ""
            try:
                aria_disabled = (await candidate.get_attribute("aria-disabled") or "").strip().lower()
            except Exception:
                aria_disabled = ""

            if ("disabled" in class_attr.split()) or (aria_disabled == "true"):
                return False

            try:
                return await self._click_with_fallbacks(candidate, timeout_ms=2000)
            except Exception:
                return False

        target_bar = None
        for bar_idx in range(count - 1, -1, -1):
            bar = actions.nth(bar_idx)
            try:
                if not await bar.is_visible():
                    continue
            except Exception:
                continue
            target_bar = bar
            break

        if target_bar is None:
            Logger.warning("Moonshot: no visible regenerate action bar found.")
            return False

        # This is desperate. Sometimes the actions belt only shows up after a hover, perhaps due to a bug or something
        try:
            await target_bar.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await target_bar.hover()
            await asyncio.sleep(0.05)
        except Exception:
            pass

        # regenerate is the Refresh icon
        refresh_btn = target_bar.locator(":scope > div.icon-button:has(svg[name='Refresh'])")
        refresh_count = await refresh_btn.count()
        if refresh_count == 0:
            Logger.warning("Moonshot: strict regenerate target (Refresh icon) not found.")
            return False

        for idx in range(min(refresh_count, 3)):
            if await _click_if_enabled(refresh_btn.nth(idx)):
                return True

        Logger.warning("Moonshot: strict regenerate target found but unavailable.")
        return False

    async def upload_file(self, file_spec: Any) -> None:
        await self._upload_file(file_spec)

    async def _upload_file_direct_input(self, file_spec: Any) -> bool:
        if not self.page:
            return False

        await self._open_kimi_toolkit_menu(timeout_ms=4000)

        file_input = await self._wait_for_first_attached(
            [
                "input.hidden-input[type='file']",
                "input[type='file'].hidden-input",
                "input[type='file']",
            ],
            timeout_ms=8000,
        )
        if file_input is None:
            Logger.warning("Moonshot: file input not found.")
            return False

        try:
            await file_input.set_input_files(file_spec)
            await asyncio.sleep(0.8)
            return True
        except Exception as e:
            Logger.warning(f"Moonshot: file upload failed: {e}")
            return False

    async def _upload_file(self, file_spec: Any) -> bool:
        await self._dismiss_kimi_sidebar_overlay()
        if await self._upload_file_direct_input(file_spec):
            return True

        Logger.warning("Moonshot: file upload could not be completed through the hidden input.")
        return False
