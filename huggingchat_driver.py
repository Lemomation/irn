from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from drivers.base_driver import BaseDriver, RequestCancelled
from drivers.providers import DriverProvider
from drivers.shared_utils import (
    COMMON_REQUEST_MACRO_ACTIONS,
    IncrementalTextAccumulator,
    build_prompt_text_file_payload,
    extract_macro_overrides,
    find_multi_slot_cache_entry,
    format_request_messages,
    make_openai_delta_sse,
    read_multi_slot_cache_payload,
    remove_multi_slot_cache_entry,
    split_leading_system_messages,
    strip_macros_from_messages,
    upsert_multi_slot_cache_entry,
)
from utils.cache_manager import CacheManager
from utils.logger import Logger
from utils.model_ids import (
    MODE_CHAT,
    MODE_REASONER,
    resolve_behavior_mode,
    resolve_real_model_label_from_model_id,
)


class _ConcatenatedJsonEventParser:
    """Parse HuggingChat's concatenated JSON stream chunks."""

    def __init__(self) -> None:
        self._decoder = json.JSONDecoder()
        self._buffer = ""

    def feed(self, chunk: bytes | str) -> list[dict[str, Any]]:
        if isinstance(chunk, bytes):
            text = chunk.decode("utf-8", errors="ignore")
        else:
            text = str(chunk or "")
        if not text:
            return []

        self._buffer += text
        out: list[dict[str, Any]] = []

        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break

            try:
                payload, end_pos = self._decoder.raw_decode(self._buffer)
            except json.JSONDecodeError:
                # Usually means the current chunk ended mid-object.
                break

            self._buffer = self._buffer[end_pos:]
            if isinstance(payload, dict):
                out.append(payload)

        # Avoid retaining a very large invalid tail forever
        if len(self._buffer) > 262_144 and not self._buffer.lstrip().startswith("{"):
            self._buffer = self._buffer[-4096:]

        return out

    def finish(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            try:
                payload, end_pos = self._decoder.raw_decode(self._buffer)
            except json.JSONDecodeError:
                break
            self._buffer = self._buffer[end_pos:]
            if isinstance(payload, dict):
                out.append(payload)
        return out


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


class HuggingChatDriver(BaseDriver):
    CHAT_URL = "https://huggingface.co/chat"
    LOGIN_URL = "https://huggingface.co/login"
    CONVERSATION_URL_RE = re.compile(
        r"^https://huggingface\.co/chat/conversation/([^/?#]+)",
        re.IGNORECASE,
    )
    CONVERSATION_ROUTE_RE = re.compile(
        r"https://huggingface\.co/chat/conversation/([^/?#]+)",
        re.IGNORECASE,
    )
    CHAT_TEXTAREA_SELECTOR = "textarea[placeholder='Ask anything']"
    SEND_BUTTON_SELECTOR = "button[type='submit'][aria-label='Send message'], button[aria-label='Send message']"
    ADD_ATTACHMENT_SELECTOR = "button[aria-label='Add attachment']"
    DISABLE_MCP_SELECTOR = "button[aria-label='Disable all MCP servers']"
    STOP_BUTTON_SELECTORS = (
        "button[aria-label='Stop generating']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Cancel']",
        "button[aria-label*='Interrupt']",
        "button[title*='Stop']",
    )
    RETRY_BUTTON_SELECTOR = (
        "button[title='Retry'], "
        "button[aria-label='Retry'], "
        "button[title*='Regenerate'], "
        "button[aria-label*='Regenerate']"
    )
    MODEL_SETTINGS_LINK_SELECTOR = "a[href*='/settings/chat/'], a[href*='/chat/settings/']"
    MODEL_LIST_UNAVAILABLE_LABEL = (
        "Model list unavailable, please successfully log into HuggingChat at least once"
    )

    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    FILE_UPLOAD_SETTLE_DELAY_S = 3.0

    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.HUGGINGCHAT)
        self.cache_manager = CacheManager()
        self.clean_regen_message_cache_key = "huggingchat_last_message.txt"
        self.clean_regen_state_cache_key = "huggingchat_last_message_state.json"
        self.multi_slot_cache_key = "huggingchat_multi_slot_cache.json"
        self.model_cache_key = "huggingchat_models.json"
        self.model_state_cache_key = "huggingchat_model_state.json"

        self.current_model: Optional[str] = None
        self.current_send_deepthink: Optional[bool] = None
        self._abort_ui_task: asyncio.Task | None = None
        self._hchat_request_active = False
        self._hchat_response_activity_seen = False
        self._pending_request_overrides: dict[str, Any] = {}
        self._last_applied_model_state: dict[str, Any] | None = None
        self._refresh_quirks()

    def get_start_url(self) -> str:
        return self.CHAT_URL

    @property
    def required_ui_language_label(self) -> str:
        return "English (en)"

    def _is_required_ui_language(self, lang: str) -> bool:
        return str(lang or "").strip().lower() == "en"

    def should_apply_configured_model_before_request(self) -> bool:
        return False

    def _refresh_quirks(self) -> None:
        self._completion_request_timeout_s = self._get_float_setting(
            "completion_request_timeout",
            150.0,
            minimum=5.0,
        )
        self._first_chunk_timeout_s = self._get_float_setting(
            "first_chunk_timeout",
            150.0,
            minimum=5.0,
        )
        self._file_upload_settle_delay_s = self._get_float_setting(
            "file_upload_settle_delay",
            self.FILE_UPLOAD_SETTLE_DELAY_S,
            minimum=0.0,
        )
        self._model_apply_timeout_s = self._get_float_setting(
            "model_apply_timeout",
            20.0,
            minimum=3.0,
        )
        self._post_action_delay_s = self._get_float_setting(
            "post_action_delay",
            0.35,
            minimum=0.0,
        )

    def _get_float_setting(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        try:
            value = float(self.config_manager.get_setting("huggingchat_behavior", key) or default)
        except Exception:
            value = default
        return max(float(minimum), value)

    def _get_bool_setting(self, key: str, default: bool = False) -> bool:
        try:
            value = self.config_manager.get_setting("huggingchat_behavior", key)
        except Exception:
            return default
        if value is None:
            return default
        return bool(value)

    def _get_str_setting(self, key: str, default: str = "") -> str:
        try:
            value = self.config_manager.get_setting("huggingchat_behavior", key)
        except Exception:
            value = default
        return str(value if value is not None else default)

    def set_request_overrides(self, overrides: dict[str, Any] | None = None) -> None:
        self._pending_request_overrides = dict(overrides or {})

    async def after_start(self, status_callback=None) -> None:
        await self._recover_chat_route_if_needed()
        await self.check_ui_language(status_callback=status_callback)
        try:
            if await self._is_logged_in():
                await self._go_to_chat()
                await self.refresh_api_model_cache()
        except Exception as exc:
            Logger.debug(f"HuggingChat: model cache refresh skipped: {exc}")

    async def cleanup_background_tasks(self) -> None:
        await self._cancel_task(self._abort_ui_task, label="stopping HuggingChat abort UI task")
        self._abort_ui_task = None

    def request_abort(self) -> None:
        super().request_abort()
        if not self._hchat_request_active:
            return
        self._schedule_abort_ui_action(use_stop=self._hchat_response_activity_seen)

    @staticmethod
    def _is_rate_limit_reason_text(reason: str) -> bool:
        lowered = str(reason or "").lower()
        return (
            "rate limit" in lowered
            or "quota" in lowered
            or "upgrade required" in lowered
            or "limit reached" in lowered
            or "429" in lowered
        )

    def ece_rotate_identity(self, reason: str) -> bool:
        if not self._is_rate_limit_reason_text(reason):
            return super().ece_rotate_identity(reason)

        previous_disable_profile_rotation = bool(
            getattr(self, "_ece_disable_profile_slot_rotation", False)
        )
        self._ece_disable_profile_slot_rotation = True
        try:
            return super().ece_rotate_identity(reason)
        finally:
            self._ece_disable_profile_slot_rotation = previous_disable_profile_rotation

    def _auto_disable_active_account_after_rate_limit(self, reason: str) -> bool:
        if not self._get_bool_setting("auto_disable_ratelimited_accounts", False):
            return False

        pair = self.ece_active_pair()
        email = str(getattr(pair, "email", "") or "").strip()
        if not email:
            Logger.warning(
                "HuggingChat: rate-limited account could not be auto-disabled because "
                "the active saved account is unknown."
            )
            return False

        try:
            disabled = bool(
                self._get_ece_manager().set_pair_disabled(
                    self.provider,
                    email,
                    disabled=True,
                    allow_disable_last=True,
                )
            )
        except Exception as exc:
            Logger.warning(f"HuggingChat: failed to auto-disable rate-limited account: {exc}")
            return False

        if not disabled:
            Logger.warning(
                f"HuggingChat: failed to auto-disable rate-limited account '{email}'."
            )
            return False

        message = (
            f"HuggingChat: auto-disabled rate-limited account '{email}'. "
            "Re-enable it in Credential Manager after the credits reset."
        )
        Logger.warning(f"{message} Reason: {reason}")
        self.notify_user(
            "HuggingChat Account Disabled",
            message,
            level="warning",
        )
        return True

    def _schedule_abort_ui_action(self, *, use_stop: bool) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            existing = self._abort_ui_task
            if existing and not existing.done():
                return
            self._abort_ui_task = loop.create_task(
                self._handle_abort_ui_action(use_stop=bool(use_stop))
            )
        except Exception:
            pass

    async def _handle_abort_ui_action(self, *, use_stop: bool) -> None:
        try:
            if use_stop:
                await self._click_stop_button(timeout_s=8.0)
                return
            await self._open_new_chat_after_aborted_wait()
        except (Exception, RequestCancelled) as exc:
            # (A cleanup task: the cancel checks in shared steps are meant for the request.)
            Logger.debug(f"HuggingChat: abort UI action failed: {exc!r}")

    async def _open_new_chat_after_aborted_wait(self) -> bool:
        if not self.page:
            return False
        Logger.info("HuggingChat: abort before response chunks; opening a fresh chat.")
        try:
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            return True
        except Exception as exc:
            Logger.debug(f"HuggingChat: direct fresh-chat navigation after abort failed: {exc}")

        try:
            await self.click_new_chat(source="abort")
            return True
        except Exception as exc:
            Logger.debug(f"HuggingChat: fallback fresh-chat action after abort failed: {exc}")
            return False

    async def _wait_for_event_or_abort(
        self,
        event: asyncio.Event,
        *,
        timeout_s: float,
        abort_event: asyncio.Event | None = None,
        poll_s: float = 0.25,
    ) -> str:
        if event.is_set():
            return "set"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(float(timeout_s or 0.0), 0.0)
        while True:
            if self.abort_requested or (abort_event and abort_event.is_set()):
                return "aborted"

            timeout_left = deadline - loop.time()
            if timeout_left <= 0:
                return "timeout"

            try:
                await asyncio.wait_for(event.wait(), timeout=min(timeout_left, poll_s))
            except asyncio.TimeoutError:
                continue

            if event.is_set():
                return "set"

    async def _upgrade_required_visible(self, *, timeout_ms: int = 2500) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            try:
                visible = bool(
                    await self.page.evaluate(
                        """() => {
                            const normalize = (value) => String(value || '')
                                .replace(/\\s+/g, ' ')
                                .trim()
                                .toLowerCase();
                            const isVisible = (el) => {
                                if (!el) return false;
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                return rect.width > 0 && rect.height > 0
                                    && style.visibility !== 'hidden'
                                    && style.display !== 'none'
                                    && Number(style.opacity || '1') !== 0;
                            };
                            return Array.from(document.querySelectorAll('h2')).some((heading) =>
                                isVisible(heading) && normalize(heading.textContent) === 'upgrade required'
                            );
                        }"""
                    )
                )
                if visible:
                    return True
            except Exception as exc:
                Logger.debug(f"HuggingChat: Upgrade Required check failed: {exc}")
                return False

            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def _upgrade_required_rate_limit_message(
        self,
        *,
        timeout_ms: int = 2500,
        context: str = "",
    ) -> str:
        if not await self._upgrade_required_visible(timeout_ms=timeout_ms):
            return ""

        message = "HuggingChat rate limit: Upgrade Required is visible in the web UI."
        if context:
            Logger.debug(f"HuggingChat: Upgrade Required detected after {context}")
        Logger.warning(message)
        self._auto_disable_active_account_after_rate_limit(message)
        return message

    async def _is_logged_in(self, *, allow_chat_input_fallback: bool = True) -> bool:
        if not self.page:
            return False
        if await self._login_form_visible():
            return False
        try:
            has_account_indicator = bool(
                await self.page.evaluate(
                    """() => {
                        const avatarPrefix = "https://huggingface.co/api/users/";
                        if (document.querySelector(`a[href^="${avatarPrefix}"][href*="/avatar?redirect=true"]`)) {
                            return true;
                        }

                        const requiredClasses = [
                            "group",
                            "flex",
                            "h-8",
                            "items-center",
                            "gap-1.5",
                            "rounded-lg",
                            "pl-2",
                            "pr-2",
                        ];
                        const hasAccountChipShape = (element) =>
                            requiredClasses.every((className) => element.classList.contains(className));

                        for (const container of document.querySelectorAll("div")) {
                            if (!hasAccountChipShape(container)) {
                                continue;
                            }

                            const children = Array.from(container.children);
                            const avatar = children[0];
                            if (!avatar || avatar.tagName !== "IMG") {
                                continue;
                            }
                            const avatarUrl = avatar.currentSrc || avatar.src || "";
                            if (!avatarUrl.startsWith(avatarPrefix)) {
                                continue;
                            }

                            const profileLink = children.find((child) => child.tagName === "A");
                            const profileName = (profileLink?.textContent || "").trim().replace(/^@/, "");
                            if (!profileLink || !profileName) {
                                continue;
                            }

                            const expectedHref = `https://huggingface.co/${profileName}`;
                            const encodedHref = `https://huggingface.co/${encodeURIComponent(profileName)}`;
                            if (profileLink.href === expectedHref || profileLink.href === encodedHref) {
                                return true;
                            }
                        }

                        return false;
                    }"""
                )
            )
            if has_account_indicator:
                return True
        except Exception:
            pass

        if await self._start_chatting_visible():
            return False
        if not allow_chat_input_fallback:
            return False
        return await self._chat_input_available()

    async def _chat_input_available(self) -> bool:
        if not self.page:
            return False
        if not str(self.page.url or "").startswith(self.CHAT_URL):
            return False
        try:
            textarea = self.page.locator(self.CHAT_TEXTAREA_SELECTOR)
            return await textarea.count() > 0 and await textarea.first.is_visible()
        except Exception:
            return False

    async def _wait_until_logged_in(self, timeout_s: float | None = None) -> None:
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        while True:
            await self._recover_chat_route_if_needed()
            if await self._is_logged_in():
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("HuggingChat login was not detected before timeout.")
            await asyncio.sleep(0.5)

    async def _login_form_visible(self) -> bool:
        if not self.page:
            return False
        try:
            username = self.page.locator("input[name='username']")
            password = self.page.locator("input[name='password']")
            return (
                await username.count() > 0
                and await password.count() > 0
                and await username.first.is_visible()
            )
        except Exception:
            return False

    async def _start_chatting_visible(self) -> bool:
        if not self.page:
            return False
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && Number(style.opacity || '1') !== 0;
                        };
                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        return Array.from(document.querySelectorAll('button, a')).some((item) =>
                            isVisible(item) && normalize(item.textContent) === 'Start chatting'
                        );
                    }"""
                )
            )
        except Exception:
            return False

    async def _wait_for_login_form_or_authenticated(self, timeout_s: float = 8.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if await self._is_logged_in(allow_chat_input_fallback=False):
                return False
            if await self._login_form_visible():
                return True

            current_url = str(self.page.url or "") if self.page else ""
            if self._is_huggingface_home_url(current_url) or self._is_huggingface_welcome_url(current_url):
                await self._recover_chat_route_if_needed()
                if await self._is_logged_in(allow_chat_input_fallback=False):
                    return False
            elif current_url.startswith(self.CHAT_URL) and await self._start_chatting_visible():
                Logger.info("HuggingChat: Start chatting gate detected after returning to chat.")
                await self._click_start_chatting_if_present(
                    wait_ms=2000,
                    wait_for_resolution=True,
                )
                if await self._login_form_visible():
                    return True
                if await self._is_logged_in(allow_chat_input_fallback=False):
                    return False

            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def login(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        await self._recover_chat_route_if_needed()
        if await self._is_logged_in():
            Logger.info("HuggingChat: already signed in.")
            self._mark_active_ece_pair_used()
            await self._go_to_chat()
            return

        auto_login = False
        try:
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        except Exception:
            auto_login = False

        if not auto_login:
            Logger.info("HuggingChat: Auto Login disabled. Waiting for manual login...")
            self.notify_user(
                "HuggingChat Login",
                "Please log in to HuggingChat in the browser window, then come back here.",
                level="info",
            )
            self._background_login_needed("Auto Login is off, so sign-in has to happen in the browser")
            await self._wait_until_logged_in(timeout_s=None)
            await self._go_to_chat()
            self._mark_active_ece_pair_used()
            return

        pair = self.ece_active_pair()
        if pair is None:
            Logger.warning(
                "HuggingChat: Auto Login is enabled but no enabled accounts are configured. "
                "Waiting for manual login..."
            )
            self.notify_user(
                "HuggingChat Login",
                "Auto Login is enabled, but no enabled HuggingChat accounts are saved. Please log in manually.",
                level="warning",
            )
            self._background_login_needed("Auto Login has no saved account for this provider")
            await self._wait_until_logged_in(timeout_s=None)
            await self._go_to_chat()
            self._mark_active_ece_pair_used()
            return

        username = str(pair.email or "").strip()
        password = str(pair.password or "")
        if not username or not password:
            Logger.error("HuggingChat account is missing a username/email or password.")
            return

        Logger.info("HuggingChat: Auto-login enabled. Attempting login...")
        try:
            await self._click_start_chatting_if_present(wait_ms=6000)
        except Exception:
            pass

        form_ready = await self._wait_for_login_form_or_authenticated(timeout_s=8.0)
        if not form_ready and await self._is_logged_in(allow_chat_input_fallback=False):
            await self._go_to_chat()
            Logger.success("HuggingChat: chat ready.")
            self.ece_mark_used(username)
            return

        if not form_ready and not str(self.page.url or "").startswith(self.LOGIN_URL):
            try:
                await self.page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                form_ready = await self._wait_for_login_form_or_authenticated(timeout_s=8.0)
            except Exception:
                pass

        if not form_ready and await self._is_logged_in(allow_chat_input_fallback=False):
            await self._go_to_chat()
            Logger.success("HuggingChat: chat ready.")
            self.ece_mark_used(username)
            return

        if not form_ready:
            Logger.warning(
                "HuggingChat: login form was not available after Start chatting/Login navigation. "
                "Waiting for manual login..."
            )
            self._background_login_needed("sign-in needs to be finished in the browser")
            await self._wait_until_logged_in(timeout_s=None)
            await self._go_to_chat()
            self.ece_mark_used(username)
            return

        try:
            await self.page.fill("input[name='username']", username)
            await self.page.fill("input[name='password']", password)
            clicked = await self._click_visible_area(
                "button[type='submit']",
                text="Login",
                timeout_ms=5000,
            )
            if not clicked:
                await self.page.locator("button[type='submit']").filter(has_text="Login").first.click()
        except Exception as exc:
            Logger.error(f"HuggingChat: failed to fill credentials/click Login: {exc}")
            return

        self.notify_user(
            "HuggingChat Login",
            "If Hugging Face asks for extra confirmation, please complete it in the browser window.",
            level="info",
        )

        try:
            await asyncio.wait_for(self._wait_until_logged_in(timeout_s=None), timeout=90.0)
        except asyncio.TimeoutError:
            Logger.warning("HuggingChat: login not detected after submit. Waiting for manual completion...")
            self._background_login_needed("sign-in needs to be finished in the browser")
            await self._wait_until_logged_in(timeout_s=None)

        await self._go_to_chat()
        Logger.success("HuggingChat: chat ready.")
        self.ece_mark_used(username)

    async def _go_to_chat(self) -> None:
        if not self.page:
            return
        await self._recover_chat_route_if_needed()
        current_url = str(self.page.url or "")
        if not current_url.startswith(self.CHAT_URL):
            try:
                await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            await self._recover_chat_route_if_needed()
        await self._wait_for_chat_ready(timeout_ms=60000)

    @staticmethod
    def _is_huggingface_home_url(url: str) -> bool:
        normalized = str(url or "").strip().rstrip("/")
        return normalized == "https://huggingface.co"

    @staticmethod
    def _is_huggingface_welcome_url(url: str) -> bool:
        lowered = str(url or "").strip().lower()
        return lowered.startswith("https://huggingface.co/welcome")

    async def _click_start_chatting_if_present(
        self,
        *,
        wait_ms: int = 2500,
        wait_for_resolution: bool = False,
    ) -> bool:
        if not self.page:
            return False
        deadline = time.monotonic() + max(0.0, float(wait_ms) / 1000.0)
        while True:
            try:
                clicked = await self._click_visible_area(
                    "button, a",
                    text="Start chatting",
                    timeout_ms=500,
                )
                if clicked:
                    if wait_for_resolution:
                        await self._wait_after_start_chatting_click(timeout_s=8.0)
                    return True
            except Exception:
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _wait_after_start_chatting_click(self, *, timeout_s: float = 8.0) -> str:
        if not self.page:
            return "missing-page"
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if await self._login_form_visible():
                return "login-form"
            if await self._is_logged_in(allow_chat_input_fallback=False):
                return "authenticated"
            if (not await self._start_chatting_visible()) and await self._chat_input_available():
                return "chat-ready"

            current_url = str(self.page.url or "")
            if not current_url.startswith(self.CHAT_URL):
                return "route-changed"

            if time.monotonic() >= deadline:
                return "timeout"
            await asyncio.sleep(0.2)

    async def _recover_chat_route_if_needed(self) -> None:
        if not self.page:
            return

        current_url = str(self.page.url or "")
        if self._is_huggingface_home_url(current_url) or self._is_huggingface_welcome_url(current_url):
            Logger.info("HuggingChat: redirect detour detected. Returning to chat...")
            try:
                await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(self._post_action_delay_s)
            except Exception:
                pass

        if str(self.page.url or "").startswith(self.CHAT_URL):
            if await self._start_chatting_visible():
                Logger.info("HuggingChat: Start chatting gate detected after redirect recovery.")
                clicked = await self._click_start_chatting_if_present(
                    wait_ms=6000,
                    wait_for_resolution=True,
                )
                if clicked:
                    await asyncio.sleep(self._post_action_delay_s)
            if await self._chat_input_available():
                return

    async def _wait_for_chat_ready(self, timeout_ms: int | None = 60000) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        deadline = None
        if timeout_ms is not None and timeout_ms > 0:
            deadline = time.monotonic() + (float(timeout_ms) / 1000.0)

        last_error: Exception | None = None
        while True:
            await self._recover_chat_route_if_needed()
            try:
                wait_ms = 1000
                if deadline is not None:
                    remaining_ms = int(max(1.0, (deadline - time.monotonic()) * 1000.0))
                    wait_ms = min(wait_ms, remaining_ms)
                await self.page.wait_for_selector(self.CHAT_TEXTAREA_SELECTOR, timeout=wait_ms)
                return
            except Exception as exc:
                last_error = exc

            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("HuggingChat chat input was not ready before timeout.") from last_error

    async def _click_visible_area(
        self,
        selector: str,
        *,
        text: str | None = None,
        contains_text: str | None = None,
        timeout_ms: int = 5000,
        settle_s: float | None = None,
        last: bool = False,
    ) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        last_error: Exception | None = None
        while True:
            try:
                point = await self.page.evaluate(
                    """(args) => {
                        const isVisible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && Number(style.opacity || '1') !== 0;
                        };
                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const exactText = normalize(args.text || '');
                        const containsText = normalize(args.containsText || '');
                        const elements = Array.from(document.querySelectorAll(args.selector || ''));
                        const matches = elements.filter((el) => {
                            if (!isVisible(el)) return false;
                            const text = normalize(el.textContent || '');
                            if (exactText && text !== exactText) return false;
                            if (containsText && !text.includes(containsText)) return false;
                            return true;
                        });
                        const target = args.last ? matches[matches.length - 1] : matches[0];
                        if (!target) return null;
                        target.scrollIntoView({ block: 'center', inline: 'center' });
                        const rect = target.getBoundingClientRect();
                        return {
                            x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                            y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                        };
                    }""",
                    {
                        "selector": selector,
                        "text": text or "",
                        "containsText": contains_text or "",
                        "last": bool(last),
                    },
                )
                if isinstance(point, dict):
                    x = float(point.get("x") or 0)
                    y = float(point.get("y") or 0)
                    if x > 0 and y > 0:
                        await self.page.mouse.click(x, y)
                        await asyncio.sleep(self._post_action_delay_s if settle_s is None else settle_s)
                        return True
            except Exception as exc:
                last_error = exc

            if time.monotonic() >= deadline:
                if last_error:
                    Logger.debug(f"HuggingChat: area click failed for {selector}: {last_error}")
                return False
            await asyncio.sleep(0.15)

    async def _click_dropdown_menu_item_by_text(
        self,
        label: str,
        *,
        contains: bool = False,
        timeout_ms: int = 5000,
    ) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        last_error: Exception | None = None
        while True:
            try:
                point = await self.page.evaluate(
                    """(args) => {
                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const expected = normalize(args.label).toLowerCase();
                        const contains = !!args.contains;
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && Number(style.opacity || '1') !== 0;
                        };
                        const matches = (value) => {
                            const normalized = normalize(value).toLowerCase();
                            if (!normalized || !expected) return false;
                            return contains ? normalized.includes(expected) : normalized === expected;
                        };
                        const candidates = Array.from(
                            document.querySelectorAll("[data-dropdown-menu-item], [role='menuitem'], [role='menuitemcheckbox']")
                        );
                        for (const item of candidates) {
                            if (!isVisible(item)) continue;
                            const elementTexts = Array.from(item.children).map((child) => child.textContent);
                            const nodeTexts = Array.from(item.childNodes).map((node) => node.textContent);
                            if (!matches(item.textContent) && !elementTexts.some(matches) && !nodeTexts.some(matches)) {
                                continue;
                            }
                            item.scrollIntoView({ block: 'center', inline: 'center' });
                            const rect = item.getBoundingClientRect();
                            return {
                                x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                                y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                            };
                        }
                        return null;
                    }""",
                    {"label": label, "contains": bool(contains)},
                )
                if isinstance(point, dict):
                    x = float(point.get("x") or 0)
                    y = float(point.get("y") or 0)
                    if x > 0 and y > 0:
                        await self.page.mouse.click(x, y)
                        await asyncio.sleep(self._post_action_delay_s)
                        return True
            except Exception as exc:
                last_error = exc

            if time.monotonic() >= deadline:
                if last_error:
                    Logger.debug(f"HuggingChat: dropdown item click failed for {label}: {last_error}")
                return False
            await asyncio.sleep(0.15)

    async def _click_dropdown_checkbox_item_by_child_span(
        self,
        label: str,
        *,
        timeout_ms: int = 5000,
    ) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + (max(0, int(timeout_ms)) / 1000.0)
        last_error: Exception | None = None
        while True:
            try:
                point = await self.page.evaluate(
                    """(label) => {
                        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const expected = normalize(label);
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && Number(style.opacity || '1') !== 0;
                        };
                        const candidates = Array.from(
                            document.querySelectorAll("[data-dropdown-menu-checkbox-item], [role='menuitemcheckbox']")
                        );
                        for (const item of candidates) {
                            if (!isVisible(item)) continue;
                            const spans = Array.from(item.children).filter((child) => child.tagName === 'SPAN');
                            if (!spans.some((span) => normalize(span.textContent) === expected)) {
                                continue;
                            }
                            item.scrollIntoView({ block: 'center', inline: 'center' });
                            const rect = item.getBoundingClientRect();
                            return {
                                x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                                y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                            };
                        }
                        return null;
                    }""",
                    label,
                )
                if isinstance(point, dict):
                    x = float(point.get("x") or 0)
                    y = float(point.get("y") or 0)
                    if x > 0 and y > 0:
                        await self.page.mouse.click(x, y)
                        await asyncio.sleep(self._post_action_delay_s)
                        return True
            except Exception as exc:
                last_error = exc

            if time.monotonic() >= deadline:
                if last_error:
                    Logger.debug(f"HuggingChat: checkbox item click failed for {label}: {last_error}")
                return False
            await asyncio.sleep(0.15)

    def _read_cached_models_payload(self) -> dict[str, Any] | None:
        raw = self.cache_manager.read_cache(self.model_cache_key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _write_cached_model_labels(self, labels: list[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for label in labels:
            safe = str(label or "").strip()
            if not safe or safe in seen:
                continue
            seen.add(safe)
            cleaned.append(safe)
        if not cleaned:
            return
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "models": cleaned,
        }
        self.cache_manager.write_cache(
            self.model_cache_key,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )

    def _read_cached_model_labels(self) -> list[str]:
        payload = self._read_cached_models_payload()
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            return []
        labels: list[str] = []
        seen: set[str] = set()
        for raw in raw_models:
            label = str(raw or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
        return labels

    def api_real_model_labels(self) -> list[str]:
        return self._read_cached_model_labels()

    async def refresh_api_model_cache(self) -> bool:
        before = self._read_cached_model_labels()
        await self._ensure_huggingchat_model_list_cached(max_age_s=0.0)
        after = self._read_cached_model_labels()
        if not after:
            Logger.warning("HuggingChat: model cache refresh did not return any models.")
        return bool(after and after != before)

    async def _ensure_huggingchat_model_list_cached(self, *, max_age_s: float = 604800.0) -> None:
        payload = self._read_cached_models_payload()
        if isinstance(payload, dict):
            try:
                updated_at = float(payload.get("updated_at") or 0.0)
            except Exception:
                updated_at = 0.0
            if updated_at > 0 and time.time() - updated_at < max_age_s:
                models = self._read_cached_model_labels()
                if models:
                    return

        labels = await self._fetch_visible_model_labels_from_ui()
        if labels:
            self._write_cached_model_labels(labels)
            Logger.info(f"HuggingChat: cached {len(labels)} model IDs.")
        else:
            cached = self._read_cached_model_labels()
            if cached:
                Logger.warning(
                    "HuggingChat: model list refresh failed; keeping the last cached list."
                )

    async def _fetch_visible_model_labels_from_ui(self) -> list[str]:
        if not self.page:
            return []
        opened = await self._open_model_settings()
        if not opened:
            return []

        labels: list[str] = []
        try:
            await self.page.wait_for_selector("button[data-model-id]", timeout=8000)
            labels = await self.page.evaluate(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button[data-model-id]'));
                    const out = [];
                    const seen = new Set();
                    for (const button of buttons) {
                        const id = String(button.getAttribute('data-model-id') || '').trim();
                        if (!id || seen.has(id)) continue;
                        seen.add(id);
                        out.push(id);
                    }
                    return out;
                }"""
            )
        except Exception:
            labels = []

        try:
            activate = self.page.locator("button[name='Activate model']")
            if await activate.count() > 0:
                await activate.first.click(timeout=3000)
                await asyncio.sleep(self._post_action_delay_s)
        except Exception:
            pass

        return [str(label or "").strip() for label in (labels or []) if str(label or "").strip()]

    def _get_configured_model_label(self) -> str:
        label = self._get_str_setting("model", "Current HuggingChat selection").strip()
        if (
            not label
            or label == "Current HuggingChat selection"
            or label == self.MODEL_LIST_UNAVAILABLE_LABEL
        ):
            return ""
        return self._resolve_model_label_alias(label) or label

    def _get_model_label_for_request(self, model: Any = None) -> str:
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        if not override:
            override = self._resolve_model_label_alias(model)
        return override or self._get_configured_model_label()

    def _resolve_model_label_alias(self, value: Any) -> str:
        wanted = str(value or "").strip()
        if not wanted:
            return ""
        for label in self.api_real_model_labels():
            if self._model_labels_match(label, wanted):
                return label
        return ""

    def _resolve_deepthink_flags(self, model: str) -> tuple[bool, bool]:
        enable_deepthink = self._get_bool_setting("enable_deepthink", False)
        send_deepthink = self._get_bool_setting("send_deepthink", False)

        mode = resolve_behavior_mode(
            model,
            self.provider,
            real_model_labels=self.api_real_model_labels(),
        )
        if mode == MODE_CHAT:
            return False, False
        if mode == MODE_REASONER:
            return True, send_deepthink
        return enable_deepthink, send_deepthink

    @staticmethod
    def _normalize_choice(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        normalized = re.sub(r"[\s_]+", "-", normalized)
        normalized = re.sub(r"-{2,}", "-", normalized)
        return normalized.strip("-")

    def _resolve_thinking_effort(self, deepthink_enabled: bool, overrides: dict[str, Any]) -> str:
        override = overrides.get("thinking_effort")
        if override is None:
            override = overrides.get("huggingchat_thinking_effort")
        value = override if override is not None else self._get_str_setting("thinking_effort", "auto")
        effort = self._normalize_choice(value)
        if not deepthink_enabled:
            return "default"
        if effort in {"", "auto"}:
            return "auto"
        if effort in {"none", "off", "disabled", "false", "0", "default"}:
            return "default"
        if effort in {"low", "medium", "high"}:
            return effort
        return str(value or "").strip()

    @staticmethod
    def _thinking_effort_requests_thinking(effort: str) -> bool:
        return effort in {"low", "medium", "high"}

    def _resolve_inference_provider(self, overrides: dict[str, Any]) -> str:
        override = overrides.get("inference_provider")
        if override is None:
            override = overrides.get("huggingchat_inference_provider")
        value = override if override is not None else self._get_str_setting("inference_provider", "auto")
        provider = str(value or "").strip()
        return provider or "auto"

    def _resolve_request_settings(
        self,
        model: str,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_model = (model or "").strip() or "huggingchat-auto"
        overrides = dict(overrides or {})
        deepthink_enabled, send_deepthink = self._resolve_deepthink_flags(resolved_model)
        settings = {
            "model_label": self._get_model_label_for_request(resolved_model),
            "deepthink_enabled": bool(deepthink_enabled),
            "send_deepthink": bool(send_deepthink),
            "search_enabled": self._get_bool_setting("enable_search", False),
            "send_as_text_file": self._get_bool_setting("send_as_text_file", False),
            "use_system_prompt_field": self._get_bool_setting("use_system_prompt_field", False),
            "paste_leading_system_messages": self._get_bool_setting(
                "paste_leading_system_messages",
                True,
            ),
        }

        for key in (
            "deepthink_enabled",
            "send_deepthink",
            "search_enabled",
            "send_as_text_file",
            "use_system_prompt_field",
            "paste_leading_system_messages",
        ):
            if key in overrides:
                settings[key] = bool(overrides[key])

        effort_override = overrides.get("thinking_effort")
        if effort_override is None:
            effort_override = overrides.get("huggingchat_thinking_effort")
        effort_source = (
            effort_override
            if effort_override is not None
            else self._get_str_setting("thinking_effort", "auto")
        )
        effort_norm = self._normalize_choice(effort_source)
        if (
            "deepthink_enabled" not in overrides
            and self._thinking_effort_requests_thinking(effort_norm)
        ):
            settings["deepthink_enabled"] = True

        settings["thinking_effort"] = self._resolve_thinking_effort(
            bool(settings["deepthink_enabled"]),
            overrides,
        )
        settings["inference_provider"] = self._resolve_inference_provider(overrides)
        return settings

    def _extract_macros_from_text(self, text: str) -> tuple[str, Dict[str, Any]]:
        return extract_macro_overrides(text, macro_actions=COMMON_REQUEST_MACRO_ACTIONS)

    def _strip_macros_from_messages(self, messages: List[Any]) -> tuple[List[Any], Dict[str, Any]]:
        return strip_macros_from_messages(messages, macro_actions=COMMON_REQUEST_MACRO_ACTIONS)

    @staticmethod
    def _message_content_as_text(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("content") or "")
        try:
            return str(getattr(message, "content") or "")
        except Exception:
            return ""

    def _prepare_prompt_payload(
        self,
        messages: Union[str, List[Any]],
        settings: Dict[str, Any],
    ) -> tuple[str, str]:
        system_parts: list[str] = []
        prompt_source: Union[str, List[Any]] = messages
        use_system_field = bool(settings.get("use_system_prompt_field"))
        if (
            use_system_field
            and isinstance(messages, list)
            and bool(settings.get("paste_leading_system_messages"))
        ):
            leading_system, remaining = split_leading_system_messages(messages)
            if leading_system:
                leading_text = "\n\n".join(
                    self._message_content_as_text(msg).strip()
                    for msg in leading_system
                    if self._message_content_as_text(msg).strip()
                )
                if leading_text:
                    system_parts.append(leading_text)
                prompt_source = remaining or messages

        formatted_message = format_request_messages(self.config_manager, prompt_source)
        system_prompt = "\n\n".join(part for part in system_parts if part).strip()
        if not use_system_field:
            return formatted_message, ""
        return formatted_message, system_prompt

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, Any]]:
        raw = self.cache_manager.read_cache(self.clean_regen_state_cache_key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _write_clean_regeneration_state(self, state: Dict[str, Any]) -> None:
        self.cache_manager.write_cache(
            self.clean_regen_state_cache_key,
            json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )

    def _build_cache_state(self, settings: Dict[str, Any], *, system_prompt: str = "") -> Dict[str, Any]:
        return {
            "model_label": str(settings.get("model_label") or ""),
            "inference_provider": str(settings.get("inference_provider") or "auto"),
            "thinking_effort": str(settings.get("thinking_effort") or "auto"),
            "deepthink_enabled": bool(settings.get("deepthink_enabled")),
            "send_deepthink": bool(settings.get("send_deepthink")),
            "search_enabled": bool(settings.get("search_enabled")),
            "send_as_text_file": bool(settings.get("send_as_text_file")),
            "use_system_prompt_field": bool(settings.get("use_system_prompt_field")),
            "system_prompt": str(system_prompt or ""),
        }

    def _parse_conversation_info_from_url(self, url: str) -> Optional[Dict[str, str]]:
        match = self.CONVERSATION_URL_RE.match(str(url or "").strip())
        if not match:
            return None
        conversation_id = str(match.group(1) or "").strip()
        if not conversation_id:
            return None
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"{self.CHAT_URL}/conversation/{conversation_id}",
        }

    async def _get_current_conversation_info(self) -> Optional[Dict[str, str]]:
        if not self.page:
            return None
        return self._parse_conversation_info_from_url(str(self.page.url or ""))

    async def _wait_for_current_conversation_info(
        self,
        timeout_ms: int = 6000,
        poll_interval_s: float = 0.2,
    ) -> Optional[Dict[str, str]]:
        deadline = time.monotonic() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            info = await self._get_current_conversation_info()
            if info is not None:
                return info
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def _open_cached_conversation(self, conversation_url: str) -> bool:
        if not self.page:
            return False
        try:
            await self.page.goto(conversation_url, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            return True
        except Exception as exc:
            Logger.warning(f"Multi-Slot Cache (HuggingChat): failed to open cached chat: {exc}")
            return False

    async def _delete_conversation_by_id(self, conversation_id: str) -> bool:
        normalized_id = str(conversation_id or "").strip()
        if not normalized_id:
            return False
        result = await self._run_browser_request(
            method="DELETE",
            url=f"{self.CHAT_URL}/api/v2/conversations/{normalized_id}",
            use_xhr=True,
            referrer=self.CHAT_URL,
        )
        if bool(result.get("ok")):
            return True
        detail = str(result.get("error") or result.get("text") or "").strip()
        status = int(result.get("status") or 0)
        suffix = f" ({detail[:180]})" if detail else ""
        Logger.warning(
            f"HuggingChat: failed to auto-delete chat {normalized_id} (status={status}){suffix}"
        )
        return False

    async def _auto_delete_current_chat(self) -> bool:
        current_info = await self._get_current_conversation_info()
        if current_info is None:
            Logger.debug("HuggingChat: auto-delete skipped because the current chat ID was not available.")
            return False
        conversation_id = str(current_info.get("conversation_id") or "").strip()
        if not conversation_id:
            return False
        try:
            await self.click_new_chat(source="auto")
            await asyncio.sleep(self._post_action_delay_s)
        except Exception as exc:
            Logger.warning(f"HuggingChat: auto-delete skipped because new chat prep failed: {exc}")
            return False
        if await self._delete_conversation_by_id(conversation_id):
            Logger.info("HuggingChat: auto-deleted the completed chat.")
            return True
        return False

    async def set_sidebar_status(self, open: bool) -> None:
        if not self.page:
            return
        title = "Expand sidebar" if open else "Collapse sidebar"
        try:
            await self._click_visible_area(f"button[title='{title}']", timeout_ms=3000)
        except Exception:
            pass

    async def click_new_chat(self, source: str = "auto") -> None:
        self._raise_if_request_cancelled()
        if not self.page:
            return
        try:
            await self._click_stop_button(timeout_s=0.5)
        except Exception:
            pass
        try:
            await self.set_sidebar_status(True)
            clicked = await self._click_visible_area(
                "a[href='/chat/'], a[href='/chat'], a[href='https://huggingface.co/chat/'], a[href='https://huggingface.co/chat']",
                text="New Chat",
                timeout_ms=5000,
            )
            if clicked:
                await self._wait_for_chat_ready(timeout_ms=60000)
                return
        except Exception as exc:
            Logger.debug(f"HuggingChat: New Chat click failed, falling back to URL: {exc}")

        await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        await self._wait_for_chat_ready(timeout_ms=60000)

    async def set_deepthink_state(self, state: bool) -> None:
        _ = state
        return None

    async def set_search_state(self, state: bool) -> None:
        if not self.page:
            return
        desired = bool(state)
        try:
            state_info = await self.page.evaluate(
                """() => {
                    const manage = document.querySelector("button[title='Manage MCP Servers']");
                    const disable = document.querySelector("button[aria-label='Disable all MCP servers']");
                    if (!manage) return { exact: false, any: !!disable };
                    const text = (manage.textContent || '').toString();
                    const span = manage.querySelector('span:first-child');
                    const imgs = span ? Array.from(span.querySelectorAll('img')) : [];
                    const hasExa = imgs.length === 1 && /exa\\.ai/.test(String(imgs[0].src || ''));
                    const countMatch = text.match(/MCP\\s*\\((\\d+)\\)/i);
                    const mcpCount = countMatch ? Number(countMatch[1]) : 0;
                    return { exact: text.includes('MCP (1)') && hasExa, any: mcpCount > 0 || !!disable };
                }"""
            )
            current = bool(state_info.get("exact")) if isinstance(state_info, dict) else False
            any_enabled = bool(state_info.get("any")) if isinstance(state_info, dict) else False
        except Exception:
            current = False
            any_enabled = False

        if current == desired and (desired or not any_enabled):
            return

        if any_enabled:
            try:
                disabled_clicked = await self._click_visible_area(self.DISABLE_MCP_SELECTOR, timeout_ms=3000)
                if disabled_clicked and not desired:
                    return
            except Exception:
                pass

        if desired:
            try:
                await self._open_attachment_submenu(index=1)
                clicked = await self._click_dropdown_checkbox_item_by_child_span(
                    "Web Search (Exa)",
                    timeout_ms=5000,
                )
                if not clicked:
                    item = self.page.locator(
                        "[data-dropdown-menu-checkbox-item], [role='menuitemcheckbox']"
                    ).filter(
                        has_text="Web Search (Exa)"
                    )
                    await item.first.click(timeout=5000)
                await asyncio.sleep(self._post_action_delay_s)
            except Exception as exc:
                Logger.warning(f"HuggingChat: failed to enable search/MCP server: {exc}")

    async def _open_attachment_submenu(self, *, index: int) -> None:
        if not self.page:
            return
        if await self._hover_attachment_submenu_trigger(index=index):
            return

        clicked = await self._click_visible_area(self.ADD_ATTACHMENT_SELECTOR, timeout_ms=5000, settle_s=0.15)
        if not clicked:
            btn = self.page.locator(self.ADD_ATTACHMENT_SELECTOR)
            await btn.first.click(timeout=5000)
        await asyncio.sleep(0.15)
        await self._hover_attachment_submenu_trigger(index=index)

    async def _hover_attachment_submenu_trigger(self, *, index: int) -> bool:
        if not self.page:
            return False
        try:
            point = await self.page.evaluate(
                """(index) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || '1') !== 0;
                    };
                    const triggers = Array.from(
                        document.querySelectorAll("div[data-dropdown-menu-sub-trigger='']")
                    ).filter(isVisible);
                    const target = triggers[Math.max(0, Number(index) || 0)];
                    if (!target) return null;
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    const rect = target.getBoundingClientRect();
                    return {
                        x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                        y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                    };
                }""",
                int(index),
            )
            if not isinstance(point, dict):
                return False
            x = float(point.get("x") or 0)
            y = float(point.get("y") or 0)
            if x <= 0 or y <= 0:
                return False
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.2)
            return True
        except Exception as exc:
            Logger.debug(f"HuggingChat: attachment submenu hover failed: {exc}")
            return False

    async def upload_file(self, file_spec: Any) -> None:
        await self._upload_file(file_spec)

    async def _upload_file(self, file_spec: Any) -> bool:
        self._check_before_new_input(upload=True)
        if not self.page:
            return False

        if isinstance(file_spec, dict):
            name = str(file_spec.get("name") or "prompt.txt")
            mime_type = str(
                file_spec.get("mimeType") or file_spec.get("mime_type") or "text/plain"
            )
            raw = file_spec.get("buffer")
            if isinstance(raw, bytes):
                data = raw
            elif isinstance(raw, bytearray):
                data = bytes(raw)
            else:
                data = str(raw or "").encode("utf-8")
            upload_spec: Any = {
                "name": name,
                "mimeType": mime_type,
                "buffer": data,
            }
        else:
            path = Path(str(file_spec or ""))
            if not path.exists():
                Logger.warning(f"HuggingChat: upload file was not found: {path}")
                return False
            upload_spec = str(path)

        try:
            await self._open_attachment_submenu(index=0)
            async with self.page.expect_file_chooser(timeout=8000) as fc_info:
                clicked = await self._click_dropdown_menu_item_by_text(
                    "Upload from device",
                    timeout_ms=5000,
                )
                if not clicked:
                    picker = self.page.locator("[data-dropdown-menu-item], [role='menuitem']").filter(
                        has_text=re.compile("upload|file|computer|device", re.I)
                    )
                    await picker.first.click(timeout=5000)
            chooser = await fc_info.value
            await chooser.set_files(upload_spec)
            await asyncio.sleep(self._post_action_delay_s)
            return True
        except Exception as exc:
            Logger.debug(f"HuggingChat: file chooser upload path failed: {exc}")

        try:
            file_input = self.page.locator("input[type='file']")
            if await file_input.count() == 0:
                await self._open_attachment_submenu(index=0)
                await self.page.wait_for_selector("input[type='file']", timeout=4000)
                file_input = self.page.locator("input[type='file']")
            if await file_input.count() == 0:
                Logger.warning("HuggingChat: file input was not found.")
                return False
            await file_input.last.set_input_files(upload_spec)
            await asyncio.sleep(self._post_action_delay_s)
            return True
        except Exception as exc:
            Logger.warning(f"HuggingChat: file upload failed: {exc}")
            return False

    async def enter_message(self, message: str) -> None:
        self._check_before_new_input()
        if not self.page:
            return
        textarea = self.page.locator(self.CHAT_TEXTAREA_SELECTOR)
        if await textarea.count() == 0:
            textarea = self.page.locator("textarea")
        if await textarea.count() == 0:
            Logger.warning("HuggingChat: message textarea not found.")
            return
        await textarea.first.fill(str(message or ""))

    async def send_message(self, timeout: int | None = None) -> bool:
        return await self._send_message(timeout=timeout)

    async def _send_message(self, timeout: int | None = None, arm_event: asyncio.Event | None = None) -> bool:
        if not self.page:
            return False
        if arm_event:
            arm_event.set()

        max_wait_s = 0 if not timeout else max(int(timeout), 0)
        start = time.monotonic()
        last_error: Exception | None = None
        while True:
            self._raise_if_request_cancelled()
            btn = self.page.locator(self.SEND_BUTTON_SELECTOR)
            try:
                if await btn.count() > 0 and await btn.first.is_visible():
                    disabled = await btn.first.get_attribute("disabled")
                    aria_disabled = str(await btn.first.get_attribute("aria-disabled") or "").strip().lower()
                    if disabled is None and aria_disabled != "true" and await btn.first.is_enabled():
                        self._mark_request_sending()
                        clicked = await self._click_visible_area(
                            self.SEND_BUTTON_SELECTOR,
                            timeout_ms=3000,
                        )
                        if not clicked:
                            await btn.first.click(timeout=3000)
                        return True
            except Exception as exc:
                last_error = exc

            if max_wait_s <= 0 or time.monotonic() - start >= max_wait_s:
                break
            await asyncio.sleep(0.1)

        if last_error:
            Logger.warning(f"HuggingChat: failed to click send button: {last_error}")
        else:
            Logger.warning("HuggingChat: send button not found or stayed disabled.")
        return False

    async def _click_regenerate(self, arm_event: asyncio.Event | None = None) -> bool:
        if not self.page:
            return False
        if arm_event:
            arm_event.set()
        buttons = self.page.locator(self.RETRY_BUTTON_SELECTOR)
        try:
            count = await buttons.count()
        except Exception:
            count = 0
        for idx in range(count - 1, -1, -1):
            button = buttons.nth(idx)
            try:
                if await button.is_visible():
                    self._mark_request_sending()
                    clicked = await self._click_visible_area(
                        self.RETRY_BUTTON_SELECTOR,
                        timeout_ms=1000,
                        last=True,
                    )
                    if not clicked:
                        await button.click(timeout=3000)
                    return True
            except Exception:
                continue
        try:
            state = await self.page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || '1') !== 0;
                    };
                    return {
                        url: location.href,
                        buttons: Array.from(document.querySelectorAll('button'))
                            .filter(isVisible)
                            .map((button) => ({
                                title: String(button.getAttribute('title') || ''),
                                ariaLabel: String(button.getAttribute('aria-label') || ''),
                                text: String(button.textContent || '').replace(/\\s+/g, ' ').trim(),
                            }))
                            .filter((item) =>
                                /retry|regenerate/i.test(item.title)
                                || /retry|regenerate/i.test(item.ariaLabel)
                                || /retry|regenerate/i.test(item.text)
                            )
                            .slice(0, 8),
                    };
                }"""
            )
            Logger.debug(
                "HuggingChat: regenerate button not found. "
                f"state={json.dumps(state, ensure_ascii=True, separators=(',', ':'))}"
            )
        except Exception:
            pass
        return False

    async def _click_stop_button(self, timeout_s: float = 8.0) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + max(float(timeout_s or 0.0), 0.0)
        while True:
            for selector in self.STOP_BUTTON_SELECTORS:
                buttons = self.page.locator(selector)
                try:
                    count = await buttons.count()
                except Exception:
                    count = 0
                for idx in range(count):
                    button = buttons.nth(idx)
                    try:
                        if await button.is_visible():
                            await button.click(timeout=1500)
                            await asyncio.sleep(0.1)
                            return True
                    except Exception:
                        continue
            if timeout_s <= 0 or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)

    @staticmethod
    def _canonicalize_model_id(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().lower())

    @classmethod
    def _model_label_basename(cls, value: Any) -> str:
        raw = str(value or "").strip().rstrip("/")
        if "/" in raw:
            raw = raw.rsplit("/", 1)[-1]
        return cls._canonicalize_model_id(raw)

    @classmethod
    def _model_labels_match(cls, left: Any, right: Any) -> bool:
        left_full = cls._canonicalize_model_id(left)
        right_full = cls._canonicalize_model_id(right)
        if not left_full or not right_full:
            return False
        if left_full == right_full:
            return True
        return cls._model_label_basename(left) == cls._model_label_basename(right)

    async def _read_current_model_info(self) -> dict[str, str]:
        if not self.page:
            return {"model": "", "provider": ""}
        try:
            info = await self.page.evaluate(
                """(settingsSelector) => {
                    const links = Array.from(document.querySelectorAll(settingsSelector));
                    let link = links.find((item) => (item.textContent || '').includes('Model:'));
                    if (!link) {
                        const spans = Array.from(document.querySelectorAll('span'));
                        const note = spans.find((item) => (item.textContent || '').trim() === 'Generated content may be inaccurate or false.');
                        const containers = [];
                        for (let node = note; node; node = node.parentElement) {
                            containers.push(node);
                            if (containers.length > 6) break;
                        }
                        for (const node of containers) {
                            let sibling = node.previousElementSibling;
                            while (sibling) {
                                if (sibling.matches && sibling.matches(settingsSelector)) {
                                    link = sibling;
                                    break;
                                }
                                if (sibling.querySelector) {
                                    const nested = sibling.querySelector(settingsSelector);
                                    if (nested) {
                                        link = nested;
                                        break;
                                    }
                                }
                                sibling = sibling.previousElementSibling;
                            }
                            if (link) break;
                        }
                    }
                    if (!link) return { model: '', provider: '' };
                    const text = (link.textContent || '').toString();
                    const model = text.replace(/^\\s*Model:\\s*/, '').trim();
                    const parent = link.parentElement || link;
                    const providerEl = parent.querySelector("span[title^='Provider:']");
                    const providerTitle = providerEl ? String(providerEl.getAttribute('title') || '') : '';
                    return { model, provider: providerTitle.replace(/^Provider:\\s*/, '').trim() };
                }""",
                self.MODEL_SETTINGS_LINK_SELECTOR,
            )
        except Exception:
            info = None
        if not isinstance(info, dict):
            return {"model": "", "provider": ""}
        return {
            "model": str(info.get("model") or "").strip(),
            "provider": str(info.get("provider") or "").strip(),
        }

    async def _open_model_settings(self) -> bool:
        if not self.page:
            return False
        locate_error: Exception | None = None
        try:
            await self._recover_chat_route_if_needed()
            point = await self.page.evaluate(
                """(settingsSelector) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || '1') !== 0;
                    };
                    const pointFor = (el) => {
                        if (!el || !isVisible(el)) return null;
                        el.scrollIntoView({ block: 'center', inline: 'center' });
                        const rect = el.getBoundingClientRect();
                        return {
                            x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                            y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                        };
                    };
                    const links = Array.from(document.querySelectorAll(settingsSelector));
                    let target = links.find((item) => isVisible(item) && (item.textContent || '').includes('Model:'));
                    if (!target) {
                        const spans = Array.from(document.querySelectorAll('span'));
                        const note = spans.find((item) => (item.textContent || '').trim() === 'Generated content may be inaccurate or false.');
                        const containers = [];
                        for (let node = note; node; node = node.parentElement) {
                            containers.push(node);
                            if (containers.length > 6) break;
                        }
                        for (const node of containers) {
                            let sibling = node.previousElementSibling;
                            while (sibling) {
                                if (sibling.matches && sibling.matches(settingsSelector)) {
                                    target = sibling;
                                    break;
                                }
                                if (sibling.querySelector) {
                                    const nested = sibling.querySelector(settingsSelector);
                                    if (nested) {
                                        target = nested;
                                        break;
                                    }
                                }
                                sibling = sibling.previousElementSibling;
                            }
                            if (target) break;
                        }
                    }
                    return pointFor(target);
                }""",
                self.MODEL_SETTINGS_LINK_SELECTOR,
            )
        except Exception as exc:
            locate_error = exc
            Logger.warning(
                "HuggingChat: failed to locate model settings anchor: "
                f"{exc} {await self._model_settings_open_diagnostics(stage='locate-anchor')}"
            )
            point = None

        try:
            clicked = False
            if isinstance(point, dict):
                x = float(point.get("x") or 0)
                y = float(point.get("y") or 0)
                if x > 0 and y > 0:
                    await self.page.mouse.click(x, y)
                    clicked = True
            if not clicked:
                clicked = await self._click_visible_area(
                    self.MODEL_SETTINGS_LINK_SELECTOR,
                    contains_text="Model:",
                    timeout_ms=5000,
                )
            if not clicked:
                reason = f" locate_error={locate_error}" if locate_error else ""
                Logger.warning(
                    "HuggingChat: model settings anchor was not clickable."
                    f"{reason} {await self._model_settings_open_diagnostics(stage='click-anchor')}"
                )
                return False
            await asyncio.sleep(self._post_action_delay_s)
            try:
                await self.page.wait_for_selector("button[data-model-id]", timeout=6000)
            except Exception as exc:
                Logger.warning(
                    "HuggingChat: model settings click did not reveal model buttons: "
                    f"{exc} {await self._model_settings_open_diagnostics(stage='wait-model-buttons')}"
                )
                return False
            return True
        except Exception as exc:
            Logger.warning(
                "HuggingChat: failed to open model settings: "
                f"{exc} {await self._model_settings_open_diagnostics(stage='open-settings')}"
            )
            return False

    async def _model_settings_open_diagnostics(self, *, stage: str) -> str:
        if not self.page:
            return f"diagnostics={{\"stage\":\"{stage}\",\"page\":\"missing\"}}"
        try:
            snapshot = await self.page.evaluate(
                """(args) => {
                    const stage = String(args.stage || '');
                    const settingsSelector = String(args.settingsSelector || '');
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || '1') !== 0;
                    };
                    const brief = (el) => {
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName,
                            href: el.href || '',
                            text: String(el.textContent || '').trim().slice(0, 160),
                            className: String(el.className || '').slice(0, 240),
                            visible: isVisible(el),
                            rect: {
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                            },
                        };
                    };
                    const links = Array.from(document.querySelectorAll(settingsSelector));
                    const spans = Array.from(document.querySelectorAll('span'));
                    const note = spans.find((item) => (item.textContent || '').trim() === 'Generated content may be inaccurate or false.');
                    const noteAncestors = [];
                    for (let node = note; node; node = node.parentElement) {
                        noteAncestors.push(brief(node));
                        if (noteAncestors.length >= 6) break;
                    }
                    const active = document.activeElement;
                    return {
                        stage,
                        url: location.href,
                        readyState: document.readyState,
                        textareaCount: document.querySelectorAll("textarea[placeholder='Ask anything']").length,
                        modelButtonCount: document.querySelectorAll("button[data-model-id]").length,
                        settingsLinkCount: links.length,
                        settingsLinks: links.slice(0, 6).map(brief),
                        noteFound: !!note,
                        noteVisible: isVisible(note),
                        noteAncestors,
                        dialogs: Array.from(document.querySelectorAll('[role="dialog"], dialog')).slice(0, 4).map(brief),
                        activeElement: brief(active),
                    };
                }""",
                {
                    "stage": stage,
                    "settingsSelector": self.MODEL_SETTINGS_LINK_SELECTOR,
                },
            )
            text = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"))
            if len(text) > 4000:
                text = text[:4000] + "...(truncated)"
            return f"diagnostics={text}"
        except Exception as exc:
            return f"diagnostics={{\"stage\":{json.dumps(stage)},\"error\":{json.dumps(str(exc))}}}"

    async def apply_configured_model(self, model: Any = None) -> None:
        settings = self._resolve_request_settings(
            str(model or "").strip() or "huggingchat-auto",
            overrides=self._pending_request_overrides,
        )
        await self._apply_model_settings(settings)

    def _read_applied_model_state_cache(self) -> dict[str, Any] | None:
        raw = self.cache_manager.read_cache(self.model_state_cache_key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        accounts = payload.get("accounts")
        if not isinstance(accounts, dict):
            return None
        state = accounts.get(self._get_multi_slot_cache_account_key())
        return state if isinstance(state, dict) else None

    def _write_applied_model_state_cache(self, state: dict[str, Any]) -> None:
        payload = {"version": 1, "accounts": {}}
        raw = self.cache_manager.read_cache(self.model_state_cache_key)
        if raw:
            try:
                existing = json.loads(raw)
                if isinstance(existing, dict) and isinstance(existing.get("accounts"), dict):
                    payload["accounts"] = dict(existing.get("accounts") or {})
            except Exception:
                pass
        payload["accounts"][self._get_multi_slot_cache_account_key()] = dict(state)
        self.cache_manager.write_cache(
            self.model_state_cache_key,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )

    def _model_state_for_settings(self, settings: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        use_system_prompt_field = bool(settings.get("use_system_prompt_field"))
        return {
            "version": 3,
            "model_label": str(settings.get("model_label") or ""),
            "inference_provider": self._normalize_choice(settings.get("inference_provider") or "auto"),
            "thinking_effort": self._normalize_choice(settings.get("thinking_effort") or "auto"),
            "use_system_prompt_field": use_system_prompt_field,
            "system_prompt_field_empty": not use_system_prompt_field,
            "system_prompt": str(system_prompt or ""),
        }

    async def _apply_model_settings(self, settings: dict[str, Any], *, system_prompt: str = "") -> None:
        if not self.page:
            return

        desired_state = self._model_state_for_settings(settings, system_prompt=system_prompt)
        current_info = await self._read_current_model_info()
        desired_model = str(settings.get("model_label") or "").strip()
        desired_provider = self._normalize_choice(settings.get("inference_provider") or "auto")
        desired_thinking_effort = self._normalize_choice(settings.get("thinking_effort") or "auto")
        provider_value = str(settings.get("inference_provider") or "auto").strip()
        provider_norm = self._normalize_choice(provider_value)
        thinking_value = str(settings.get("thinking_effort") or "auto").strip()
        thinking_norm = self._normalize_choice(thinking_value)
        thinking_requested = thinking_norm not in {"", "auto"}
        cached_state = self._last_applied_model_state or self._read_applied_model_state_cache()
        model_matches = (not desired_model) or self._model_labels_match(
            current_info.get("model"),
            desired_model,
        )
        provider_matches = desired_provider in {"", "auto"} or (
            self._normalize_choice(current_info.get("provider")) == desired_provider
        )
        if cached_state == desired_state:
            thinking_effort_verifiable = desired_thinking_effort in {"", "auto"} or (
                desired_thinking_effort == "default"
                and not bool(settings.get("deepthink_enabled"))
            )
            if model_matches and provider_matches and thinking_effort_verifiable:
                return

        thinking_selected = False
        if thinking_requested:
            Logger.info(f"HuggingChat: applying thinking effort '{thinking_value}'.")
            thinking_selected = await self._select_thinking_effort_value(thinking_value)

        use_system_prompt_field = bool(settings.get("use_system_prompt_field"))
        previous_used_system_prompt_field = bool(
            (cached_state or {}).get("use_system_prompt_field")
        )
        model_needs_change = bool(desired_model) and not model_matches
        provider_needs_change = (
            provider_norm not in {"", "auto"} and not provider_matches
        )
        system_prompt_needs_change = bool(
            use_system_prompt_field or previous_used_system_prompt_field
        )
        needs_model_settings = (
            model_needs_change
            or provider_needs_change
            or system_prompt_needs_change
        )
        if not needs_model_settings:
            if thinking_requested and not thinking_selected:
                Logger.warning(
                    "HuggingChat: thinking effort was not selected. "
                    "The current model may not expose the control or the requested option."
                )
            self._last_applied_model_state = desired_state
            self._write_applied_model_state_cache(desired_state)
            return

        if not await self._open_model_settings():
            if desired_model:
                raise RuntimeError(f"HuggingChat: could not open model settings to select '{desired_model}'.")
            if thinking_requested and not thinking_selected:
                Logger.warning(
                    "HuggingChat: thinking effort was not selected. "
                    "The current model may not expose the control or the requested option."
            )
            return

        model_selected = False
        if desired_model and model_needs_change:
            Logger.info(f"HuggingChat: applying model '{desired_model}'.")
            model_selected = await self._select_model_button(desired_model)
            if not model_selected:
                raise RuntimeError(f"HuggingChat: model '{desired_model}' was not found in the model selector.")

        provider_selected = False
        if provider_needs_change:
            provider_selected = await self._select_dropdown_value(
                "Select inference provider",
                provider_value,
                skip_values={"", "auto"},
            )

        if use_system_prompt_field:
            await self._sync_system_prompt_field(system_prompt)
        elif previous_used_system_prompt_field:
            await self._disable_system_prompt_field()

        try:
            activate = self.page.locator("button[name='Activate model']")
            activated = False
            settings_changed = bool(
                model_selected
                or provider_selected
                or use_system_prompt_field
                or previous_used_system_prompt_field
            )
            if settings_changed and await activate.count() > 0:
                activated = await self._click_visible_area(
                    "button[name='Activate model']",
                    timeout_ms=int(self._model_apply_timeout_s * 1000),
                )
                if not activated:
                    await activate.first.click(timeout=int(self._model_apply_timeout_s * 1000))
                    await asyncio.sleep(self._post_action_delay_s)
                    activated = True
            if model_selected and not activated:
                raise RuntimeError("Activate model button was not found.")
            if provider_selected and not activated:
                Logger.warning(
                    "HuggingChat: inference provider was selected, but the Activate model "
                    "button was not found."
                )
        except Exception as exc:
            raise RuntimeError(f"HuggingChat: failed to activate model/settings: {exc}") from exc

        if model_selected:
            deadline = time.monotonic() + self._model_apply_timeout_s
            confirmed_info: dict[str, str] = {"model": "", "provider": ""}
            while time.monotonic() < deadline:
                confirmed_info = await self._read_current_model_info()
                if self._model_labels_match(confirmed_info.get("model"), desired_model):
                    break
                await asyncio.sleep(0.2)
            else:
                shown = str(confirmed_info.get("model") or "Unknown").strip()
                raise RuntimeError(
                    f"HuggingChat: model switch to '{desired_model}' was not confirmed (still showing '{shown}')."
                )

        if thinking_requested:
            thinking_selected = (
                await self._select_thinking_effort_value(thinking_value)
            ) or thinking_selected
            if not thinking_selected:
                Logger.warning(
                    "HuggingChat: thinking effort was not selected. "
                    "The current model may not expose the control or the requested option."
                )

        self._last_applied_model_state = desired_state
        self._write_applied_model_state_cache(desired_state)

    async def _select_model_button(self, model_id: str) -> bool:
        if not self.page:
            return False
        selected_label = ""
        try:
            selected = await self.page.evaluate(
                    """(desired) => {
                        const normalize = (value) => String(value || '').trim().toLowerCase().replace(/\\s+/g, '');
                        const basename = (value) => {
                            const raw = String(value || '').trim().replace(/\\/+$/g, '');
                            const parts = raw.split('/');
                            return parts[parts.length - 1] || raw;
                        };
                        const desiredNorm = normalize(desired);
                        const desiredBase = normalize(basename(desired));
                        const buttons = Array.from(document.querySelectorAll('button[data-model-id]'));
                        const target = buttons.find((button) => {
                            const id = String(button.getAttribute('data-model-id') || '').trim();
                            const text = String(button.textContent || '').trim();
                            return normalize(id) === desiredNorm
                                || normalize(text) === desiredNorm
                                || normalize(basename(id)) === desiredBase
                                || normalize(basename(text)) === desiredBase;
                        });
                        if (!target) return null;
                        target.scrollIntoView({ block: 'center', inline: 'center' });
                        const rect = target.getBoundingClientRect();
                        return {
                            label: String(target.getAttribute('data-model-id') || target.textContent || '').trim(),
                            x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                            y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                        };
                    }""",
                    model_id,
                )
            if isinstance(selected, dict):
                selected_label = str(selected.get("label") or "")
                x = float(selected.get("x") or 0)
                y = float(selected.get("y") or 0)
                if x > 0 and y > 0:
                    await self.page.mouse.click(x, y)
                    await asyncio.sleep(self._post_action_delay_s)
        except Exception:
            selected_label = ""
        if not selected_label:
            return False

        deadline = time.monotonic() + self._model_apply_timeout_s
        while time.monotonic() < deadline:
            try:
                heading = await self.page.locator("h2.text-base.font-semibold.md\\:text-lg").first.text_content(timeout=1000)
            except Exception:
                heading = ""
            if self._model_labels_match(heading, selected_label) or self._model_labels_match(heading, model_id):
                await asyncio.sleep(self._post_action_delay_s)
                return True
            await asyncio.sleep(0.15)
        return False

    async def _select_dropdown_value(
        self,
        aria_label: str,
        desired: str,
        *,
        skip_values: set[str] | None = None,
        aliases: dict[str, set[str]] | None = None,
    ) -> bool:
        if not self.page:
            return False
        desired_raw = str(desired or "").strip()
        desired_norm = self._normalize_choice(desired_raw)
        if desired_norm in set(skip_values or set()):
            return False
        try:
            opened = await self._click_visible_area(
                f"button[aria-label='{aria_label}']",
                timeout_ms=4000,
                settle_s=0.15,
            )
            if not opened:
                return False
            result = await self.page.evaluate(
                """(args) => {
                    const normalize = (value) => String(value || '').trim().toLowerCase().replace(/[\\s_]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
                    const desired = normalize(args.desired || '');
                    const aliasMap = args.aliases || {};
                    const desiredAliases = new Set([desired, ...((aliasMap[desired] || []).map(normalize))]);
                    const items = Array.from(document.querySelectorAll('div[data-select-item][data-value], div[data-dropdown-menu-item][data-value], [role="option"][data-value], [role="menuitem"][data-value]'));
                    for (const item of items) {
                        const rawValue = String(item.getAttribute('data-value') || '').trim();
                        const text = String(item.textContent || '').trim();
                        const candidates = [normalize(rawValue), normalize(text)];
                        if (candidates.some((candidate) => desiredAliases.has(candidate))) {
                            item.scrollIntoView({ block: 'center', inline: 'center' });
                            const rect = item.getBoundingClientRect();
                            return {
                                x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                                y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                            };
                        }
                    }
                    return null;
                }""",
                {
                    "desired": desired_raw,
                    "aliases": {k: list(v) for k, v in (aliases or {}).items()},
                },
            )
            if isinstance(result, dict):
                await self.page.mouse.click(float(result.get("x") or 0), float(result.get("y") or 0))
                result = True
            if not result and desired_norm not in {"", "auto"}:
                result = await self.page.evaluate(
                    """() => {
                        const normalize = (value) => String(value || '').trim().toLowerCase().replace(/[\\s_]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
                        const items = Array.from(document.querySelectorAll('div[data-select-item][data-value], div[data-dropdown-menu-item][data-value], [role="option"][data-value], [role="menuitem"][data-value]'));
                        const target = items.find((item) => normalize(item.getAttribute('data-value')) === 'auto' || normalize(item.textContent) === 'auto');
                        if (!target) return false;
                        target.scrollIntoView({ block: 'center', inline: 'center' });
                        const rect = target.getBoundingClientRect();
                        return {
                            x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                            y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                        };
                    }"""
                )
                if isinstance(result, dict):
                    await self.page.mouse.click(float(result.get("x") or 0), float(result.get("y") or 0))
                    result = True
            if not result:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
            await asyncio.sleep(self._post_action_delay_s)
            return bool(result)
        except Exception as exc:
            Logger.debug(f"HuggingChat: selector '{aria_label}' skipped: {exc}")
            return False

    async def _select_thinking_effort_value(self, desired: str) -> bool:
        if not self.page:
            return False
        desired_raw = str(desired or "").strip()
        desired_norm = self._normalize_choice(desired_raw)
        if desired_norm in {"", "auto"}:
            return False

        aliases: dict[str, set[str]] = {
            "default": {"off", "none", "disabled", "no-thinking"},
            "off": {"default", "none", "disabled", "no-thinking"},
            "none": {"default", "off", "disabled", "no-thinking"},
        }
        desired_aliases = {
            desired_norm,
            *{self._normalize_choice(alias) for alias in aliases.get(desired_norm, set())},
        }

        def _brief_effort_state(raw: Any) -> str:
            try:
                text = json.dumps(raw, ensure_ascii=True, separators=(",", ":"))
            except Exception:
                text = str(raw)
            if len(text) > 1200:
                text = text[:1200] + "...(truncated)"
            return text

        try:
            trigger_state = await self.page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '')
                        .trim()
                        .toLowerCase()
                        .replace(/[\\s_]+/g, '-')
                        .replace(/-+/g, '-')
                        .replace(/^-|-$/g, '');
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || '1') !== 0;
                    };
                    const isThinkingEffortButton = (button) => {
                        const label = String(button.getAttribute('aria-label') || '').trim().toLowerCase();
                        const text = String(button.textContent || '').replace(/\\s+/g, ' ').trim();
                        return label === 'select thinking effort'
                            || label.includes('thinking effort')
                            || /\\bEffort:\\s*/i.test(text);
                    };
                    const buttons = Array.from(document.querySelectorAll('button')).filter(isThinkingEffortButton);
                    const summaries = buttons.map((button) => {
                        const rect = button.getBoundingClientRect();
                        const text = String(button.textContent || '').replace(/\\s+/g, ' ').trim();
                        const match = text.match(/Effort:\\s*([^\\s]+)/i);
                        return {
                            ariaLabel: String(button.getAttribute('aria-label') || ''),
                            text,
                            current: normalize(match ? match[1] : text),
                            visible: isVisible(button),
                            expanded: String(button.getAttribute('aria-expanded') || ''),
                            state: String(button.getAttribute('data-state') || ''),
                            rect: {
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                            },
                        };
                    });
                    const button = buttons.find(isVisible);
                    if (!button) return { current: '', found: buttons.length, url: location.href, buttons: summaries };
                    const text = String(button.textContent || '').replace(/\\s+/g, ' ').trim();
                    const match = text.match(/Effort:\\s*([^\\s]+)/i);
                    return { current: normalize(match ? match[1] : text), found: buttons.length, url: location.href, buttons: summaries };
                }"""
            )
            current_norm = ""
            if isinstance(trigger_state, dict):
                current_norm = str(trigger_state.get("current") or "")
            if self._normalize_choice(current_norm) in desired_aliases:
                return True

            trigger_point = await self.page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && Number(style.opacity || '1') !== 0;
                    };
                    const isThinkingEffortButton = (button) => {
                        const label = String(button.getAttribute('aria-label') || '').trim().toLowerCase();
                        const text = String(button.textContent || '').replace(/\\s+/g, ' ').trim();
                        return label === 'select thinking effort'
                            || label.includes('thinking effort')
                            || /\\bEffort:\\s*/i.test(text);
                    };
                    const button = Array.from(document.querySelectorAll('button'))
                        .filter(isThinkingEffortButton)
                        .find(isVisible);
                    if (!button) return null;
                    button.scrollIntoView({ block: 'center', inline: 'center' });
                    const rect = button.getBoundingClientRect();
                    return {
                        x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                        y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                    };
                }"""
            )
            if not isinstance(trigger_point, dict):
                Logger.debug(
                    "HuggingChat: thinking effort trigger not found. "
                    f"state={_brief_effort_state(trigger_state)}"
                )
                return False
            await self.page.mouse.click(
                float(trigger_point.get("x") or 0),
                float(trigger_point.get("y") or 0),
            )
            await asyncio.sleep(0.2)

            result: Any = None
            last_menu_state: Any = None
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                result = await self.page.evaluate(
                    """(args) => {
                        const normalize = (value) => String(value || '')
                            .trim()
                            .toLowerCase()
                            .replace(/[\\s_]+/g, '-')
                            .replace(/-+/g, '-')
                            .replace(/^-|-$/g, '');
                        const desiredAliases = new Set((args.desiredAliases || []).map(normalize));
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none'
                                && Number(style.opacity || '1') !== 0;
                        };
                        const brief = (item) => {
                            const spans = Array.from(item.querySelectorAll('span'));
                            const spanTexts = spans.map((span) => span.textContent || '').filter(Boolean);
                            return {
                                text: String(item.textContent || '').replace(/\\s+/g, ' ').trim(),
                                spans: spanTexts.map((text) => String(text || '').trim()),
                                dataValue: String(item.getAttribute('data-value') || ''),
                                selected: !!item.querySelector('svg'),
                                candidates: [
                                    item.getAttribute('data-value') || '',
                                    ...spanTexts,
                                    item.textContent || '',
                                ].map(normalize).filter(Boolean),
                            };
                        };
                        const menus = Array.from(document.querySelectorAll('div[data-dropdown-menu-content]'))
                            .filter(isVisible);
                        const menuState = [];
                        for (const menu of menus) {
                            const items = Array.from(menu.children).filter((item) => item.tagName === 'DIV' && isVisible(item));
                            const summaries = items.map(brief);
                            menuState.push({ itemCount: items.length, items: summaries });
                            for (let i = 0; i < items.length; i += 1) {
                                const candidates = summaries[i].candidates || [];
                                if (!candidates.some((candidate) => desiredAliases.has(candidate))) {
                                    continue;
                                }
                                const item = items[i];
                                item.scrollIntoView({ block: 'center', inline: 'center' });
                                const rect = item.getBoundingClientRect();
                                return {
                                    found: true,
                                    menuState,
                                    x: rect.left + Math.max(1, Math.min(rect.width - 1, rect.width / 2)),
                                    y: rect.top + Math.max(1, Math.min(rect.height - 1, rect.height / 2)),
                                };
                            }
                        }
                        return { found: false, menuState };
                    }""",
                    {"desiredAliases": sorted(desired_aliases)},
                )
                if isinstance(result, dict):
                    last_menu_state = result.get("menuState")
                    if result.get("found"):
                        break
                await asyncio.sleep(0.1)

            if isinstance(result, dict) and result.get("found"):
                await self.page.mouse.click(float(result.get("x") or 0), float(result.get("y") or 0))
                await asyncio.sleep(self._post_action_delay_s)
                return True

            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            Logger.debug(
                f"HuggingChat: thinking effort '{desired_raw}' was not available. "
                f"trigger={_brief_effort_state(trigger_state)} "
                f"menu={_brief_effort_state(last_menu_state)}"
            )
            return False
        except Exception as exc:
            Logger.debug(f"HuggingChat: thinking effort selector skipped: {exc}")
            return False

    async def _sync_system_prompt_field(self, system_prompt: str) -> None:
        if not self.page:
            return
        try:
            switch = self.page.locator("div[aria-roledescription='switch']").first
            if await switch.count() > 0:
                checked = str(await switch.get_attribute("aria-checked") or "").strip().lower()
                if checked != "true":
                    await switch.click(timeout=3000)
                    await asyncio.sleep(self._post_action_delay_s)
            textarea = self.page.locator("textarea[aria-label='Custom system prompt']")
            if await textarea.count() > 0:
                target = textarea.first
                if await target.is_enabled():
                    await target.fill(str(system_prompt or ""), timeout=3000)
                    await asyncio.sleep(self._post_action_delay_s)
                else:
                    Logger.debug(
                        "HuggingChat: custom system prompt field was visible but not editable."
                    )
        except Exception as exc:
            Logger.warning(f"HuggingChat: failed to sync custom system prompt: {exc}")

    async def _disable_system_prompt_field(self) -> None:
        if not self.page:
            return
        try:
            switch = self.page.locator("div[aria-roledescription='switch']").first
            switch_count = await switch.count()
            switch_was_on = False
            if switch_count > 0:
                checked = str(await switch.get_attribute("aria-checked") or "").strip().lower()
                switch_was_on = checked == "true"

            textarea = self.page.locator("textarea[aria-label='Custom system prompt']")
            if switch_was_on and await textarea.count() > 0:
                target = textarea.first
                if await target.is_enabled():
                    await target.fill("", timeout=3000)
                    await asyncio.sleep(self._post_action_delay_s)
                else:
                    Logger.debug(
                        "HuggingChat: custom system prompt field is already disabled; "
                        "skipping clear."
                    )

            if switch_count == 0:
                return
            checked = str(await switch.get_attribute("aria-checked") or "").strip().lower()
            if checked == "true":
                await switch.click(timeout=3000)
                await asyncio.sleep(self._post_action_delay_s)
        except Exception as exc:
            Logger.warning(f"HuggingChat: failed to disable custom system prompt: {exc}")

    async def _try_multi_slot_regeneration(
        self,
        *,
        formatted_message: str,
        multi_slot_state: Dict[str, Any],
        completion_armed: asyncio.Event,
        completion_started: asyncio.Event,
        abort_event: asyncio.Event | None = None,
    ) -> bool:
        account_key = self._get_multi_slot_cache_account_key()
        payload = read_multi_slot_cache_payload(
            self.cache_manager,
            self.multi_slot_cache_key,
            log_label="Multi-Slot Cache (HuggingChat)",
        )
        entry = find_multi_slot_cache_entry(payload, account_key, formatted_message, multi_slot_state)
        if entry is None:
            return False

        current_info = await self._get_current_conversation_info()
        if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
            Logger.info("Multi-Slot Cache (HuggingChat): opening cached conversation for regeneration...")
            if not await self._open_cached_conversation(entry["conversation_url"]):
                return False

        Logger.info("Multi-Slot Cache (HuggingChat): cached prompt match found. Attempting to regenerate...")
        if not await self._click_regenerate(arm_event=completion_armed):
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (HuggingChat)",
            )
            return False

        wait_result = await self._wait_for_event_or_abort(
            completion_started,
            timeout_s=self._completion_request_timeout_s,
            abort_event=abort_event,
        )
        if wait_result == "aborted":
            self._schedule_abort_ui_action(use_stop=False)
            return False
        if wait_result == "timeout":
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (HuggingChat)",
            )
            return False
        return True

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "huggingchat-auto",
        stream: bool = False,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        abort_event: asyncio.Event | None = None,
    ):
        _ = (stream, temperature, top_p, max_tokens)
        if not self.page or not self.context:
            yield f"data: {json.dumps({'error': 'HuggingChat driver is not running.'})}\n\n"
            return

        response_queue: asyncio.Queue = asyncio.Queue()
        completion_armed = asyncio.Event()
        completion_started = asyncio.Event()
        completion_claim_lock = asyncio.Lock()
        completion_claimed = False

        self.abort_requested = False
        self.current_abort_event = abort_event
        self._hchat_request_active = False
        self._hchat_response_activity_seen = False

        try:
            await self._go_to_chat()
            await self.require_english_ui()
        except Exception:
            self.current_abort_event = None
            raise
        self._refresh_quirks()
        resolved_model = (model or "").strip() or "huggingchat-auto"
        self.current_model = resolved_model

        request_overrides = dict(getattr(self, "_pending_request_overrides", {}) or {})
        macros_overrides: Dict[str, Any] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = self._strip_macros_from_messages(message)
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = self._extract_macros_from_text(message)
        request_overrides.update(macros_overrides)

        if macros_overrides:
            Logger.debug(f"HuggingChat macros applied: {macros_overrides}")

        effective_settings = self._resolve_request_settings(resolved_model, overrides=request_overrides)
        formatted_message, system_prompt = self._prepare_prompt_payload(
            message_for_formatting,
            effective_settings,
        )
        self.current_send_deepthink = bool(effective_settings.get("send_deepthink"))

        cache_state = self._build_cache_state(effective_settings, system_prompt=system_prompt)
        self._capture_diagnostics_prompt_snapshot(
            formatted_message,
            system_prompt_text=system_prompt,
            metadata={
                "model": resolved_model,
                "ui_model": str(effective_settings.get("model_label") or ""),
                "inference_provider": str(effective_settings.get("inference_provider") or "auto"),
                "thinking_effort": str(effective_settings.get("thinking_effort") or "auto"),
                "deepthink_enabled": bool(effective_settings.get("deepthink_enabled")),
                "send_deepthink": bool(effective_settings.get("send_deepthink")),
                "search_enabled": bool(effective_settings.get("search_enabled")),
                "send_as_text_file": bool(effective_settings.get("send_as_text_file")),
                "use_system_prompt_field": bool(effective_settings.get("use_system_prompt_field")),
            },
        )

        cdp_session: Any = None
        cdp_listeners_registered = False
        cdp_tasks: set[asyncio.Task] = set()
        request_methods: dict[str, str] = {}
        stream_states: dict[str, dict[str, Any]] = {}
        provider_activity_count = 0

        def request_aborted() -> bool:
            return bool(self.abort_requested or (abort_event and abort_event.is_set()))

        def _new_stream_state(response_status: int = 200) -> dict[str, Any]:
            return {
                "parser": _ConcatenatedJsonEventParser(),
                "answer_accumulator": IncrementalTextAccumulator(),
                "final_answer": "",
                "status_error": "",
                "emitted_finish": False,
                "response_status": response_status,
                "think_filter": None if self.current_send_deepthink else _ThinkTagStripper(),
            }

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"HuggingChat: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"HuggingChat: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        def enqueue_delta(
            state: dict[str, Any],
            text: str,
            finish_reason: str | None = None,
        ) -> None:
            if (not text) and (not finish_reason):
                return
            if finish_reason:
                state["emitted_finish"] = True
            response_queue.put_nowait(
                make_openai_delta_sse(
                    self.current_model or "huggingchat-auto",
                    text,
                    finish_reason=finish_reason,
                )
            )

        def process_event(state: dict[str, Any], event: dict[str, Any]) -> None:
            nonlocal provider_activity_count
            provider_activity_count += 1
            event_type = str(event.get("type") or "").strip()
            if event_type == "stream":
                token = str(event.get("token") or "").replace("\x00", "")
                if not token:
                    return
                think_filter = state.get("think_filter")
                if think_filter is not None:
                    token = think_filter.feed(token)
                if token:
                    state["answer_accumulator"].append(token)
                    enqueue_delta(state, token)
                return
            if event_type == "finalAnswer":
                state["final_answer"] = str(event.get("text") or "")
                return
            if event_type == "status":
                status_text = str(event.get("status") or "")
                if "limit" in status_text.lower() or "quota" in status_text.lower():
                    state["status_error"] = status_text
                return
            if "error" in event:
                state["status_error"] = str(event.get("error") or "HuggingChat request failed.")

        async def finish_stream(
            stream_id: str,
            state: dict[str, Any],
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            if stream_id not in stream_states:
                return

            if not aborted and not encountered_error:
                for event in state["parser"].finish():
                    process_event(state, event)

                think_filter = state.get("think_filter")
                if think_filter is not None:
                    tail = think_filter.finish()
                    if tail:
                        state["answer_accumulator"].append(tail)
                        enqueue_delta(state, tail)

                final_answer = str(state.get("final_answer") or "")
                if (not state["answer_accumulator"].has_text) and final_answer:
                    text = final_answer.replace("\x00", "")
                    if think_filter is not None:
                        fallback_filter = _ThinkTagStripper()
                        text = fallback_filter.feed(text) + fallback_filter.finish()
                    if text:
                        state["answer_accumulator"].append(text)
                        enqueue_delta(state, text)

                response_status = int(state.get("response_status") or 200)
                status_error = str(state.get("status_error") or "")
                if response_status >= 400 and not state["answer_accumulator"].has_text:
                    message = await self._upgrade_required_rate_limit_message(
                        timeout_ms=2500,
                        context=f"HTTP status {response_status}",
                    )
                    if message:
                        await response_queue.put({"error": message})
                    else:
                        await response_queue.put(
                            {"error": f"HuggingChat request failed with status {response_status}."}
                        )
                    encountered_error = True
                elif status_error and not state["answer_accumulator"].has_text:
                    if self._is_rate_limit_reason_text(status_error):
                        self._auto_disable_active_account_after_rate_limit(status_error)
                        status_error = f"HuggingChat rate limit: {status_error}"
                    await response_queue.put({"error": status_error})
                    encountered_error = True
                elif not state["answer_accumulator"].has_text:
                    message = await self._upgrade_required_rate_limit_message(
                        timeout_ms=2500,
                        context="empty assistant response",
                    )
                    if not message:
                        message = (
                            "HuggingChat returned no assistant text. The request may have failed "
                            "in the web UI before the answer stream started."
                        )
                        Logger.warning(message)
                    await response_queue.put({"error": message})
                    encountered_error = True
                elif not state["emitted_finish"]:
                    enqueue_delta(state, "", finish_reason="stop")

            await response_queue.put(None)
            stream_states.pop(stream_id, None)
            request_methods.pop(stream_id, None)
            if not aborted and not encountered_error and not self.abort_requested:
                Logger.success("HuggingChat CDP stream completed.")

        async def feed_stream_chunk(
            stream_id: str,
            state: dict[str, Any],
            data: bytes,
        ) -> None:
            if not data:
                return
            self._hchat_response_activity_seen = True
            if request_aborted():
                await finish_stream(stream_id, state, aborted=True)
                return
            for event in state["parser"].feed(data):
                process_event(state, event)

        async def feed_base64_stream_chunk(
            stream_id: str,
            state: dict[str, Any],
            encoded_data: Any,
        ) -> None:
            if not encoded_data:
                return
            encoded_text = str(encoded_data)
            try:
                data = base64.b64decode(encoded_text, validate=True)
            except Exception:
                data = encoded_text.encode("utf-8", errors="ignore")
            await feed_stream_chunk(stream_id, state, data)

        async def start_cdp_stream(request_id: str, url: str, response_status: int = 200) -> None:
            nonlocal completion_claimed
            if not request_id or not completion_armed.is_set() or not cdp_session:
                return
            async with completion_claim_lock:
                if completion_claimed:
                    return
                completion_claimed = True
                completion_started.set()
                stream_states[request_id] = _new_stream_state(response_status=response_status)

            state = stream_states[request_id]
            Logger.info("Teeing HuggingChat conversation response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message = await self._upgrade_required_rate_limit_message(
                    timeout_ms=2500,
                    context=f"CDP response streaming failure: {exc}",
                )
                if not message:
                    message = f"HuggingChat CDP response streaming failed: {exc}"
                    Logger.error(message)
                await response_queue.put({"error": message})
                await finish_stream(request_id, state, encountered_error=True)
                return

            if isinstance(result, dict):
                await feed_base64_stream_chunk(request_id, state, result.get("bufferedData"))

        async def handle_response_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            response = params.get("response")
            if not request_id or not isinstance(response, dict):
                return
            url = str(response.get("url") or "")
            if not self.CONVERSATION_ROUTE_RE.search(url):
                return
            method = request_methods.get(request_id, "").upper()
            if method and method != "POST":
                return
            try:
                response_status = int(response.get("status") or 200)
            except Exception:
                response_status = 200
            await start_cdp_stream(request_id, url, response_status=response_status)

        async def handle_data_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            state = stream_states.get(request_id)
            if not state:
                return
            await feed_base64_stream_chunk(request_id, state, params.get("data"))

        async def handle_loading_finished(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request_methods.pop(request_id, None)
            state = stream_states.get(request_id)
            if state:
                await finish_stream(request_id, state)

        async def handle_loading_failed(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request_methods.pop(request_id, None)
            state = stream_states.get(request_id)
            if not state:
                return
            if request_aborted():
                await finish_stream(request_id, state, aborted=True)
                return
            error_text = str(params.get("errorText") or "network loading failed").strip()
            has_answer = bool(
                state["answer_accumulator"].has_text or str(state.get("final_answer") or "")
            )
            if "ERR_ABORTED" in error_text.upper() and has_answer:
                Logger.debug(
                    "HuggingChat CDP stream ended with net::ERR_ABORTED after "
                    "answer data arrived; treating it as complete."
                )
                await finish_stream(request_id, state)
                return
            message = await self._upgrade_required_rate_limit_message(
                timeout_ms=2500,
                context=f"CDP stream failure: {error_text}",
            )
            if not message:
                message = f"HuggingChat CDP stream failed: {error_text}"
                Logger.error(message)
            await response_queue.put({"error": message})
            await finish_stream(request_id, state, encountered_error=True)

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

        self._hchat_request_active = True

        try:
            if request_aborted():
                self._schedule_abort_ui_action(use_stop=False)
                return

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
                message = f"HuggingChat CDP setup failed: {exc}"
                Logger.error(message)
                yield f"data: {json.dumps({'error': message})}\n\n"
                return

            clean_regeneration = self._get_bool_setting("clean_regeneration", False)
            multi_slot_cache_enabled = bool(
                clean_regeneration and self._get_bool_setting("multi_slot_cache", False)
            )
            auto_delete_requested = self._get_bool_setting("auto_delete_chats", False)
            auto_delete_enabled = bool(auto_delete_requested and (not clean_regeneration))
            if auto_delete_requested and clean_regeneration:
                Logger.warning(
                    "HuggingChat: Delete Chat After Reply is skipped because Reuse Matching Chat is enabled."
                )

            regenerated = False
            current_cache_matched = False
            should_record_multi_slot = False

            if clean_regeneration:
                last_message = self.cache_manager.read_cache(self.clean_regen_message_cache_key)
                last_state = self._read_clean_regeneration_state()
                if last_message == formatted_message and last_state == cache_state:
                    current_cache_matched = True
                    Logger.info(
                        "Clean Regeneration (HuggingChat): Message and settings match cache. Attempting to regenerate..."
                    )
                    regeneration_target = await self._get_current_conversation_info()
                    await self._apply_model_settings(effective_settings, system_prompt=system_prompt)
                    await self.set_search_state(bool(effective_settings.get("search_enabled")))
                    if regeneration_target is not None:
                        current_info = await self._get_current_conversation_info()
                        if (
                            current_info is None
                            or current_info.get("conversation_id") != regeneration_target.get("conversation_id")
                        ):
                            Logger.info(
                                "Clean Regeneration (HuggingChat): returning to the cached "
                                "conversation before pressing Retry..."
                            )
                            await self._open_cached_conversation(
                                regeneration_target["conversation_url"]
                            )
                    await asyncio.sleep(self._post_action_delay_s)
                    if request_aborted():
                        self._schedule_abort_ui_action(use_stop=False)
                        return
                    if await self._click_regenerate(arm_event=completion_armed):
                        wait_result = await self._wait_for_event_or_abort(
                            completion_started,
                            timeout_s=self._completion_request_timeout_s,
                            abort_event=abort_event,
                        )
                        if wait_result == "aborted":
                            self._schedule_abort_ui_action(use_stop=False)
                            return
                        if wait_result == "timeout":
                            Logger.warning(
                                "Clean Regeneration (HuggingChat): completion request not observed. Falling back to new chat."
                            )
                        else:
                            regenerated = True
                            self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                            self._write_clean_regeneration_state(cache_state)

            if (
                (not regenerated)
                and multi_slot_cache_enabled
                and (not current_cache_matched)
            ):
                await self._apply_model_settings(effective_settings, system_prompt=system_prompt)
                await self.set_search_state(bool(effective_settings.get("search_enabled")))
                if request_aborted():
                    self._schedule_abort_ui_action(use_stop=False)
                    return
                regenerated = await self._try_multi_slot_regeneration(
                    formatted_message=formatted_message,
                    multi_slot_state=cache_state,
                    completion_armed=completion_armed,
                    completion_started=completion_started,
                    abort_event=abort_event,
                )
                if request_aborted():
                    self._schedule_abort_ui_action(use_stop=False)
                    return
                if regenerated:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(cache_state)

            if not regenerated:
                Logger.info("HuggingChat: preparing new chat session...")
                await self.click_new_chat(source="auto")
                await asyncio.sleep(self._post_action_delay_s)
                if request_aborted():
                    self._schedule_abort_ui_action(use_stop=False)
                    return
                await self._apply_model_settings(effective_settings, system_prompt=system_prompt)
                await self.set_search_state(bool(effective_settings.get("search_enabled")))
                await asyncio.sleep(self._post_action_delay_s)
                if request_aborted():
                    self._schedule_abort_ui_action(use_stop=False)
                    return

                if bool(effective_settings.get("send_as_text_file")):
                    Logger.info("HuggingChat: sending message as text file...")
                    uploaded = await self._upload_file(build_prompt_text_file_payload(formatted_message))
                    if request_aborted():
                        self._schedule_abort_ui_action(use_stop=False)
                        return
                    if uploaded:
                        settle_delay = float(
                            getattr(
                                self,
                                "_file_upload_settle_delay_s",
                                self.FILE_UPLOAD_SETTLE_DELAY_S,
                            )
                        )
                        if settle_delay > 0:
                            Logger.debug(
                                f"HuggingChat: waiting {settle_delay:.1f}s for file upload to settle..."
                            )
                            await asyncio.sleep(settle_delay)
                            if request_aborted():
                                self._schedule_abort_ui_action(use_stop=False)
                                return
                        file_message = self._get_str_setting(
                            "text_file_message",
                            "Please read the attached file and respond to it.",
                        ).strip()
                        if not file_message:
                            file_message = "Please read the attached file and respond to it."
                        await self.enter_message(file_message)
                        await asyncio.sleep(self._post_action_delay_s)
                        if request_aborted():
                            self._schedule_abort_ui_action(use_stop=False)
                            return
                        upload_timeout = int(self._get_float_setting("file_upload_timeout", 20.0, minimum=1.0))
                        Logger.info("HuggingChat: sending request...")
                        sent = await self._send_message(timeout=upload_timeout, arm_event=completion_armed)
                        if not sent:
                            yield f"data: {json.dumps({'error': 'HuggingChat: send button not found or stayed disabled.'})}\n\n"
                            return
                    else:
                        Logger.warning("HuggingChat: falling back to pasted text for this request.")
                        await self.enter_message(formatted_message)
                        await asyncio.sleep(self._post_action_delay_s)
                        if request_aborted():
                            self._schedule_abort_ui_action(use_stop=False)
                            return
                        msg_timeout = int(self._get_float_setting("message_send_timeout", 8.0, minimum=1.0))
                        Logger.info("HuggingChat: sending request...")
                        sent = await self._send_message(timeout=msg_timeout, arm_event=completion_armed)
                        if not sent:
                            yield f"data: {json.dumps({'error': 'HuggingChat: send button not found or stayed disabled.'})}\n\n"
                            return
                else:
                    await self.enter_message(formatted_message)
                    await asyncio.sleep(self._post_action_delay_s)
                    if request_aborted():
                        self._schedule_abort_ui_action(use_stop=False)
                        return
                    msg_timeout = int(self._get_float_setting("message_send_timeout", 8.0, minimum=1.0))
                    Logger.info("HuggingChat: sending request...")
                    sent = await self._send_message(timeout=msg_timeout, arm_event=completion_armed)
                    if not sent:
                        yield f"data: {json.dumps({'error': 'HuggingChat: send button not found or stayed disabled.'})}\n\n"
                        return

                if request_aborted():
                    self._schedule_abort_ui_action(use_stop=self._hchat_response_activity_seen)
                    return

                if clean_regeneration:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(cache_state)
                    should_record_multi_slot = bool(multi_slot_cache_enabled)

            if not completion_started.is_set():
                wait_result = await self._wait_for_event_or_abort(
                    completion_started,
                    timeout_s=self._completion_request_timeout_s,
                    abort_event=abort_event,
                )
                if wait_result == "aborted":
                    self._schedule_abort_ui_action(use_stop=False)
                    return
                if wait_result == "timeout":
                    message = await self._upgrade_required_rate_limit_message(
                        timeout_ms=2500,
                        context="completion request observation timeout",
                    )
                    if not message:
                        message = "HuggingChat: completion request not observed"
                        Logger.error(message)
                    yield f"data: {json.dumps({'error': message})}\n\n"
                    return

            stream_had_error = False
            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self._first_chunk_timeout_s,
                idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                on_timeout=lambda: self._click_stop_button(timeout_s=4.0),
                activity_counter=lambda: int(provider_activity_count),
            ):
                if isinstance(item, dict) and "error" in item:
                    stream_had_error = True
                    error_text = str(item.get("error") or "")
                    if not self._is_rate_limit_reason_text(error_text):
                        rate_limit_message = await self._upgrade_required_rate_limit_message(
                            timeout_ms=1500,
                            context=f"response queue error: {error_text}",
                        )
                        if rate_limit_message:
                            item = {"error": rate_limit_message}
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                yield item

            if request_aborted():
                self._schedule_abort_ui_action(use_stop=self._hchat_response_activity_seen)
                return

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
                        log_label="Multi-Slot Cache (HuggingChat)",
                    )

            if (
                auto_delete_enabled
                and (not stream_had_error)
                and (not self.abort_requested)
                and not (abort_event and abort_event.is_set())
            ):
                await self._auto_delete_current_chat()
        finally:
            self.current_abort_event = None
            self.abort_requested = False
            self.current_model = None
            self.current_send_deepthink = None
            self._hchat_request_active = False
            self._hchat_response_activity_seen = False
            self._pending_request_overrides = {}
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
                    Logger.debug(f"HuggingChat: CDP detach failed: {exc}")
