import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Union

from drivers.base_driver import BaseDriver
from drivers.providers import DriverProvider
from drivers.shared_utils import (
    COMMON_REQUEST_MACRO_ACTIONS,
    build_prompt_text_file_payload,
    extract_macro_overrides,
    find_multi_slot_cache_entry,
    format_request_messages,
    make_openai_delta_sse,
    make_openai_usage_sse,
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


class _ThinkTagStripper:
    """Remove <think>...</think> sections from an incremental text stream."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    def feed(self, text: str) -> str:
        self._buffer += str(text or "")
        out: list[str] = []

        while self._buffer:
            lower = self._buffer.lower()
            if self._in_think:
                close_idx = lower.find(self.CLOSE)
                if close_idx < 0:
                    self._buffer = self._buffer[-(len(self.CLOSE) - 1) :]
                    return "".join(out)
                self._buffer = self._buffer[close_idx + len(self.CLOSE) :]
                self._in_think = False
                continue

            open_idx = lower.find(self.OPEN)
            if open_idx < 0:
                keep = self._suffix_prefix_len(self._buffer, self.OPEN)
                if keep:
                    out.append(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                else:
                    out.append(self._buffer)
                    self._buffer = ""
                return "".join(out)

            out.append(self._buffer[:open_idx])
            self._buffer = self._buffer[open_idx + len(self.OPEN) :]
            self._in_think = True

        return "".join(out)

    def finish(self) -> str:
        if self._in_think:
            self._buffer = ""
            self._in_think = False
            return ""
        tail = self._buffer
        self._buffer = ""
        return tail

    @staticmethod
    def _suffix_prefix_len(text: str, prefix: str) -> int:
        max_len = min(len(text), len(prefix) - 1)
        lower_text = text.lower()
        lower_prefix = prefix.lower()
        for size in range(max_len, 0, -1):
            if lower_prefix.startswith(lower_text[-size:]):
                return size
        return 0


class _MimoEventStreamParser:
    """Parse Xiaomi MiMo's EventStream frames into OpenAI-style stream events."""

    def __init__(self, *, send_thinking: bool, include_usage: bool) -> None:
        self.send_thinking = bool(send_thinking)
        self.include_usage = bool(include_usage)
        self.emitted_text = False
        self.finish_emitted = False
        self.provider_final_seen = False
        self.sensitive_query_seen = False

        self._line_buffer = bytearray()
        self._event_name = ""
        self._data_lines: list[str] = []
        self._stripper = None if self.send_thinking else _ThinkTagStripper()
        self._usage_emitted = False

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not chunk:
            return []

        self._line_buffer.extend(chunk)
        out: list[dict[str, Any]] = []

        while True:
            newline = self._line_buffer.find(b"\n")
            if newline == -1:
                break
            raw_line = bytes(self._line_buffer[:newline])
            del self._line_buffer[: newline + 1]
            line = raw_line.decode("utf-8", errors="ignore").rstrip("\r")
            out.extend(self._process_line(line))

        return out

    def finish(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self._line_buffer:
            line = self._line_buffer.decode("utf-8", errors="ignore").rstrip("\r")
            self._line_buffer.clear()
            out.extend(self._process_line(line))
        out.extend(self._dispatch_current_event())

        if self.sensitive_query_seen:
            return out

        if self._stripper is not None:
            tail = self._stripper.finish()
            if tail:
                self.emitted_text = True
                out.append({"type": "delta", "content": tail})

        if self.emitted_text and not self.finish_emitted:
            self.finish_emitted = True
            out.append({"type": "delta", "content": "", "finish_reason": "stop"})
        return out

    def _process_line(self, line: str) -> list[dict[str, Any]]:
        if line == "":
            return self._dispatch_current_event()

        if line.startswith(":"):
            return []

        field, sep, value = line.partition(":")
        if not sep:
            return []
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            self._event_name = value.strip()
        elif field == "data":
            self._data_lines.append(value)

        return []

    def _dispatch_current_event(self) -> list[dict[str, Any]]:
        event_name = self._event_name.strip()
        data = "\n".join(self._data_lines)
        self._event_name = ""
        self._data_lines = []

        if not event_name and not data:
            return []
        return self._dispatch_event(event_name, data)

    def _dispatch_event(self, event_name: str, data: str) -> list[dict[str, Any]]:
        event_name = str(event_name or "").strip()
        payload = self._parse_json_data(data)
        out: list[dict[str, Any]] = []

        if event_name == "message":
            content = ""
            if isinstance(payload, dict) and str(payload.get("type") or "") == "text":
                content = str(payload.get("content") or "")
            content = content.replace("\x00", "")
            if not content:
                return []

            if self._stripper is not None:
                content = self._stripper.feed(content)
            if content:
                self.emitted_text = True
                out.append({"type": "delta", "content": content})
            return out

        if event_name == "usage":
            usage = self._normalize_usage(payload)
            if self.include_usage and usage and not self._usage_emitted:
                self._usage_emitted = True
                out.append({"type": "usage", "usage": usage})
            return out

        if event_name == "sensitive_query":
            self.sensitive_query_seen = True
            out.append(
                {
                    "type": "error",
                    "message": "MiMo blocked this request with its provider-side sensitive-query filter.",
                }
            )
            return out

        if event_name == "finish":
            self.provider_final_seen = True
            if self.sensitive_query_seen:
                return []
            if self.emitted_text and not self.finish_emitted:
                self.finish_emitted = True
                out.append({"type": "delta", "content": "", "finish_reason": "stop"})
            return out

        return []

    @staticmethod
    def _parse_json_data(data: str) -> Any:
        raw = str(data or "").strip()
        if not raw or raw == "[DONE]":
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            return max(0, int(value))
        except Exception:
            return None

    @classmethod
    def _normalize_usage(cls, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        native = payload.get("nativeUsage")
        native = native if isinstance(native, dict) else {}

        prompt_tokens = cls._to_int(payload.get("promptTokens"))
        completion_tokens = cls._to_int(payload.get("completionTokens"))
        total_tokens = cls._to_int(payload.get("totalTokens"))

        if prompt_tokens is None:
            prompt_tokens = cls._to_int(native.get("prompt_tokens"))
        if completion_tokens is None:
            completion_tokens = cls._to_int(native.get("completion_tokens"))
        if total_tokens is None:
            total_tokens = cls._to_int(native.get("total_tokens"))

        if prompt_tokens is None and completion_tokens is None and total_tokens is None:
            return None

        prompt_tokens = prompt_tokens if prompt_tokens is not None else 0
        completion_tokens = completion_tokens if completion_tokens is not None else 0
        total_tokens = (
            total_tokens
            if total_tokens is not None
            else prompt_tokens + completion_tokens
        )

        usage: dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        prompt_details = native.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            usage["prompt_tokens_details"] = prompt_details

        completion_details = native.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            usage["completion_tokens_details"] = completion_details

        return usage


class MimoDriver(BaseDriver):
    BASE_URL = "https://aistudio.xiaomimimo.com/#/c"
    AUTH_COOKIE_URL = "https://aistudio.xiaomimimo.com"
    AUTH_COOKIE_NAMES = ("usedId", "userId", "userid")
    CHAT_REQUEST_URL_PREFIX = "https://aistudio.xiaomimimo.com/open-apis/bot/chat?"
    CHAT_URL_RE = re.compile(
        r"^https://aistudio\.xiaomimimo\.com/#/chat/([^/?#]+)",
        re.IGNORECASE,
    )
    LOGIN_URL_PREFIX = "https://account.xiaomi.com/fe/service/login/password"

    ANNOUNCEMENT_CLOSE_SELECTOR = "[data-track-id='claw_announcement_close_btn']"
    COOKIE_DECLINE_SELECTOR = "[data-track-id='cookie_decline_all_btn']"
    AGREEMENT_CHECKBOX_SELECTOR = "#agreement-checkbox"
    AGREEMENT_CONFIRM_SELECTOR = "[data-track-id='agreement_confirm_btn']"
    SIGN_IN_BUTTON_SELECTOR = "button:has-text('Sign in')"
    CHAT_TEXTAREA_SELECTOR = "textarea"
    SEND_BUTTON_SELECTOR = "[data-track-id='home_send_btn']"
    FILE_INPUT_SELECTOR = "input[type='file']"
    REGENERATE_BUTTON_SELECTOR = "[data-track-id='msg_regenerate_btn']"

    MODEL_SELECTOR_CLASSES = (
        "flex",
        "cursor-pointer",
        "select-none",
        "items-center",
        "gap-1",
        "rounded-md",
        "px-1",
        "py-0.5",
    )
    MODEL_MENU_CLASSES = (
        "flex",
        "flex-col",
        "gap-0.5",
        "rounded-md",
        "border",
        "border-mimo-line-border-card",
        "bg-mimo-bg-outlined",
        "p-3px",
    )
    MODEL_LABELS: List[str] = [
        "MiMo-V2.6-Pro",
        "MiMo-V2.6-Flash",
        "MiMo-V2.5-Pro",
        "MiMo-V2.5",
    ]
    RETIRED_MODEL_LABELS: set[str] = {
        "MiMo-V2.5-Pro",
        "MiMo-V2.5",
    }
    MIMO_RETIRED_MODEL_FALLBACKS: Dict[str, str] = {
        "MiMo-V2.5-Pro": "MiMo-V2.6-Pro",
        "MiMo-V2.5": "MiMo-V2.6-Pro",
    }

    AUTH_TEXTAREA_SETTLE_S = 6.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0

    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.MIMO)
        self.cache_manager = CacheManager()
        self.clean_regen_message_cache_key = "mimo_last_message.txt"
        self.clean_regen_state_cache_key = "mimo_last_message_state.json"
        self.multi_slot_cache_key = "mimo_multi_slot_cache.json"

        self.current_model: Optional[str] = None
        self.current_send_deepthink: Optional[bool] = None
        self._abort_ui_task: asyncio.Task | None = None
        self._refresh_quirks()

    def get_start_url(self) -> str:
        return self.BASE_URL

    def should_apply_configured_model_before_request(self) -> bool:
        return False

    def _refresh_quirks(self) -> None:
        self._completion_request_timeout_s = self._get_int_setting(
            "completion_request_timeout",
            150,
            minimum=5,
        )
        self._first_chunk_timeout_s = self._get_int_setting(
            "first_chunk_timeout",
            150,
            minimum=5,
        )

    def _get_int_setting(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            value = int(self.config_manager.get_setting("mimo_behavior", key) or default)
        except Exception:
            value = default
        return max(int(minimum), int(value))

    def _get_bool_setting(self, key: str, default: bool = False) -> bool:
        try:
            value = self.config_manager.get_setting("mimo_behavior", key)
        except Exception:
            return default
        if value is None:
            return default
        return bool(value)

    def _get_str_setting(self, key: str, default: str = "") -> str:
        try:
            value = self.config_manager.get_setting("mimo_behavior", key)
        except Exception:
            value = default
        return str(value if value is not None else default)

    def _get_browser_proxy_option(self) -> dict[str, str] | None:
        if not self._get_bool_setting("use_proxy", False):
            return super()._get_browser_proxy_option()

        raw_proxy = self._get_str_setting("proxy_url", "").strip()
        proxy = self._parse_browser_proxy_option(raw_proxy, setting_label="MiMo proxy URL")
        if proxy:
            Logger.info("MiMo browser proxy enabled.")
            return proxy

        Logger.warning("MiMo proxy is enabled, but no usable MiMo proxy URL is configured.")
        return None

    async def _navigate_to_start_url(self, start_url: str) -> None:
        try:
            await super()._navigate_to_start_url(start_url)
        except Exception as exc:
            message = str(exc)
            if "ERR_CONNECTION_REFUSED" in message.upper():
                detail = (
                    "Xiaomi MiMo could not be reached. MiMo is heavily geoblocked; "
                    "try a system-wide VPN, or enable MiMo's proxy setting with a "
                    "supported-region HTTP/SOCKS proxy URL and restart the provider."
                )
                Logger.error(detail)
                self.notify_user("Xiaomi MiMo Geoblock", detail, level="warning")
                raise RuntimeError(detail) from exc
            raise

    async def after_start(self, status_callback=None) -> None:
        await self._dismiss_blocking_popups()
        await self.check_ui_language(status_callback=status_callback)
        try:
            await self._wait_for_chat_ready(timeout_ms=60000)
        except Exception as exc:
            Logger.warning(f"MiMo: chat textarea was not ready after startup: {exc}")

    async def cleanup_background_tasks(self) -> None:
        await self._cancel_task(self._abort_ui_task, label="stopping MiMo abort UI task")
        self._abort_ui_task = None

    def request_abort(self) -> None:
        super().request_abort()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            existing = self._abort_ui_task
            if existing and not existing.done():
                return
            self._abort_ui_task = loop.create_task(self._click_stop_button(timeout_s=8.0))
        except Exception:
            pass

    async def _click_first_visible(self, selector: str, *, timeout_ms: int = 1500) -> bool:
        if not self.page:
            return False

        locator = self.page.locator(selector)
        try:
            count = await locator.count()
        except Exception:
            count = 0

        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
                await candidate.click(timeout=timeout_ms)
                return True
            except Exception:
                try:
                    await candidate.evaluate("el => el.click()")
                    return True
                except Exception:
                    continue
        return False

    async def _dismiss_announcement(self) -> bool:
        clicked = await self._click_first_visible(self.ANNOUNCEMENT_CLOSE_SELECTOR)
        if clicked:
            await asyncio.sleep(0.2)
        return clicked

    async def _dismiss_cookie_consent(self) -> bool:
        if not self._get_bool_setting("auto_decline_cookies", True):
            return False
        clicked = await self._click_first_visible(self.COOKIE_DECLINE_SELECTOR)
        if clicked:
            await asyncio.sleep(0.2)
        return clicked

    async def _accept_policy_if_present(self) -> bool:
        if not self.page:
            return False

        checkbox = self.page.locator(self.AGREEMENT_CHECKBOX_SELECTOR)
        try:
            if await checkbox.count() == 0 or not await checkbox.first.is_visible():
                return False
        except Exception:
            return False

        try:
            await checkbox.first.click(timeout=2000)
        except Exception:
            try:
                await checkbox.first.evaluate("el => el.click()")
            except Exception as exc:
                Logger.debug(f"MiMo: failed to click policy checkbox: {exc}")
                return False

        await asyncio.sleep(0.1)
        clicked_confirm = await self._click_first_visible(self.AGREEMENT_CONFIRM_SELECTOR)
        if clicked_confirm:
            await asyncio.sleep(0.4)
        return clicked_confirm

    async def _dismiss_blocking_popups(self) -> None:
        for _ in range(3):
            changed = False
            changed = await self._dismiss_announcement() or changed
            changed = await self._dismiss_cookie_consent() or changed
            changed = await self._accept_policy_if_present() or changed
            if not changed:
                return

    async def _wait_for_chat_ready(self, timeout_ms: int | None = 60000) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        await self._dismiss_blocking_popups()
        await self.page.wait_for_selector(self.CHAT_TEXTAREA_SELECTOR, timeout=timeout_ms or 0)

    async def _read_auth_cookie_value(self) -> str:
        context = getattr(self, "context", None)
        if not context:
            return ""

        cookies = []
        try:
            cookies = await context.cookies(self.AUTH_COOKIE_URL)
        except TypeError:
            try:
                cookies = await context.cookies([self.AUTH_COOKIE_URL])
            except Exception:
                cookies = []
        except Exception:
            cookies = []

        if not cookies:
            try:
                cookies = await context.cookies()
            except Exception:
                cookies = []

        wanted_names = {name.lower() for name in self.AUTH_COOKIE_NAMES}
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "").strip()
            domain = str(cookie.get("domain") or "").strip().lower()
            if (
                name.lower() in wanted_names
                and value
                and (not domain or "xiaomimimo.com" in domain)
            ):
                return value
        return ""

    async def _has_auth_cookie(self) -> bool:
        return bool(await self._read_auth_cookie_value())

    async def _chat_textarea_visible(self) -> bool:
        if not self.page:
            return False

        textarea = self.page.locator(self.CHAT_TEXTAREA_SELECTOR)
        try:
            count = await textarea.count()
        except Exception:
            return False

        for idx in range(count):
            candidate = textarea.nth(idx)
            try:
                if await candidate.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _visible_sign_in_button(self):
        if not self.page:
            return None

        locator = self.page.locator(self.SIGN_IN_BUTTON_SELECTOR)
        try:
            count = await locator.count()
        except Exception:
            count = 0

        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    async def _wait_for_auth_surface(
        self,
        *,
        timeout_s: float = 60.0,
        textarea_settle_s: float | None = None,
    ) -> str:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        load_timeout_ms = max(1, min(int(max(timeout_s, 1.0) * 1000), 15000))
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=load_timeout_ms)
        except Exception:
            pass

        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        textarea_visible_since: float | None = None
        settle_s = (
            self.AUTH_TEXTAREA_SETTLE_S
            if textarea_settle_s is None
            else max(0.0, float(textarea_settle_s))
        )
        while True:
            await self._dismiss_blocking_popups()

            if await self._has_auth_cookie():
                return "authenticated"

            current_url = str(getattr(self.page, "url", "") or "")
            if current_url.startswith(self.LOGIN_URL_PREFIX):
                return "login"

            if (await self._visible_sign_in_button()) is not None:
                return "sign_in"

            if await self._chat_textarea_visible():
                if textarea_visible_since is None:
                    textarea_visible_since = time.monotonic()
                    Logger.debug(
                        "MiMo: chat textarea is visible, but the auth cookie is absent; "
                        "waiting briefly for the Sign in state."
                    )
                elif time.monotonic() - textarea_visible_since >= settle_s:
                    return "loaded_signed_out"
            else:
                textarea_visible_since = None

            if time.monotonic() >= deadline:
                if await self._has_auth_cookie():
                    return "authenticated"
                if textarea_visible_since is not None:
                    return "loaded_signed_out"
                raise TimeoutError(
                    "MiMo page did not expose the chat textarea or Sign in button before timeout "
                    f"(current URL: {current_url or 'unknown'})."
                )
            await asyncio.sleep(0.25)

    async def _wait_for_sign_in_button(self, *, timeout_s: float = 60.0):
        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        textarea_visible_since: float | None = None
        while True:
            await self._dismiss_blocking_popups()
            if await self._has_auth_cookie():
                return None

            sign_in_button = await self._visible_sign_in_button()
            if sign_in_button is not None:
                return sign_in_button

            current_url = str(getattr(self.page, "url", "") or "") if self.page else ""
            if current_url.startswith(self.LOGIN_URL_PREFIX):
                return None

            if await self._chat_textarea_visible():
                if textarea_visible_since is None:
                    textarea_visible_since = time.monotonic()
                elif time.monotonic() - textarea_visible_since >= self.AUTH_TEXTAREA_SETTLE_S:
                    return None
            else:
                textarea_visible_since = None

            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)

    async def _is_logged_in(self) -> bool:
        return await self._has_auth_cookie()

    async def _wait_until_logged_in(self, timeout_s: float | None = None) -> None:
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if await self._has_auth_cookie():
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("MiMo login cookie was not detected before timeout.")
            await asyncio.sleep(0.5)

    async def login(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        surface = await self._wait_for_auth_surface(timeout_s=60.0)
        if surface == "authenticated":
            Logger.info("MiMo: already signed in.")
            self._mark_active_ece_pair_used()
            return

        auto_login = False
        try:
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        except Exception:
            auto_login = False

        if not auto_login:
            Logger.info("MiMo: Auto Login disabled. Waiting for manual login...")
            self.notify_user(
                "Xiaomi MiMo Login",
                "Please log in to Xiaomi MiMo in the browser window, then come back here.",
                level="info",
            )
            await self._wait_until_logged_in()
            await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            self._mark_active_ece_pair_used()
            return

        pair = self.ece_active_pair()
        if not pair:
            Logger.warning(
                "MiMo: Auto Login is enabled but no accounts are configured. Waiting for manual login..."
            )
            self.notify_user(
                "Xiaomi MiMo Login",
                "Auto Login is enabled, but no Xiaomi MiMo accounts are saved. Please log in manually.",
                level="warning",
            )
            await self._wait_until_logged_in()
            await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            self._mark_active_ece_pair_used()
            return

        email = str(pair.email or "").strip()
        password = str(pair.password or "")
        if not email or not password:
            raise RuntimeError("MiMo account is missing an email or password.")

        on_login_page = surface == "login"
        if on_login_page:
            Logger.info("MiMo: Xiaomi login page is already open.")
        else:
            sign_in_button = await self._wait_for_sign_in_button(timeout_s=60.0)
            if sign_in_button is None:
                if await self._is_logged_in():
                    Logger.info("MiMo: already signed in.")
                    self._mark_active_ece_pair_used()
                    return
                current_url = str(getattr(self.page, "url", "") or "")
                if current_url.startswith(self.LOGIN_URL_PREFIX):
                    on_login_page = True
                    Logger.info("MiMo: Xiaomi login page opened while waiting for Sign in.")
                else:
                    raise RuntimeError(
                        "MiMo auth cookie is absent and the Sign in button was not found after "
                        "the page finished loading."
                    )

            if not on_login_page:
                Logger.info("MiMo: Auto Login enabled. Attempting login...")
                try:
                    await sign_in_button.click(timeout=5000)
                except Exception:
                    await sign_in_button.evaluate("el => el.click()")

                try:
                    await self.page.wait_for_url(
                        re.compile(r"^https://account\.xiaomi\.com/fe/service/login/password\?"),
                        timeout=20000,
                    )
                except Exception as exc:
                    raise RuntimeError("MiMo login did not redirect to Xiaomi's password page.") from exc

        try:
            account_input = self.page.locator("input[name='account']")
            password_input = self.page.locator("input[type='password']")
            await account_input.first.fill(email, timeout=15000)
            await password_input.first.fill(password, timeout=15000)

            checkbox = self.page.locator("input.ant-checkbox-input")
            if await checkbox.count() > 0:
                try:
                    await checkbox.first.check(timeout=3000)
                except Exception:
                    await checkbox.first.click(timeout=3000)

            submit = self.page.locator("button[type='submit']")
            await submit.first.click(timeout=5000)
        except Exception as exc:
            raise RuntimeError(f"MiMo failed to fill Xiaomi login credentials: {exc}") from exc

        self.notify_user(
            "Xiaomi MiMo Login",
            "If Xiaomi asks for extra confirmation, please complete it in the browser window.",
            level="info",
        )

        try:
            await asyncio.wait_for(self._wait_until_logged_in(), timeout=15.0)
        except asyncio.TimeoutError:
            Logger.warning("MiMo: login redirect was not detected after submit. Waiting for manual completion...")
            self.notify_user(
                "Xiaomi MiMo Login",
                "Login needs manual attention. Complete Xiaomi's prompt in the browser window; IntenseRP will keep waiting.",
                level="warning",
            )
            await self._wait_until_logged_in()

        try:
            await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        await self._wait_for_chat_ready(timeout_ms=60000)
        self.ece_mark_used(email)
        Logger.success("MiMo: chat ready.")

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _canonicalize_model_label(cls, value: Any) -> str:
        normalized = cls._normalize_text(value).lower()
        return re.sub(r"[^a-z0-9]+", "", normalized)

    def api_real_model_labels(self) -> list[str]:
        return [
            label for label in self.MODEL_LABELS
            if label not in self.RETIRED_MODEL_LABELS
        ]

    def _get_configured_model_label(self) -> str:
        value = self._get_str_setting("model", "MiMo-V2.6-Pro").strip()
        chosen = value or "MiMo-V2.6-Pro"
        return self.MIMO_RETIRED_MODEL_FALLBACKS.get(chosen, chosen)

    def _get_model_label_for_request(self, model: Any = None) -> str:
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        target = override or self._get_configured_model_label()
        return self.MIMO_RETIRED_MODEL_FALLBACKS.get(target, target)

    async def apply_configured_model(self, model: Any = None) -> None:
        desired = self._get_model_label_for_request(model)
        if not desired:
            return
        await self._ensure_mimo_model_selected(desired)

    async def _read_current_mimo_model_label(self) -> str:
        if not self.page:
            return ""

        try:
            text = await self.page.evaluate(
                """({classes, knownLabels}) => {
                    const hasClasses = (el, names) => names.every((name) => el.classList.contains(name));
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== "hidden"
                            && style.display !== "none";
                    };
                    const candidates = Array.from(document.querySelectorAll("div"))
                        .filter((el) => hasClasses(el, classes) && isVisible(el));
                    for (const el of candidates) {
                        const raw = (el.textContent || "").toString().trim();
                        for (const label of knownLabels) {
                            if (raw.includes(label)) return label;
                        }
                        if (raw) return raw;
                    }
                    return "";
                }""",
                {
                    "classes": list(self.MODEL_SELECTOR_CLASSES),
                    "knownLabels": list(self.MODEL_LABELS),
                },
            )
        except Exception as exc:
            Logger.debug(f"MiMo: failed to read current model label: {exc}")
            return ""
        return str(text or "").strip()

    async def _click_viewport_point(self, point: Any, *, label: str) -> bool:
        if not self.page or not isinstance(point, dict):
            return False

        try:
            x = float(point.get("x"))
            y = float(point.get("y"))
        except Exception:
            return False

        try:
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.03)
            await self.page.mouse.down()
            await asyncio.sleep(0.06)
            await self.page.mouse.up()
            return True
        except Exception as exc:
            Logger.debug(f"MiMo: physical click failed for {label}: {exc}")
            return False

    async def _model_selector_click_targets(self) -> dict[str, Any]:
        if not self.page:
            return {"found": False, "candidates": []}

        try:
            payload = await self.page.evaluate(
                """({selectorClasses}) => {
                    const compact = (value) => (value || "").toString().replace(/\\s+/g, " ").trim();
                    const hasClasses = (el, names) => names.every((name) => el.classList.contains(name));
                    const rectInfo = (el) => {
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) return null;
                        return {
                            x: rect.left,
                            y: rect.top,
                            width: rect.width,
                            height: rect.height,
                            centerX: rect.left + rect.width / 2,
                            centerY: rect.top + rect.height / 2
                        };
                    };
                    const isVisible = (el) => {
                        const rect = rectInfo(el);
                        if (!rect) return false;
                        const style = window.getComputedStyle(el);
                        return style.visibility !== "hidden" && style.display !== "none";
                    };
                    const elementAtPointInfo = (x, y) => {
                        const atPoint = document.elementFromPoint(x, y);
                        return atPoint
                            ? {
                                tag: compact(atPoint.tagName).toLowerCase(),
                                role: compact(atPoint.getAttribute("role")),
                                text: compact(atPoint.textContent).slice(0, 80),
                                className: compact(atPoint.className).slice(0, 140)
                            }
                            : null;
                    };
                    const describe = (el) => {
                        if (!el) return null;
                        const style = window.getComputedStyle(el);
                        const rect = rectInfo(el);
                        return {
                            tag: compact(el.tagName).toLowerCase(),
                            role: compact(el.getAttribute("role")),
                            ariaLabel: compact(el.getAttribute("aria-label")),
                            ariaHasPopup: compact(el.getAttribute("aria-haspopup")),
                            dataTrackId: compact(el.getAttribute("data-track-id")),
                            dataTrackName: compact(el.getAttribute("data-track-name")),
                            text: compact(el.textContent).slice(0, 120),
                            className: compact(el.className).slice(0, 180),
                            cursor: compact(style.cursor),
                            pointerEvents: compact(style.pointerEvents),
                            rect,
                            elementAtPoint: rect ? elementAtPointInfo(rect.centerX, rect.centerY) : null
                        };
                    };
                    const add = (items, seen, el, reason) => {
                        if (!el || seen.has(el) || !isVisible(el)) return;
                        const targetIndex = seen.size;
                        seen.add(el);
                        const info = describe(el);
                        if (!info || !info.rect) return;
                        items.push({
                            reason: `${reason} center`,
                            x: info.rect.centerX,
                            y: info.rect.centerY,
                            targetIndex,
                            elementAtClickPoint: elementAtPointInfo(info.rect.centerX, info.rect.centerY),
                            info
                        });
                        if (info.rect.width >= 48) {
                            items.push({
                                reason: `${reason} right edge`,
                                x: info.rect.x + info.rect.width - 10,
                                y: info.rect.centerY,
                                targetIndex,
                                elementAtClickPoint: elementAtPointInfo(info.rect.x + info.rect.width - 10, info.rect.centerY),
                                info
                            });
                        }
                    };

                    const triggerInner = Array.from(document.querySelectorAll("div"))
                        .find((el) => hasClasses(el, selectorClasses) && isVisible(el));
                    if (!triggerInner) {
                        return {found: false, candidates: [], reason: "selector class div not found"};
                    }

                    const candidates = [];
                    const seen = new Set();
                    try {
                        (triggerInner.parentElement || triggerInner).scrollIntoView({
                            block: "center",
                            inline: "center"
                        });
                    } catch (e) {}

                    add(candidates, seen, triggerInner, "matched selector div");
                    add(candidates, seen, triggerInner.parentElement, "parent element");
                    add(
                        candidates,
                        seen,
                        triggerInner.closest("button,[role='button'],[aria-haspopup='true']"),
                        "closest button/role popup"
                    );

                    let ancestor = triggerInner.parentElement;
                    let depth = 0;
                    while (ancestor && depth < 6) {
                        const style = window.getComputedStyle(ancestor);
                        const role = compact(ancestor.getAttribute("role")).toLowerCase();
                        const clickable =
                            compact(style.cursor).toLowerCase() === "pointer"
                            || role === "button"
                            || compact(ancestor.getAttribute("aria-haspopup"))
                            || ancestor.onclick
                            || ancestor.tabIndex >= 0;
                        if (clickable) {
                            add(candidates, seen, ancestor, `clickable ancestor ${depth + 1}`);
                        }
                        ancestor = ancestor.parentElement;
                        depth += 1;
                    }

                    return {
                        found: true,
                        matched: describe(triggerInner),
                        candidates
                    };
                }""",
                {"selectorClasses": list(self.MODEL_SELECTOR_CLASSES)},
            )
        except Exception as exc:
            Logger.warning(f"MiMo: failed to inspect model selector targets: {exc}")
            return {"found": False, "candidates": [], "error": str(exc)}

        return payload if isinstance(payload, dict) else {"found": False, "candidates": []}

    async def _dispatch_model_selector_click(self, candidate_index: int) -> bool:
        if not self.page:
            return False
        try:
            return bool(
                await self.page.evaluate(
                    """({selectorClasses, candidateIndex}) => {
                        const compact = (value) => (value || "").toString().replace(/\\s+/g, " ").trim();
                        const hasClasses = (el, names) => names.every((name) => el.classList.contains(name));
                        const rectInfo = (el) => {
                            if (!el) return null;
                            const rect = el.getBoundingClientRect();
                            if (!rect || rect.width <= 0 || rect.height <= 0) return null;
                            return {
                                x: rect.left,
                                y: rect.top,
                                width: rect.width,
                                height: rect.height,
                                centerX: rect.left + rect.width / 2,
                                centerY: rect.top + rect.height / 2
                            };
                        };
                        const isVisible = (el) => {
                            const rect = rectInfo(el);
                            if (!rect) return false;
                            const style = window.getComputedStyle(el);
                            return style.visibility !== "hidden" && style.display !== "none";
                        };
                        const add = (items, seen, el) => {
                            if (!el || seen.has(el) || !isVisible(el)) return;
                            seen.add(el);
                            items.push(el);
                        };
                        const triggerInner = Array.from(document.querySelectorAll("div"))
                            .find((el) => hasClasses(el, selectorClasses) && isVisible(el));
                        if (!triggerInner) return false;
                        const items = [];
                        const seen = new Set();
                        add(items, seen, triggerInner);
                        add(items, seen, triggerInner.parentElement);
                        add(items, seen, triggerInner.closest("button,[role='button'],[aria-haspopup='true']"));
                        let ancestor = triggerInner.parentElement;
                        let depth = 0;
                        while (ancestor && depth < 6) {
                            const style = window.getComputedStyle(ancestor);
                            const role = compact(ancestor.getAttribute("role")).toLowerCase();
                            if (
                                compact(style.cursor).toLowerCase() === "pointer"
                                || role === "button"
                                || compact(ancestor.getAttribute("aria-haspopup"))
                                || ancestor.onclick
                                || ancestor.tabIndex >= 0
                            ) {
                                add(items, seen, ancestor);
                            }
                            ancestor = ancestor.parentElement;
                            depth += 1;
                        }
                        const target = items[Math.max(0, Number(candidateIndex) || 0)];
                        const rect = rectInfo(target);
                        if (!target || !rect) return false;
                        const eventInit = {
                            bubbles: true,
                            cancelable: true,
                            composed: true,
                            clientX: rect.centerX,
                            clientY: rect.centerY,
                            button: 0,
                            buttons: 1,
                            pointerId: 1,
                            pointerType: "mouse"
                        };
                        try { target.dispatchEvent(new PointerEvent("pointerover", eventInit)); } catch (e) {}
                        try { target.dispatchEvent(new MouseEvent("mouseover", eventInit)); } catch (e) {}
                        try { target.dispatchEvent(new PointerEvent("pointerenter", eventInit)); } catch (e) {}
                        try { target.dispatchEvent(new MouseEvent("mouseenter", eventInit)); } catch (e) {}
                        try { target.dispatchEvent(new PointerEvent("pointerdown", eventInit)); } catch (e) {}
                        try { target.dispatchEvent(new MouseEvent("mousedown", eventInit)); } catch (e) {}
                        try { target.dispatchEvent(new PointerEvent("pointerup", {...eventInit, buttons: 0})); } catch (e) {}
                        try { target.dispatchEvent(new MouseEvent("mouseup", {...eventInit, buttons: 0})); } catch (e) {}
                        try { target.dispatchEvent(new MouseEvent("click", {...eventInit, buttons: 0})); } catch (e) {}
                        return true;
                    }""",
                    {
                        "selectorClasses": list(self.MODEL_SELECTOR_CLASSES),
                        "candidateIndex": int(candidate_index),
                    },
                )
            )
        except Exception as exc:
            Logger.debug(f"MiMo: synthetic selector click failed: {exc}")
            return False

    async def _wait_for_model_menu_visible(self, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        while time.monotonic() < deadline:
            if await self._model_menu_visible():
                return True
            await asyncio.sleep(0.1)
        return await self._model_menu_visible()

    async def _open_mimo_model_dropdown(self, timeout_ms: int = 6000) -> bool:
        if not self.page:
            return False

        initial_menu_info = await self._model_menu_info()
        if initial_menu_info.get("visible"):
            Logger.debug(
                "MiMo: model menu already visible before selector click: "
                f"{json.dumps(initial_menu_info, ensure_ascii=True)[:2000]}"
            )
            return True

        diagnostics = await self._model_selector_click_targets()
        candidates = diagnostics.get("candidates") if isinstance(diagnostics, dict) else []
        if isinstance(diagnostics, dict):
            summary = {
                "found": bool(diagnostics.get("found")),
                "matched": diagnostics.get("matched"),
                "candidates": candidates,
                "reason": diagnostics.get("reason"),
                "error": diagnostics.get("error"),
            }
            Logger.debug(f"MiMo: model selector diagnostics: {json.dumps(summary, ensure_ascii=True)[:4000]}")

        if not isinstance(candidates, list) or not candidates:
            Logger.warning("MiMo: model selector trigger not found.")
            return False

        per_candidate_timeout = max(0.4, min(1.2, (timeout_ms / 1000.0) / max(1, len(candidates))))
        for idx, candidate in enumerate(candidates):
            pre_click_menu_info = await self._model_menu_info()
            if pre_click_menu_info.get("visible"):
                Logger.debug(
                    "MiMo: model menu became visible before selector click "
                    f"#{idx}: {json.dumps(pre_click_menu_info, ensure_ascii=True)[:2000]}"
                )
                return True
            reason = str(candidate.get("reason") or f"candidate {idx}")
            target_index = int(candidate.get("targetIndex") or 0)
            point = "(unknown)"
            if isinstance(candidate.get("x"), (int, float)) and isinstance(candidate.get("y"), (int, float)):
                point = f"({float(candidate.get('x')):.1f}, {float(candidate.get('y')):.1f})"
            info = candidate.get("info") if isinstance(candidate.get("info"), dict) else {}
            at_point = candidate.get("elementAtClickPoint") or (
                info.get("elementAtPoint") if isinstance(info, dict) else None
            )
            Logger.debug(
                "MiMo: trying model selector click "
                f"#{idx} ({reason}); targetIndex={target_index}; point={point}; "
                f"tag={info.get('tag') if isinstance(info, dict) else ''}; "
                f"cursor={info.get('cursor') if isinstance(info, dict) else ''}; "
                f"pointerEvents={info.get('pointerEvents') if isinstance(info, dict) else ''}; "
                f"atPoint={at_point}"
            )

            if await self._click_viewport_point(candidate, label=f"model selector {reason}"):
                if await self._wait_for_model_menu_visible(timeout_s=per_candidate_timeout):
                    menu_info = await self._model_menu_info()
                    Logger.debug(
                        "MiMo: model menu visible after physical selector click "
                        f"#{idx}: {json.dumps(menu_info, ensure_ascii=True)[:2000]}"
                    )
                    return True
                menu_info = await self._model_menu_info()
                Logger.debug(
                    "MiMo: menu after physical selector click "
                    f"#{idx}: {json.dumps(menu_info, ensure_ascii=True)[:2000]}"
                )

            Logger.debug(f"MiMo: physical click did not open model menu for selector #{idx}; trying synthetic events.")
            if await self._dispatch_model_selector_click(target_index):
                if await self._wait_for_model_menu_visible(timeout_s=per_candidate_timeout):
                    menu_info = await self._model_menu_info()
                    Logger.debug(
                        "MiMo: model menu visible after synthetic selector click "
                        f"#{idx}: {json.dumps(menu_info, ensure_ascii=True)[:2000]}"
                    )
                    return True
                menu_info = await self._model_menu_info()
                Logger.debug(
                    "MiMo: menu after synthetic selector click "
                    f"#{idx}: {json.dumps(menu_info, ensure_ascii=True)[:2000]}"
                )
            else:
                Logger.debug(f"MiMo: synthetic selector click returned false for selector #{idx}.")

        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.1)
        except Exception:
            pass

        for idx, candidate in enumerate(candidates[:2]):
            if await self._click_viewport_point(candidate, label=f"model selector double-click {idx}"):
                await asyncio.sleep(0.08)
                await self._click_viewport_point(candidate, label=f"model selector double-click {idx}")
            if await self._wait_for_model_menu_visible(timeout_s=0.8):
                menu_info = await self._model_menu_info()
                Logger.debug(
                    "MiMo: model menu visible after selector double-click "
                    f"#{idx}: {json.dumps(menu_info, ensure_ascii=True)[:2000]}"
                )
                return True

        Logger.warning("MiMo: model selector popup did not appear.")
        return False

    async def _model_menu_info(self) -> dict[str, Any]:
        if not self.page:
            return {"visible": False, "menuCount": 0, "menus": []}
        try:
            payload = await self.page.evaluate(
                """({menuClasses, knownLabels}) => {
                    const compact = (value) => (value || "").toString().replace(/\\s+/g, " ").trim();
                    const canon = (value) => compact(value).replace(/[^a-z0-9]+/gi, "").toLowerCase();
                    const optionCanon = (value) => {
                        const label = canon(value);
                        return label.endsWith("new") ? label.slice(0, -3) : label;
                    };
                    const known = (knownLabels || []).map(canon).filter(Boolean);
                    const hasClasses = (el, names) => names.every((name) => el.classList.contains(name));
                    const rectInfo = (el) => {
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) return null;
                        return {
                            x: rect.left,
                            y: rect.top,
                            width: rect.width,
                            height: rect.height,
                            centerX: rect.left + rect.width / 2,
                            centerY: rect.top + rect.height / 2
                        };
                    };
                    const isVisible = (el) => {
                        const rect = rectInfo(el);
                        if (!rect) return false;
                        const style = window.getComputedStyle(el);
                        return style.visibility !== "hidden" && style.display !== "none";
                    };
                    const matchesKnownModel = (button) => {
                        const labels = [
                            button.dataTrackNameCanon,
                            button.textCanon,
                            button.combinedCanon
                        ].filter(Boolean);
                        return labels.some((label) => known.includes(label));
                    };

                    const menus = [];
                    const candidates = Array.from(document.querySelectorAll("div"))
                        .filter((el) => hasClasses(el, menuClasses) && isVisible(el));
                    for (const menu of candidates) {
                        const buttons = Array.from(menu.querySelectorAll("button"))
                            .filter(isVisible)
                            .map((button) => {
                                const dataTrackName = compact(button.getAttribute("data-track-name"));
                                const text = compact(button.textContent);
                                return {
                                    dataTrackName: dataTrackName.slice(0, 120),
                                    text: text.slice(0, 120),
                                    dataTrackNameCanon: optionCanon(dataTrackName),
                                    textCanon: optionCanon(text),
                                    combinedCanon: optionCanon(`${dataTrackName} ${text}`),
                                    rect: rectInfo(button)
                                };
                            });
                        const menuText = compact(menu.textContent);
                        menus.push({
                            text: menuText.slice(0, 180),
                            rect: rectInfo(menu),
                            buttonCount: buttons.length,
                            modelButtonCount: buttons.filter(matchesKnownModel).length,
                            buttons: buttons.slice(0, 8)
                        });
                    }

                    return {
                        visible: menus.some((menu) => menu.modelButtonCount > 0),
                        menuCount: menus.length,
                        menus: menus.slice(0, 5)
                    };
                }""",
                {
                    "menuClasses": list(self.MODEL_MENU_CLASSES),
                    "knownLabels": list(self.MODEL_LABELS),
                },
            )
        except Exception as exc:
            return {"visible": False, "menuCount": 0, "menus": [], "error": str(exc)}

        return payload if isinstance(payload, dict) else {"visible": False, "menuCount": 0, "menus": []}

    async def _model_menu_visible(self) -> bool:
        info = await self._model_menu_info()
        return bool(info.get("visible"))

    async def _click_mimo_model_option(self, target_label: str) -> bool:
        if not self.page:
            return False

        wanted = str(target_label or "").strip()
        wanted_canon = self._canonicalize_model_label(wanted)
        if not wanted_canon:
            return False

        try:
            click_point = await self.page.evaluate(
                    """({menuClasses, wantedRaw, wantedCanon}) => {
                        const compact = (value) => (value || "").toString().replace(/\\s+/g, " ").trim();
                        const canon = (value) => (value || "").toString()
                            .replace(/[^a-z0-9]+/gi, "")
                            .toLowerCase();
                        const optionCanon = (value) => {
                            const label = canon(value);
                            return label.endsWith("new") ? label.slice(0, -3) : label;
                        };
                        const hasClasses = (el, names) => names.every((name) => el.classList.contains(name));
                        const rectInfo = (el) => {
                            if (!el) return null;
                            const rect = el.getBoundingClientRect();
                            if (!rect || rect.width <= 0 || rect.height <= 0) return null;
                            return {
                                x: rect.left,
                                y: rect.top,
                                width: rect.width,
                                height: rect.height,
                                centerX: rect.left + rect.width / 2,
                                centerY: rect.top + rect.height / 2
                            };
                        };
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = rectInfo(el);
                            if (!rect) return false;
                            const style = window.getComputedStyle(el);
                            return style.visibility !== "hidden"
                                && style.display !== "none";
                        };
                        const elementAtPointInfo = (x, y) => {
                            const atPoint = document.elementFromPoint(x, y);
                            return atPoint
                                ? {
                                    tag: compact(atPoint.tagName).toLowerCase(),
                                    role: compact(atPoint.getAttribute("role")),
                                    text: compact(atPoint.textContent).slice(0, 80),
                                    className: compact(atPoint.className).slice(0, 140)
                                }
                                : null;
                        };
                        const menus = Array.from(document.querySelectorAll("div"))
                            .filter((el) => hasClasses(el, menuClasses) && isVisible(el));
                        for (const menu of menus) {
                            const buttons = Array.from(menu.querySelectorAll("button")).filter(isVisible);
                            for (const button of buttons) {
                                const trackName = compact(button.getAttribute("data-track-name"));
                                const text = compact(button.textContent);
                                const labels = [
                                    optionCanon(trackName),
                                    optionCanon(text),
                                    optionCanon(`${trackName} ${text}`)
                                ].filter(Boolean);
                                if (labels.some((label) => label === wantedCanon)) {
                                    try { button.scrollIntoView({block: "center", inline: "center"}); } catch (e) {}
                                    const rect = rectInfo(button);
                                    if (!rect) return null;
                                    return {
                                        x: rect.centerX,
                                        y: rect.centerY,
                                        dataTrackName: trackName.slice(0, 120),
                                        text: text.slice(0, 120),
                                        labels,
                                        menuText: compact(menu.textContent).slice(0, 180),
                                        elementAtClickPoint: elementAtPointInfo(rect.centerX, rect.centerY)
                                    };
                                }
                            }
                        }
                        return null;
                    }""",
                    {
                        "menuClasses": list(self.MODEL_MENU_CLASSES),
                        "wantedRaw": wanted,
                        "wantedCanon": wanted_canon,
                    },
                )
        except Exception as exc:
            Logger.debug(f"MiMo: model option lookup failed: {exc}")
            return False

        if not click_point:
            menu_info = await self._model_menu_info()
            Logger.debug(
                "MiMo: model option lookup found no exact match for "
                f"'{wanted}'; menu info: {json.dumps(menu_info, ensure_ascii=True)[:2000]}"
            )
            return False
        Logger.debug(
            "MiMo: model option click target for "
            f"'{wanted}': {json.dumps(click_point, ensure_ascii=True)[:2000]}"
        )
        clicked = await self._click_viewport_point(click_point, label=f"model option {wanted}")
        Logger.debug(f"MiMo: model option click result for '{wanted}': {clicked}.")
        return clicked

    async def _read_visible_model_options(self) -> list[str]:
        if not self.page:
            return []
        try:
            values = await self.page.evaluate(
                """(menuClasses) => {
                    const hasClasses = (el, names) => names.every((name) => el.classList.contains(name));
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== "hidden"
                            && style.display !== "none";
                    };
                    const out = [];
                    const menus = Array.from(document.querySelectorAll("div"))
                        .filter((el) => hasClasses(el, menuClasses) && isVisible(el));
                    for (const menu of menus) {
                        for (const button of Array.from(menu.querySelectorAll("button")).filter(isVisible)) {
                            const raw = button.getAttribute("data-track-name") || button.textContent || "";
                            const text = raw.toString().trim();
                            if (text) out.push(text);
                        }
                    }
                    return out;
                }""",
                list(self.MODEL_MENU_CLASSES),
            )
        except Exception:
            values = []
        out: list[str] = []
        seen: set[str] = set()
        for value in values if isinstance(values, list) else []:
            label = str(value or "").strip()
            if label and label not in seen:
                seen.add(label)
                out.append(label)
        return out

    async def _ensure_mimo_model_selected(self, desired_label: str) -> None:
        await self._dismiss_blocking_popups()
        desired = str(desired_label or "").strip()
        desired = self.MIMO_RETIRED_MODEL_FALLBACKS.get(desired, desired)
        if not desired:
            return

        current = await self._read_current_mimo_model_label()
        Logger.debug(f"MiMo: model switch check current='{current or 'unknown'}' desired='{desired}'.")
        if self._canonicalize_model_label(current) == self._canonicalize_model_label(desired):
            return

        if not await self._open_mimo_model_dropdown():
            raise RuntimeError("MiMo model selector could not be opened.")

        try:
            if not await self._click_mimo_model_option(desired):
                visible = await self._read_visible_model_options()
                options = ", ".join(visible) if visible else "none detected"
                raise RuntimeError(f"MiMo model '{desired}' was not found. Visible options: {options}.")

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current = await self._read_current_mimo_model_label()
                if self._canonicalize_model_label(current) == self._canonicalize_model_label(desired):
                    Logger.debug(f"MiMo: confirmed model switch to '{current or desired}'.")
                    return
                await asyncio.sleep(0.15)

            current = await self._read_current_mimo_model_label()
            menu_info = await self._model_menu_info()
            Logger.debug(
                "MiMo: model switch confirmation failed; menu info: "
                f"{json.dumps(menu_info, ensure_ascii=True)[:2000]}"
            )
            raise RuntimeError(
                f"MiMo did not confirm model switch to '{desired}' (showing '{current or 'unknown'}')."
            )
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

    def _resolve_request_settings(self, model: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resolved_model = str(model or "").strip() or "mimo-auto"
        mode = resolve_behavior_mode(
            resolved_model,
            self.provider,
            real_model_labels=self.api_real_model_labels(),
        )

        send_thinking = self._get_bool_setting("send_deepthink", False)
        if mode == MODE_CHAT:
            send_thinking = False
        elif mode == MODE_REASONER:
            send_thinking = True

        settings: dict[str, Any] = {
            "model_label": self._get_model_label_for_request(resolved_model),
            "send_deepthink": bool(send_thinking),
            "send_as_text_file": self._get_bool_setting("send_as_text_file", False),
            "count_tokens": self._get_bool_setting("count_tokens", True),
        }

        if overrides:
            if "deepthink_enabled" in overrides:
                settings["send_deepthink"] = bool(overrides["deepthink_enabled"])
            if "send_deepthink" in overrides:
                settings["send_deepthink"] = bool(overrides["send_deepthink"])
            if "send_as_text_file" in overrides:
                settings["send_as_text_file"] = bool(overrides["send_as_text_file"])

        return settings

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        return format_request_messages(self.config_manager, messages)

    def _build_clean_regen_state(
        self,
        *,
        send_thinking: bool,
        send_as_text_file: bool,
        ui_model_label: str,
    ) -> Dict[str, Any]:
        return {
            "deepthink_enabled": bool(send_thinking),
            "search_enabled": False,
            "tools_enabled": False,
            "send_as_text_file": bool(send_as_text_file),
            "ui_model": str(ui_model_label or "").strip(),
        }

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, Any]]:
        return read_clean_regeneration_state(
            self.cache_manager,
            self.clean_regen_state_cache_key,
            log_label="Clean Regeneration (MiMo)",
        )

    def _write_clean_regeneration_state(self, state: Dict[str, Any]) -> None:
        write_clean_regeneration_state(
            self.cache_manager,
            self.clean_regen_state_cache_key,
            state,
        )

    def _parse_conversation_info_from_url(self, url: str) -> Optional[Dict[str, str]]:
        match = self.CHAT_URL_RE.match(str(url or "").strip())
        if not match:
            return None
        conversation_id = str(match.group(1) or "").strip()
        if not conversation_id:
            return None
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"https://aistudio.xiaomimimo.com/#/chat/{conversation_id}",
        }

    async def _get_current_conversation_info(self) -> Optional[Dict[str, str]]:
        if not self.page:
            return None
        return self._parse_conversation_info_from_url(str(self.page.url or ""))

    async def _wait_for_current_conversation_info(
        self,
        timeout_ms: int | None = 6000,
    ) -> Optional[Dict[str, str]]:
        deadline = time.monotonic() + max(0.0, float(timeout_ms or 0) / 1000.0)
        while True:
            info = await self._get_current_conversation_info()
            if info is not None:
                return info
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.15)

    async def _open_cached_conversation(self, conversation_url: str) -> bool:
        if not self.page:
            return False
        try:
            await self.page.goto(conversation_url, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            return True
        except Exception as exc:
            Logger.debug(f"MiMo: failed to open cached conversation: {exc}")
            return False

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
            log_label="Multi-Slot Cache (MiMo)",
        )
        entry = find_multi_slot_cache_entry(payload, account_key, formatted_message, multi_slot_state)
        if not entry:
            return False

        conversation_url = str(entry.get("conversation_url") or "").strip()
        conversation_id = str(entry.get("conversation_id") or "").strip()
        if not conversation_url or not conversation_id:
            return False

        Logger.info("Multi-Slot Cache (MiMo): opening cached conversation for regeneration...")
        if not await self._open_cached_conversation(conversation_url):
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                conversation_id,
                log_label="Multi-Slot Cache (MiMo)",
            )
            return False

        if await self._click_regenerate(arm_event=completion_armed):
            try:
                await asyncio.wait_for(
                    completion_started.wait(),
                    timeout=self._completion_request_timeout_s,
                )
            except asyncio.TimeoutError:
                Logger.warning(
                    "Multi-Slot Cache (MiMo): completion request not observed. Falling back to new chat."
                )
                return False
            Logger.info("Multi-Slot Cache (MiMo): regenerating cached conversation.")
            return True

        remove_multi_slot_cache_entry(
            self.cache_manager,
            self.multi_slot_cache_key,
            account_key,
            conversation_id,
            log_label="Multi-Slot Cache (MiMo)",
        )
        return False

    async def set_sidebar_status(self, open: bool) -> None:
        _ = open
        return None

    async def click_new_chat(self, source: str = "auto") -> None:
        _ = source
        if not self.page:
            return
        await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=45000)
        await self._wait_for_chat_ready(timeout_ms=60000)

    async def set_deepthink_state(self, state: bool) -> None:
        _ = state
        return None

    async def set_search_state(self, state: bool) -> None:
        _ = state
        return None

    async def upload_file(self, file_spec: Any) -> None:
        await self._upload_file(file_spec)

    async def _upload_file(self, file_spec: Any) -> bool:
        if not self.page:
            return False

        temp_dir: str | None = None
        upload_target = file_spec

        try:
            if isinstance(file_spec, dict):
                buffer = file_spec.get("buffer")
                if not isinstance(buffer, (bytes, bytearray)) or not buffer:
                    return False
                name = os.path.basename(str(file_spec.get("name") or "prompt.txt")) or "prompt.txt"
                temp_dir = tempfile.mkdtemp(prefix="irp-mimo-upload-")
                temp_path = os.path.join(temp_dir, name)
                with open(temp_path, "wb") as handle:
                    handle.write(buffer)
                upload_target = temp_path

            file_input = self.page.locator(self.FILE_INPUT_SELECTOR)
            if await file_input.count() == 0:
                try:
                    await self.page.wait_for_selector(self.FILE_INPUT_SELECTOR, timeout=8000)
                except Exception:
                    pass
                file_input = self.page.locator(self.FILE_INPUT_SELECTOR)

            if await file_input.count() == 0:
                Logger.warning("MiMo: file input not found.")
                return False

            await file_input.first.set_input_files(upload_target)
            try:
                await self.page.evaluate(
                    """() => {
                        for (const input of document.querySelectorAll("input[type='file']")) {
                            try { input.dispatchEvent(new Event("input", {bubbles: true})); } catch (e) {}
                            try { input.dispatchEvent(new Event("change", {bubbles: true})); } catch (e) {}
                        }
                    }"""
                )
            except Exception:
                pass

            timeout_s = self._get_int_setting("file_upload_timeout", 30, minimum=1)
            await self._wait_for_upload_parsing(timeout_s=timeout_s)
            return True
        except Exception as exc:
            Logger.warning(f"MiMo: file upload failed: {exc}")
            return False
        finally:
            if temp_dir:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

    async def _wait_for_upload_parsing(self, *, timeout_s: float) -> None:
        if not self.page:
            return

        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        no_indicator_deadline = time.monotonic() + min(max(0.0, float(timeout_s or 0.0)), 2.0)
        saw_parsing = False
        while True:
            parsing = False
            try:
                parsing = bool(
                    await self.page.evaluate(
                        """() => {
                            const hasClasses = (el) => el.classList.contains("truncate")
                                && el.classList.contains("text-xs");
                            const isVisible = (el) => {
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                return rect.width > 0 && rect.height > 0
                                    && style.visibility !== "hidden"
                                    && style.display !== "none";
                            };
                            return Array.from(document.querySelectorAll("div"))
                                .some((el) => hasClasses(el)
                                    && isVisible(el)
                                    && (el.textContent || "").trim().startsWith("Parsing"));
                        }"""
                    )
                )
            except Exception:
                parsing = False

            saw_parsing = saw_parsing or parsing
            if saw_parsing and not parsing:
                return
            if not saw_parsing and time.monotonic() >= no_indicator_deadline:
                return
            if time.monotonic() >= deadline:
                if parsing:
                    raise TimeoutError("MiMo file parsing did not finish before timeout.")
                return
            await asyncio.sleep(0.2)

    async def enter_message(self, message: str) -> None:
        if not self.page:
            return
        await self._dismiss_blocking_popups()
        textarea = self.page.locator(self.CHAT_TEXTAREA_SELECTOR)
        if await textarea.count() == 0:
            Logger.warning("MiMo: message textarea not found.")
            return
        await textarea.first.fill(str(message or ""))

    async def send_message(self, timeout: int | None = None) -> bool:
        return await self._send_message(timeout=timeout)

    async def _send_message(
        self,
        timeout: int | None = None,
        arm_event: asyncio.Event | None = None,
    ) -> bool:
        if not self.page:
            return False

        if arm_event:
            arm_event.set()

        max_wait_s = 0 if not timeout else max(int(timeout), 0)
        start = time.monotonic()
        last_error: Exception | None = None

        while True:
            await self._dismiss_blocking_popups()
            button = self.page.locator(self.SEND_BUTTON_SELECTOR)
            try:
                count = await button.count()
            except Exception:
                count = 0

            for idx in range(count - 1, -1, -1):
                candidate = button.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                    disabled_attr = await candidate.get_attribute("disabled")
                    aria_disabled = str(await candidate.get_attribute("aria-disabled") or "").lower()
                    is_enabled = await candidate.is_enabled()
                    if disabled_attr is not None or aria_disabled == "true" or not is_enabled:
                        last_error = RuntimeError("Send button is disabled.")
                        continue
                    await candidate.click(timeout=3000)
                    return True
                except Exception as exc:
                    last_error = exc
                    try:
                        await candidate.evaluate("el => el.click()")
                        return True
                    except Exception:
                        continue

            if max_wait_s <= 0 or time.monotonic() - start >= max_wait_s:
                break
            await asyncio.sleep(0.1)

        if last_error:
            Logger.warning(f"MiMo: failed to click send button: {last_error}")
        else:
            Logger.warning("MiMo: send button not found.")
        return False

    async def _click_stop_button(self, timeout_s: float = 8.0) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        while True:
            button = self.page.locator(self.SEND_BUTTON_SELECTOR)
            try:
                count = await button.count()
            except Exception:
                count = 0

            for idx in range(count - 1, -1, -1):
                candidate = button.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                    await candidate.click(timeout=1500)
                    return True
                except Exception:
                    try:
                        await candidate.evaluate("el => el.click()")
                        return True
                    except Exception:
                        continue

            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _click_regenerate(self, arm_event: asyncio.Event | None = None) -> bool:
        if not self.page:
            return False

        await self._dismiss_blocking_popups()
        if arm_event:
            arm_event.set()

        button = self.page.locator(self.REGENERATE_BUTTON_SELECTOR)
        try:
            count = await button.count()
        except Exception:
            count = 0

        for idx in range(count - 1, -1, -1):
            candidate = button.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
                await candidate.click(timeout=3000)
                return True
            except Exception:
                try:
                    await candidate.evaluate("el => el.click()")
                    return True
                except Exception:
                    continue
        return False

    async def _enqueue_parser_event(
        self,
        response_queue: asyncio.Queue,
        event: dict[str, Any],
    ) -> None:
        event_type = str(event.get("type") or "")
        model_name = self.current_model or "mimo-auto"
        if event_type == "delta":
            content = str(event.get("content") or "")
            finish_reason = event.get("finish_reason")
            if content or finish_reason:
                await response_queue.put(
                    make_openai_delta_sse(
                        model_name,
                        content,
                        finish_reason=str(finish_reason) if finish_reason else None,
                    )
                )
            return

        if event_type == "usage":
            usage = event.get("usage")
            if isinstance(usage, dict):
                await response_queue.put(make_openai_usage_sse(model_name, usage))
            return

        if event_type == "error":
            await response_queue.put({"error": str(event.get("message") or "MiMo stream error.")})

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "mimo-auto",
        stream: bool = False,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        abort_event: asyncio.Event | None = None,
    ):
        _ = stream, temperature, top_p, max_tokens
        if not self.page or not self.context:
            yield f"data: {json.dumps({'error': 'MiMo driver is not running.'})}\n\n"
            return

        await self.require_english_ui()
        await self._dismiss_blocking_popups()
        self._refresh_quirks()

        response_queue: asyncio.Queue = asyncio.Queue()
        completion_armed = asyncio.Event()
        completion_started = asyncio.Event()
        completion_claim_lock = asyncio.Lock()
        completion_claimed = False
        provider_activity_count = 0

        self.abort_requested = False
        self.current_abort_event = abort_event
        resolved_model = str(model or "").strip() or "mimo-auto"
        self.current_model = resolved_model

        macros_overrides: Dict[str, Any] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = strip_macros_from_messages(
                message,
                macro_actions=COMMON_REQUEST_MACRO_ACTIONS,
            )
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = extract_macro_overrides(
                message,
                macro_actions=COMMON_REQUEST_MACRO_ACTIONS,
            )
        if macros_overrides:
            Logger.debug(f"MiMo macros applied: {macros_overrides}")

        effective_settings = self._resolve_request_settings(
            resolved_model,
            overrides=macros_overrides,
        )
        ui_model_label = str(effective_settings.get("model_label") or "MiMo-V2.6-Pro")
        send_thinking = bool(effective_settings.get("send_deepthink"))
        send_as_text_file = bool(effective_settings.get("send_as_text_file"))
        include_usage = bool(effective_settings.get("count_tokens"))
        self.current_send_deepthink = send_thinking

        formatted_message = self._format_messages(message_for_formatting)
        extra_prompt_texts: dict[str, str] = {}
        text_file_message = self._get_str_setting(
            "text_file_message",
            "Please read the attached file and respond to it.",
        ).strip()
        if send_as_text_file:
            extra_prompt_texts["text_file_message"] = (
                text_file_message or "Please read the attached file and respond to it."
            )

        self._capture_diagnostics_prompt_snapshot(
            formatted_message,
            extra_prompt_texts=extra_prompt_texts or None,
            metadata={
                "model": resolved_model,
                "ui_model": ui_model_label,
                "send_deepthink": send_thinking,
                "send_as_text_file": send_as_text_file,
                "count_tokens": include_usage,
            },
        )

        cdp_session: Any = None
        cdp_listeners_registered = False
        cdp_tasks: set[asyncio.Task] = set()
        request_methods: dict[str, str] = {}
        stream_parsers: dict[str, _MimoEventStreamParser] = {}

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"MiMo: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"MiMo: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        async def finish_stream(
            stream_id: str,
            parser: _MimoEventStreamParser,
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            if not aborted and not encountered_error:
                for event in parser.finish():
                    await self._enqueue_parser_event(response_queue, event)
                    if str(event.get("type") or "") == "error":
                        encountered_error = True

            if (
                not aborted
                and not encountered_error
                and not parser.emitted_text
                and not parser.sensitive_query_seen
            ):
                message_text = (
                    "MiMo returned no assistant text. The request may have failed before "
                    "the answer stream started."
                )
                Logger.warning(message_text)
                await response_queue.put({"error": message_text})
                encountered_error = True

            await response_queue.put(None)
            stream_parsers.pop(stream_id, None)
            if not aborted and not encountered_error and not self.abort_requested:
                Logger.success("MiMo CDP stream completed.")

        async def feed_stream_chunk(
            stream_id: str,
            parser: _MimoEventStreamParser,
            data: bytes,
        ) -> None:
            nonlocal provider_activity_count
            if not data:
                return
            provider_activity_count += 1
            if self.abort_requested or (abort_event and abort_event.is_set()):
                await finish_stream(stream_id, parser, aborted=True)
                return
            for event in parser.feed(data):
                await self._enqueue_parser_event(response_queue, event)

        async def feed_base64_stream_chunk(
            stream_id: str,
            parser: _MimoEventStreamParser,
            encoded_data: Any,
        ) -> None:
            if not encoded_data:
                return
            encoded_text = str(encoded_data)
            try:
                data = base64.b64decode(encoded_text, validate=True)
            except Exception:
                data = encoded_text.encode("utf-8", errors="ignore")
            await feed_stream_chunk(stream_id, parser, data)

        async def start_cdp_stream(request_id: str, url: str) -> None:
            nonlocal completion_claimed
            if not request_id or not completion_armed.is_set() or not cdp_session:
                return

            async with completion_claim_lock:
                if completion_claimed:
                    return
                completion_claimed = True
                completion_started.set()
                stream_parsers[request_id] = _MimoEventStreamParser(
                    send_thinking=send_thinking,
                    include_usage=include_usage,
                )

            parser = stream_parsers[request_id]
            Logger.info("Teeing MiMo chat response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message_text = f"MiMo CDP response streaming failed: {exc}"
                Logger.error(message_text)
                await response_queue.put({"error": message_text})
                await finish_stream(request_id, parser, encountered_error=True)
                return

            if isinstance(result, dict):
                await feed_base64_stream_chunk(request_id, parser, result.get("bufferedData"))

        async def handle_response_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            response = params.get("response")
            if not request_id or not isinstance(response, dict):
                return
            url = str(response.get("url") or "")
            if not url.startswith(self.CHAT_REQUEST_URL_PREFIX):
                return
            method = request_methods.get(request_id, "").upper()
            if method and method != "POST":
                return
            await start_cdp_stream(request_id, url)

        async def handle_data_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            parser = stream_parsers.get(request_id)
            if parser:
                await feed_base64_stream_chunk(request_id, parser, params.get("data"))

        async def handle_loading_finished(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request_methods.pop(request_id, None)
            parser = stream_parsers.get(request_id)
            if parser:
                await finish_stream(request_id, parser)

        async def handle_loading_failed(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request_methods.pop(request_id, None)
            parser = stream_parsers.get(request_id)
            if not parser:
                return
            if self.abort_requested or (abort_event and abort_event.is_set()):
                await finish_stream(request_id, parser, aborted=True)
                return
            error_text = str(params.get("errorText") or "network loading failed").strip()
            if "ERR_ABORTED" in error_text.upper() and (
                parser.emitted_text or parser.provider_final_seen
            ):
                Logger.debug(
                    "MiMo CDP stream ended with net::ERR_ABORTED after answer data arrived; "
                    "treating it as complete."
                )
                await finish_stream(request_id, parser)
                return
            message_text = f"MiMo CDP stream failed: {error_text}"
            Logger.error(message_text)
            await response_queue.put({"error": message_text})
            await finish_stream(request_id, parser, encountered_error=True)

        def on_request_will_be_sent(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request = params.get("request")
            if not request_id or not isinstance(request, dict):
                return
            request_methods[request_id] = str(request.get("method") or "").upper()

        def on_response_received(params: Any) -> None:
            _schedule_cdp_task(handle_response_received(params), "responseReceived")

        def on_data_received(params: Any) -> None:
            _schedule_cdp_task(handle_data_received(params), "dataReceived")

        def on_loading_finished(params: Any) -> None:
            _schedule_cdp_task(handle_loading_finished(params), "loadingFinished")

        def on_loading_failed(params: Any) -> None:
            _schedule_cdp_task(handle_loading_failed(params), "loadingFailed")

        try:
            try:
                cdp_session = await self.context.new_cdp_session(self.page)
                await cdp_session.send("Network.enable", {})
                cdp_session.on("Network.requestWillBeSent", on_request_will_be_sent)
                cdp_session.on("Network.responseReceived", on_response_received)
                cdp_session.on("Network.dataReceived", on_data_received)
                cdp_session.on("Network.loadingFinished", on_loading_finished)
                cdp_session.on("Network.loadingFailed", on_loading_failed)
                cdp_listeners_registered = True
            except Exception as exc:
                message_text = f"MiMo CDP setup failed: {exc}"
                Logger.error(message_text)
                yield f"data: {json.dumps({'error': message_text})}\n\n"
                return

            clean_regeneration = self._get_bool_setting("clean_regeneration", False)
            multi_slot_cache_enabled = bool(
                clean_regeneration and self._get_bool_setting("multi_slot_cache", False)
            )
            cache_state = self._build_clean_regen_state(
                send_thinking=send_thinking,
                send_as_text_file=send_as_text_file,
                ui_model_label=ui_model_label,
            )

            regenerated = False
            current_cache_matched = False
            should_record_multi_slot = False

            if clean_regeneration:
                last_message = self.cache_manager.read_cache(self.clean_regen_message_cache_key)
                last_state = self._read_clean_regeneration_state()
                current_cache_matched = last_message == formatted_message and last_state == cache_state
                if current_cache_matched:
                    Logger.info(
                        "Clean Regeneration (MiMo): Message and settings match cache. Attempting to regenerate..."
                    )
                    await self.apply_configured_model(model=resolved_model)
                    if await self._click_regenerate(arm_event=completion_armed):
                        try:
                            await asyncio.wait_for(
                                completion_started.wait(),
                                timeout=self._completion_request_timeout_s,
                            )
                        except asyncio.TimeoutError:
                            Logger.warning(
                                "Clean Regeneration (MiMo): completion request not observed. Falling back to new chat."
                            )
                        else:
                            regenerated = True
                            self.cache_manager.write_cache(
                                self.clean_regen_message_cache_key,
                                formatted_message,
                            )
                            self._write_clean_regeneration_state(cache_state)

            if (
                (not regenerated)
                and multi_slot_cache_enabled
                and (not current_cache_matched)
            ):
                await self.apply_configured_model(model=resolved_model)
                regenerated = await self._try_multi_slot_regeneration(
                    formatted_message=formatted_message,
                    multi_slot_state=cache_state,
                    completion_armed=completion_armed,
                    completion_started=completion_started,
                )
                if regenerated:
                    self.cache_manager.write_cache(
                        self.clean_regen_message_cache_key,
                        formatted_message,
                    )
                    self._write_clean_regeneration_state(cache_state)

            if not regenerated:
                Logger.info("MiMo: preparing new chat session...")
                await self.click_new_chat(source="auto")
                await self.apply_configured_model(model=resolved_model)
                await asyncio.sleep(0.2)

                if send_as_text_file:
                    Logger.info("MiMo: sending message as text file...")
                    uploaded = await self._upload_file(build_prompt_text_file_payload(formatted_message))
                    if uploaded:
                        companion = text_file_message or "Please read the attached file and respond to it."
                        await self.enter_message(companion)
                        timeout = self._get_int_setting("file_upload_timeout", 30, minimum=1)
                        sent = await self._send_message(timeout=timeout, arm_event=completion_armed)
                    else:
                        Logger.warning("MiMo: falling back to pasted text for this request.")
                        await self.enter_message(formatted_message)
                        timeout = self._get_int_setting("message_send_timeout", 8, minimum=1)
                        sent = await self._send_message(timeout=timeout, arm_event=completion_armed)
                else:
                    await self.enter_message(formatted_message)
                    timeout = self._get_int_setting("message_send_timeout", 8, minimum=1)
                    sent = await self._send_message(timeout=timeout, arm_event=completion_armed)

                if not sent:
                    yield f"data: {json.dumps({'error': 'MiMo: send button not found or stayed disabled.'})}\n\n"
                    return

                if clean_regeneration:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(cache_state)
                    should_record_multi_slot = bool(multi_slot_cache_enabled)

            if not completion_started.is_set():
                try:
                    await asyncio.wait_for(
                        completion_started.wait(),
                        timeout=self._completion_request_timeout_s,
                    )
                except asyncio.TimeoutError:
                    message_text = "MiMo: completion request not observed"
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return

            stream_had_error = False
            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self._first_chunk_timeout_s,
                idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                on_timeout=lambda: self._click_stop_button(timeout_s=4.0),
                activity_counter=lambda: provider_activity_count,
            ):
                if isinstance(item, dict) and "error" in item:
                    stream_had_error = True
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                yield item

            if (
                should_record_multi_slot
                and (not stream_had_error)
                and (not self.abort_requested)
                and not (abort_event and abort_event.is_set())
            ):
                conversation_info = await self._wait_for_current_conversation_info(timeout_ms=6000)
                if conversation_info is not None:
                    upsert_multi_slot_cache_entry(
                        self.cache_manager,
                        self.multi_slot_cache_key,
                        self._get_multi_slot_cache_account_key(),
                        {
                            "conversation_id": conversation_info["conversation_id"],
                            "conversation_url": conversation_info["conversation_url"],
                            "prompt": formatted_message,
                            "state": cache_state,
                        },
                        log_label="Multi-Slot Cache (MiMo)",
                    )
        finally:
            self.current_abort_event = None
            self.abort_requested = False
            self.current_model = None
            self.current_send_deepthink = None
            if cdp_session and cdp_listeners_registered:
                for event_name, listener in (
                    ("Network.requestWillBeSent", on_request_will_be_sent),
                    ("Network.responseReceived", on_response_received),
                    ("Network.dataReceived", on_data_received),
                    ("Network.loadingFinished", on_loading_finished),
                    ("Network.loadingFailed", on_loading_failed),
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
                    Logger.debug(f"MiMo: CDP detach failed: {exc}")
