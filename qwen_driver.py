import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
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


QWEN_REQUEST_MACRO_ACTIONS: Dict[str, tuple[str, Any]] = {
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


class _QwenStreamConsumer:
    """Convert Qwen's raw SSE bytes into OpenAI-style stream chunks."""

    def __init__(
        self,
        *,
        driver: "QwenLMDriver",
        response_queue: asyncio.Queue,
        count_tokens_enabled: bool,
    ) -> None:
        self.driver = driver
        self.response_queue = response_queue
        self.count_tokens_enabled = count_tokens_enabled
        self.text_buffer = bytearray()
        self.text_buffer_pos = 0
        self.thinking_emitted = IncrementalTextAccumulator()
        self.answer_started = False
        self.openai_usage: dict[str, Any] | None = None
        self.openai_usage_emitted = False
        self.openai_finish_emitted = False
        self.encountered_error = False
        self.finished = False

    @staticmethod
    def _normalize_openai_usage(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        def _to_int(value: Any) -> int | None:
            try:
                return int(value)
            except Exception:
                return None

        prompt_tokens = _to_int(raw.get("input_tokens"))
        completion_tokens = _to_int(raw.get("output_tokens"))
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

        prompt_details = raw.get("input_tokens_details")
        if isinstance(prompt_details, dict):
            usage["prompt_tokens_details"] = prompt_details

        completion_details = raw.get("output_tokens_details")
        if isinstance(completion_details, dict):
            usage["completion_tokens_details"] = completion_details

        return usage

    def _enqueue_openai_delta(self, content: str, finish_reason: str | None = None) -> None:
        if (not content) and (not finish_reason):
            return
        model_name = self.driver.current_model or "qwen-auto"
        self.response_queue.put_nowait(
            make_openai_delta_sse(
                model_name,
                content,
                finish_reason=finish_reason,
            )
        )

    def _enqueue_openai_usage(self, usage: dict[str, Any]) -> None:
        if self.openai_usage_emitted:
            return
        model_name = self.driver.current_model or "qwen-auto"
        self.response_queue.put_nowait(make_openai_usage_sse(model_name, usage))
        self.openai_usage_emitted = True

    def _process_sse_line(self, line: str) -> None:
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

        error_message = self.driver._qwen_provider_error_message(payload)
        if error_message:
            Logger.warning(error_message)
            self.encountered_error = True
            self.response_queue.put_nowait({"error": error_message})
            return

        if self.count_tokens_enabled:
            normalized = self._normalize_openai_usage(payload.get("usage"))
            if normalized:
                self.openai_usage = normalized

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return

        choice0 = choices[0]
        if not isinstance(choice0, dict):
            return

        delta = choice0.get("delta")
        if not isinstance(delta, dict):
            return

        phase = str(delta.get("phase") or "").strip().lower()
        role = str(delta.get("role") or "").strip().lower()
        status = str(delta.get("status") or "").strip().lower()

        if (
            phase == "web_search"
            or role == "function"
            or delta.get("function_call") is not None
            or delta.get("tool_calls") is not None
        ):
            # Ignore tool-call chatter + raw search results for stability.
            return

        if phase == "thinking_summary":
            if not self.driver.current_send_deepthink:
                return
            summary_text = self.driver._extract_qwen_thinking_summary_text(delta)
            if not summary_text:
                return

            if not self.driver.thinking_active:
                self._enqueue_openai_delta("<think>")
                self.driver.thinking_active = True

            missing = self.thinking_emitted.missing_suffix(summary_text)
            if (
                self.thinking_emitted.has_text
                and missing
                and missing == summary_text
                and not missing.startswith(("\n", "\r"))
            ):
                missing = "\n\n" + missing
            if missing:
                self._enqueue_openai_delta(missing)
                self.thinking_emitted.append(missing)
            return

        if phase == "answer":
            if self.driver.thinking_active and self.driver.current_send_deepthink and not self.answer_started:
                self._enqueue_openai_delta("</think>")
                self.driver.thinking_active = False

            content = delta.get("content")
            if isinstance(content, str) and content:
                self._enqueue_openai_delta(content)
                self.answer_started = True

            if status == "finished":
                if not self.openai_finish_emitted:
                    if self.driver.thinking_active and self.driver.current_send_deepthink:
                        self._enqueue_openai_delta("</think>")
                        self.driver.thinking_active = False
                    self._enqueue_openai_delta("", finish_reason="stop")
                    self.openai_finish_emitted = True
                    if self.count_tokens_enabled and self.openai_usage:
                        self._enqueue_openai_usage(self.openai_usage)

    def feed_bytes(self, chunk: bytes) -> None:
        if self.finished or self.encountered_error or not chunk:
            return

        self.text_buffer.extend(chunk)

        while True:
            nl = self.text_buffer.find(b"\n", self.text_buffer_pos)
            if nl == -1:
                break
            raw_line = self.text_buffer[self.text_buffer_pos:nl]
            self.text_buffer_pos = nl + 1
            try:
                line = raw_line.decode("utf-8", errors="ignore")
            except Exception:
                continue
            self._process_sse_line(line)
            if self.encountered_error:
                break

        if self.text_buffer_pos > 8192:
            del self.text_buffer[: self.text_buffer_pos]
            self.text_buffer_pos = 0

    def finish(self) -> None:
        if self.finished:
            return

        self.finished = True
        tail = bytes(self.text_buffer[self.text_buffer_pos :])
        if tail.strip() and not self.encountered_error:
            self._process_sse_line(tail.decode("utf-8", errors="ignore"))
        self.text_buffer.clear()
        self.text_buffer_pos = 0


class QwenLMDriver(BaseDriver):
    REQUEST_CAPTURE_MODE_REPLAY = "replay"
    REQUEST_CAPTURE_MODE_CDP_TEEING = "cdp_teeing"
    CHAT_URL = "https://chat.qwen.ai/"
    AUTH_URL = "https://chat.qwen.ai/auth"
    SETTINGS_URL = "https://chat.qwen.ai/api/v2/users/user/settings"
    SETTINGS_UPDATE_URL = "https://chat.qwen.ai/api/v2/users/user/settings/update"
    CONVERSATION_URL_RE = re.compile(r"^https://chat\.qwen\.ai/c/([^/?#]+)", re.IGNORECASE)
    COMPLETION_REQUEST_TIMEOUT_S = 150.0
    INTERCEPT_FIRST_CHUNK_TIMEOUT_S = 150.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    DATA_INSPECTION_FAILED_CODE = "data_inspection_failed"
    DATA_INSPECTION_FAILED_MESSAGE = (
        "QwenLM data_inspection_failed: Qwen censored this request (sadly :<), "
        "so the message has been aborted."
    )
    COMPLETIONS_URL_PATH = "/api/v2/chat/completions"

    COMPLETIONS_ROUTE_GLOB = "**/api/v2/chat/completions*"

    CHAT_TEXTAREA_SELECTOR = "textarea.message-input-textarea"
    SEND_BUTTON_SELECTOR = "div.chat-prompt-send-button button"
    FILE_INPUT_SELECTOR = "input#filesUpload"

    SIDEBAR_SELECTOR = "div.sidebar"
    SIDEBAR_OPEN_BUTTON_SELECTOR = "div.sidebar-side-fold-container-open"
    SIDEBAR_CLOSE_BUTTON_SELECTOR = "button.sidebar-toggle-button"
    NEW_CHAT_BUTTON_SELECTOR = "div.sidebar-entry-fixed-list-content"
    NEW_CHAT_LOOKUP_TIMEOUT_MS = 5000
    NEW_CHAT_LOOKUP_POLL_INTERVAL_S = 0.15

    THINKING_TRIGGER_SELECTOR = "span.ant-select-selection-item:has(div.qwen-select-thinking-label)"
    THINKING_TRIGGER_LABEL_SELECTOR = "span.qwen-select-thinking-label-text"
    THINKING_OPTIONS_SELECTOR = "div.rc-virtual-list-holder-inner"
    THINKING_OPTION_LABEL_SELECTOR = "span.qwen-select-option-selected-label"
    THINKING_OPTION_SELECTOR = "div.ant-select-item-option"

    SEARCH_MODE_CONTAINER_SELECTOR = "div.mode-select-current-mode"
    SEARCH_CLOSE_SELECTOR = "span.mode-select-current-mode-close"
    SEARCH_ENABLE_ANCHOR = "a2ty_o01.29997169.0.i44.3d4d5171h8MbwS"
    SEARCH_ENABLED_ANCHOR = "a2ty_o01.29997169.0.i49.3d4d5171h8MbwS"
    MODE_SELECT_TRIGGER_SELECTOR = "div.mode-select"
    MODE_SELECT_TRIGGER_OPEN_SELECTOR = "div.mode-select-open"
    MODE_SELECT_TRIGGER_ANY_SELECTOR = "div.mode-select-open, div.mode-select"
    MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR = "ul.ant-dropdown-menu-root.qwen-dropdown-menu"
    MODE_SELECT_COMMON_SUBMENU_SELECTOR = (
        "li.ant-dropdown-menu-submenu.ant-dropdown-menu-submenu-vertical.mode-select-common-item"
    )
    MODE_SELECT_MENU_ITEM_SELECTOR = "li[data-menu-id]"
    MODE_SELECT_TOOLS_SWITCH_SELECTOR = "div[class*='qwen-chat-ui-packages-design-switch-size-small']"

    MODEL_SELECTOR_TEXT_SELECTOR = "[class*='model-selector-text']"
    MODEL_SELECTOR_POPUP_SELECTOR = "div[class*='model-selector-popup']"

    MODEL_LABELS: List[str] = [
        "Qwen3.8-Max",
        "Qwen3.8-Omni-Flash",
        "Qwen3.7-Plus",
        "Qwen3.7-Max",
        "Qwen3.6-Plus",
        "Qwen3.5-Plus",
    ]

    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.QWEN_LM)
        self.cache_manager = CacheManager()

        self.current_model: Optional[str] = None
        self.current_send_deepthink: Optional[bool] = None
        self.current_tools_enabled: Optional[bool] = None
        self._tools_disabled_for_request = False
        self.thinking_active = False

        self.clean_regen_message_cache_key = "qwen_last_message.txt"
        self.clean_regen_state_cache_key = "qwen_last_message_state.json"
        self.multi_slot_cache_key = "qwen_multi_slot_cache.json"

        # Best-effort provider settings enforcement (once per session, can be retried)
        self._rp_settings_last_attempt_ts: float = 0.0
        self._abort_ui_task: asyncio.Task | None = None
        self._refresh_quirks()

    def _refresh_quirks(self) -> None:
        """Read configurable timing knobs from QwenLM Behavior settings."""
        try:
            completion_request_timeout = float(
                self.config_manager.get_setting("qwen_behavior", "completion_request_timeout")
                or self.COMPLETION_REQUEST_TIMEOUT_S
            )
        except Exception:
            completion_request_timeout = self.COMPLETION_REQUEST_TIMEOUT_S
        try:
            first_chunk_timeout = float(
                self.config_manager.get_setting("qwen_behavior", "first_chunk_timeout")
                or self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S
            )
        except Exception:
            first_chunk_timeout = self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S

        self._completion_request_timeout_s = max(completion_request_timeout, 5.0)
        self._first_chunk_timeout_s = max(first_chunk_timeout, 5.0)

    def _get_request_capture_mode(self) -> str:
        try:
            mode = str(
                self.config_manager.get_setting("qwen_behavior", "request_capture_mode")
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

        return parsed.path.rstrip("/") == cls.COMPLETIONS_URL_PATH

    def get_start_url(self) -> str:
        return self.CHAT_URL

    @classmethod
    def _qwen_provider_error_message(cls, payload: Any) -> str | None:
        """Return an IntenseRP-facing message for terminal Qwen stream errors."""
        if not isinstance(payload, dict):
            return None

        error = payload.get("error")
        if not isinstance(error, dict):
            return None

        code = str(error.get("code") or "").strip().lower()
        if code == cls.DATA_INSPECTION_FAILED_CODE:
            return cls.DATA_INSPECTION_FAILED_MESSAGE

        return None

    async def after_start(self, status_callback: Optional[Callable[[str], None]] = None) -> None:
        await self.check_ui_language(status_callback=status_callback)
        clear_clean_regeneration_cache(
            self.cache_manager,
            self.clean_regen_message_cache_key,
            self.clean_regen_state_cache_key,
        )

        # Qwen has a few RP-unfriendly settings that can silently change how prompts are sent
        # we send them settings update requests to clear them up
        try:
            await self._wait_for_chat_ready(timeout_ms=60000)
            changed = await self._ensure_rp_friendly_settings()
            if changed and self.page:
                Logger.info("QwenLM: reloading chat to apply updated settings...")
                try:
                    await self.page.reload(wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    Logger.warning(f"QwenLM: reload after settings update failed: {e}")
                else:
                    await self._wait_for_chat_ready(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"QwenLM: settings enforcement skipped/failed: {e}")

    async def cleanup_background_tasks(self) -> None:
        await self._cancel_task(
            self._abort_ui_task,
            label="stopping QwenLM abort UI task",
        )
        self._abort_ui_task = None

    def request_abort(self) -> None:
        super().request_abort()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        try:
            existing = getattr(self, "_abort_ui_task", None)
            if existing and (not existing.done()):
                return
        except Exception:
            pass

        try:
            self._abort_ui_task = loop.create_task(self._abort_generation_ui())
        except Exception:
            pass

    async def abort_generation(self) -> None:
        Logger.info("QwenLM: abort generation requested...")
        self.abort_requested = True
        if self.current_abort_event:
            try:
                self.current_abort_event.set()
            except Exception:
                pass
        await self._click_stop_button(timeout_s=12.0)

    async def _abort_generation_ui(self) -> None:
        try:
            await self._click_stop_button(timeout_s=12.0)
        except Exception as e:
            Logger.debug(f"QwenLM: abort UI click failed: {e}")

    async def _click_stop_button(self, timeout_s: float = 12.0) -> bool:
        if not self.page:
            return False

        deadline = time.time() + max(float(timeout_s or 0.0), 0.0)
        container = self.page.locator("div.chat-prompt-send-button")
        buttons = container.locator("button")

        while True:
            if timeout_s and (time.time() >= deadline):
                Logger.warning("QwenLM: stop button did not become clickable before timeout.")
                return False

            count = 0
            try:
                count = await buttons.count()
            except Exception:
                count = 0

            any_visible = False
            clicked = False
            stop_present = False

            for idx in range(count - 1, -1, -1):
                btn = buttons.nth(idx)
                try:
                    if not await btn.is_visible():
                        continue
                except Exception:
                    continue

                any_visible = True

                cls = ""
                try:
                    cls = str(await btn.get_attribute("class") or "")
                except Exception:
                    cls = ""

                is_stop = "stop-button" in cls
                if not is_stop:
                    # If it isn't a stop button anymore, generation likely finished
                    return False

                stop_present = True

                disabled_attr = None
                aria_disabled = ""
                try:
                    disabled_attr = await btn.get_attribute("disabled")
                except Exception:
                    disabled_attr = None
                try:
                    aria_disabled = str(await btn.get_attribute("aria-disabled") or "").strip().lower()
                except Exception:
                    aria_disabled = ""

                is_enabled = True
                try:
                    is_enabled = await btn.is_enabled()
                except Exception:
                    is_enabled = True

                if disabled_attr is not None or aria_disabled == "true" or (not is_enabled):
                    # Stop exists but isn't clickable yet. Keep waiting
                    break

                try:
                    await btn.click(timeout=2000)
                    clicked = True
                    break
                except Exception:
                    try:
                        await btn.evaluate("el => el.click()")
                        clicked = True
                        break
                    except Exception:
                        break

            if clicked:
                return True

            if (count == 0) or (not any_visible):
                # No button visible - treat as finished/not generating
                return False

            if not stop_present:
                return False

            await asyncio.sleep(0.1)

    async def _wait_for_chat_ready(self, timeout_ms: int | None = 60000) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        await self.page.wait_for_selector(self.CHAT_TEXTAREA_SELECTOR, timeout=timeout_ms or 0)

    async def _has_token_cookie(self) -> bool:
        context = getattr(self, "context", None)
        if not context:
            return False

        try:
            cookies = await context.cookies()
        except Exception:
            return False

        for cookie in cookies:
            try:
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                domain = str(cookie.get("domain") or "")
            except Exception:
                continue
            if name == "token" and value and ("qwen.ai" in domain):
                return True

        return False

    async def login(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        try:
            if await self._has_token_cookie():
                Logger.info("QwenLM: already signed in.")
                self._mark_active_ece_pair_used()
                return
        except Exception:
            pass

        auto_login = False
        try:
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        except Exception:
            auto_login = False

        # Always land on the auth page when we need to sign in
        try:
            await self.page.goto(self.AUTH_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass

        if not auto_login:
            Logger.info("QwenLM: Auto Login disabled. Waiting for manual login...")
            self.notify_user(
                "QwenLM Login",
                "Please log in to QwenLM in the browser window, then come back here.",
                level="info",
            )
            # Wait indefinitely until we can see a session cookie and the chat UI
            while True:
                if await self._has_token_cookie():
                    break
                await asyncio.sleep(0.5)
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            Logger.success("QwenLM: login detected.")
            self._mark_active_ece_pair_used()
            return

        pair = self.ece_active_pair()
        if not pair:
            Logger.warning(
                "QwenLM: Auto-login is enabled but no accounts are configured in Credential Manager. "
                "Waiting for manual login..."
            )
            self.notify_user(
                "QwenLM Login",
                "Auto Login is enabled, but no QwenLM accounts are saved. Please log in manually.",
                level="warning",
            )
            while True:
                if await self._has_token_cookie():
                    break
                await asyncio.sleep(0.5)
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_chat_ready(timeout_ms=60000)
            Logger.success("QwenLM: login detected.")
            self._mark_active_ece_pair_used()
            return

        email = str(pair.email or "").strip()
        password = str(pair.password or "")
        if not email or not password:
            Logger.error("QwenLM account is missing an email or password.")
            return

        Logger.info("QwenLM: Auto-login enabled. Attempting login...")

        try:
            await self.page.wait_for_selector(".qwenchat-auth-pc-input-items", timeout=30000)
        except Exception as e:
            Logger.error(f"QwenLM: login form not found: {e}")
            return

        try:
            root = self.page.locator(".qwenchat-auth-pc-input-items")

            email_input = root.first.locator("input:not([type='password'])")
            if await email_input.count() == 0:
                email_input = self.page.locator("input:not([type='password'])")
            await email_input.first.fill(email)

            password_input = self.page.locator("input[type='password']")
            await password_input.first.fill(password)

            submit = self.page.locator("button.qwenchat-auth-pc-submit-button")
            await submit.first.click()
        except Exception as e:
            Logger.error(f"QwenLM: failed to fill credentials/click submit: {e}")
            return

        self.notify_user(
            "QwenLM Login",
            "If QwenLM asks for extra confirmation, please complete it in the browser window.",
            level="info",
        )

        # Wait for cookie + chat UI
        try:
            await asyncio.wait_for(self._wait_for_token_cookie(), timeout=90.0)
        except asyncio.TimeoutError:
            Logger.warning("QwenLM: login cookie not detected after submit. Waiting for manual completion...")
            await self._wait_for_token_cookie()

        try:
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass

        await self._wait_for_chat_ready(timeout_ms=60000)
        Logger.success("QwenLM: chat ready.")
        self.ece_mark_used(email)

    async def _wait_for_token_cookie(self) -> None:
        while True:
            if await self._has_token_cookie():
                return
            await asyncio.sleep(0.5)

    def _resolve_deepthink_flags(self, model: str) -> tuple[bool, bool]:
        enable_deepthink = bool(self.config_manager.get_setting("qwen_behavior", "enable_deepthink"))
        send_deepthink = bool(self.config_manager.get_setting("qwen_behavior", "send_deepthink"))

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

    def _resolve_qwen_request_settings(self, model: str, overrides: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        resolved_model = (model or "").strip() or "qwen-auto"
        ui_model_label = self._get_qwen_model_label_for_request(resolved_model)
        deepthink_enabled, send_deepthink = self._resolve_deepthink_flags(resolved_model)
        enable_search = bool(self.config_manager.get_setting("qwen_behavior", "enable_search"))
        enable_tools = bool(self.config_manager.get_setting("qwen_behavior", "enable_tools"))
        send_as_text_file = bool(self.config_manager.get_setting("qwen_behavior", "send_as_text_file"))

        settings = {
            "model_label": ui_model_label,
            "deepthink_enabled": bool(deepthink_enabled),
            "send_deepthink": bool(send_deepthink),
            "search_enabled": bool(enable_search),
            "tools_enabled": bool(enable_tools),
            "send_as_text_file": bool(send_as_text_file),
        }

        if overrides:
            for key in (
                "deepthink_enabled",
                "send_deepthink",
                "search_enabled",
                "tools_enabled",
                "send_as_text_file",
            ):
                if key in overrides:
                    settings[key] = bool(overrides[key])

        return settings

    def _extract_qwen_macros_from_text(self, text: str) -> tuple[str, Dict[str, bool]]:
        return extract_macro_overrides(text, macro_actions=QWEN_REQUEST_MACRO_ACTIONS)

    def _strip_qwen_macros_from_messages(self, messages: List[Any]) -> tuple[List[Any], Dict[str, bool]]:
        return strip_macros_from_messages(messages, macro_actions=QWEN_REQUEST_MACRO_ACTIONS)

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, Any]]:
        return read_clean_regeneration_state(
            self.cache_manager,
            self.clean_regen_state_cache_key,
            log_label="Clean Regeneration (QwenLM)",
        )

    def _write_clean_regeneration_state(self, state: Dict[str, Any]) -> None:
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
        enable_tools: bool,
        send_as_text_file: bool,
        ui_model_label: str | None = None,
    ) -> Dict[str, Any]:
        return {
            "deepthink_enabled": bool(effective_deepthink),
            "search_enabled": bool(enable_search),
            "tools_enabled": bool(enable_tools),
            "send_as_text_file": bool(send_as_text_file),
            "ui_model": str(ui_model_label or self._get_qwen_model_label_for_request(self.current_model)),
        }

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
            "conversation_url": f"https://chat.qwen.ai/c/{conversation_id}",
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
            url=f"https://chat.qwen.ai/api/v2/chats/{normalized_id}",
            use_xhr=True,
            referrer="https://chat.qwen.ai/",
        )
        if bool(result.get("ok")):
            return True

        detail = str(result.get("error") or result.get("text") or "").strip()
        status = int(result.get("status") or 0)
        suffix = f" ({detail[:180]})" if detail else ""
        Logger.warning(
            f"QwenLM: failed to auto-delete chat {normalized_id} (status={status}){suffix}"
        )
        return False

    async def _auto_delete_current_chat(self) -> bool:
        current_info = await self._get_current_conversation_info()
        if current_info is None:
            Logger.debug("QwenLM: auto-delete skipped because the current chat ID was not available.")
            return False

        conversation_id = str(current_info.get("conversation_id") or "").strip()
        if not conversation_id:
            Logger.debug("QwenLM: auto-delete skipped because the current chat ID was empty.")
            return False

        try:
            await self.click_new_chat(source="auto")
            await asyncio.sleep(0.35)
        except Exception as e:
            Logger.warning(
                f"QwenLM: auto-delete skipped because a replacement chat could not be prepared: {e}"
            )
            return False

        if await self._delete_conversation_by_id(conversation_id):
            Logger.info("QwenLM: auto-deleted the completed chat.")
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
            Logger.warning(f"Multi-Slot Cache (QwenLM): failed to open cached chat URL: {e}")
            return False

        try:
            await self._wait_for_chat_ready(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"Multi-Slot Cache (QwenLM): chat shell did not become ready: {e}")
            return False

        try:
            if not await self._has_token_cookie():
                Logger.warning("Multi-Slot Cache (QwenLM): cached chat URL is not available for the active session.")
                return False
        except Exception:
            pass

        return True

    async def _open_new_chat_route(self) -> bool:
        if not self.page:
            return False

        target_url = f"{self.CHAT_URL}c/new-chat"
        try:
            await self.page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            Logger.warning(f"QwenLM: failed to navigate to the new-chat route: {e}")
            return False

        try:
            await self._wait_for_chat_ready(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"QwenLM: new-chat route loaded, but chat UI was not ready: {e}")
            return False

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
            log_label="Multi-Slot Cache (QwenLM)",
        )
        entry = find_multi_slot_cache_entry(payload, account_key, formatted_message, multi_slot_state)
        if entry is None:
            return False

        current_info = await self._get_current_conversation_info()
        if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
            Logger.info("Multi-Slot Cache (QwenLM): opening cached conversation for regeneration...")
            opened = await self._open_cached_conversation(entry["conversation_url"])
            if not opened:
                return False
            current_info = await self._get_current_conversation_info()
            if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
                Logger.warning(
                    "Multi-Slot Cache (QwenLM): cached conversation URL opened, but the expected "
                    "chat ID was not available. Falling back to a new chat."
                )
                return False

        try:
            await self.apply_configured_model(model=self.current_model)
            await self.set_tools_state(bool(multi_slot_state.get("tools_enabled")))
            await self.set_deepthink_state(bool(multi_slot_state.get("deepthink_enabled")))
            await self.set_search_state(bool(multi_slot_state.get("search_enabled")))
            await asyncio.sleep(0.25)
        except Exception:
            pass

        Logger.info("Multi-Slot Cache (QwenLM): cached prompt match found. Attempting to regenerate...")
        if not await self._click_regenerate(arm_event=completion_armed):
            Logger.warning(
                "Multi-Slot Cache (QwenLM): regenerate button unavailable. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (QwenLM)",
            )
            return False

        try:
            await asyncio.wait_for(
                completion_started.wait(),
                timeout=self._completion_request_timeout_s,
            )
        except asyncio.TimeoutError:
            Logger.warning(
                "Multi-Slot Cache (QwenLM): completion request not observed after clicking "
                "Regenerate. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (QwenLM)",
            )
            return False

        return True

    async def _ensure_rp_friendly_settings(self) -> bool:
        if not self.page:
            return False

        # Don't spam settings calls; this is just a "guardrail" feature
        now = time.time()
        last_attempt = float(getattr(self, "_rp_settings_last_attempt_ts", 0.0) or 0.0)
        if (now - last_attempt) < 15.0:
            return False
        self._rp_settings_last_attempt_ts = now

        Logger.info("QwenLM: checking RP-friendly provider settings...")

        settings: Any = None
        try:
            settings = await self.page.evaluate(
                "() => fetch('/api/v2/users/user/settings', { credentials: 'include' })"
                ".then(r => r.json()).catch(() => null)"
            )
        except Exception as e:
            Logger.warning(f"QwenLM: failed to fetch user settings: {e}")
            settings = None

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

        desired: list[dict[str, Any]] = []

        if isinstance(settings, dict):
            data = settings.get("data")
        else:
            data = None

        def _must_force_false(section: Any, key: str) -> bool:
            if not isinstance(section, dict):
                return True
            if key not in section:
                return True
            return _flag_is_true(section.get(key))

        if isinstance(data, dict):
            ui = data.get("ui")
            memory = data.get("memory")

            if _must_force_false(ui, "largeTextAsFile"):
                desired.append({"ui": {"largeTextAsFile": False}})
            if _must_force_false(ui, "splitLargeChunks"):
                desired.append({"ui": {"splitLargeChunks": False}})
            if _must_force_false(memory, "enable_memory"):
                desired.append({"memory": {"enable_memory": False}})
            if _must_force_false(memory, "enable_history_memory"):
                desired.append({"memory": {"enable_history_memory": False}})

        if not desired:
            # if we couldn't read settings, still try a safe, idempotent "set to false" pass
            if not isinstance(data, dict):
                Logger.warning(
                    "QwenLM: could not read user settings (unexpected response). Attempting blind settings update..."
                )
                desired = [
                    {"ui": {"largeTextAsFile": False}},
                    {"ui": {"splitLargeChunks": False}},
                    {"memory": {"enable_memory": False}},
                    {"memory": {"enable_history_memory": False}},
                ]
            else:
                Logger.info("QwenLM: RP-friendly settings already look OK.")
                return False

        Logger.info(f"QwenLM: applying {len(desired)} settings tweak(s) for RP compatibility...")

        results: Any = None
        try:
            results = await self.page.evaluate(
                "async (updates) => {"
                "  const sleep = (ms) => new Promise(r => setTimeout(r, ms));"
                "  const out = [];"
                "  for (const body of (updates || [])) {"
                "    try {"
                "      const resp = await fetch('/api/v2/users/user/settings/update', {"
                "        method: 'POST',"
                "        credentials: 'include',"
                "        headers: { 'Content-Type': 'application/json' },"
                "        body: JSON.stringify(body),"
                "      });"
                "      let success = null;"
                "      try {"
                "        const j = await resp.json();"
                "        if (j && typeof j === 'object' && ('success' in j)) success = !!j.success;"
                "      } catch (e) { success = null; }"
                "      out.push({ ok: !!resp.ok, status: (resp.status || 0), success });"
                "    } catch (e) {"
                "      out.push({ ok: false, status: 0, error: String(e) });"
                "    }"
                "    await sleep(220);"
                "  }"
                "  return out;"
                "}",
                desired,
            )
        except Exception as e:
            Logger.warning(f"QwenLM: settings update calls failed: {e}")
            return True

        try:
            if isinstance(results, list) and results:
                ok_count = 0
                for r in results:
                    if isinstance(r, dict) and r.get("ok") is True:
                        ok_count += 1
                if ok_count != len(results):
                    Logger.warning(
                        f"QwenLM: {len(results) - ok_count}/{len(results)} settings update request(s) failed."
                    )
                    Logger.debug(f"QwenLM: settings update results: {results}")
        except Exception:
            pass

        return True

    def api_real_model_labels(self) -> list[str]:
        return list(self.MODEL_LABELS)

    def _get_qwen_model_label_for_request(self, model: Any = None) -> str:
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        return override or self._get_configured_qwen_model_label()

    async def apply_configured_model(self, model: Any = None) -> None:
        desired = self._get_qwen_model_label_for_request(model)
        if not desired:
            return

        try:
            await self._ensure_qwen_model_selected(desired)
        except Exception as e:
            Logger.warning(f"QwenLM: Failed to apply model selection '{desired}': {e}")

    def _get_configured_qwen_model_label(self) -> str:
        try:
            value = self.config_manager.get_setting("qwen_behavior", "model")
        except Exception:
            value = None
        return str(value or "").strip()

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    @staticmethod
    def _canonicalize_label_text(value: str) -> str:
        normalized = QwenLMDriver._normalize_text(value)
        try:
            normalized = unicodedata.normalize("NFKC", normalized)
            normalized = (
                normalized.replace("\u200b", "")
                .replace("\u200c", "")
                .replace("\u200d", "")
                .replace("\ufeff", "")
            )
        except Exception:
            pass
        return re.sub(r"[^a-z0-9]+", "", normalized)

    @staticmethod
    def _canonicalize_model_label(value: str) -> str:
        return QwenLMDriver._canonicalize_label_text(value)

    async def _read_current_qwen_model_label(self) -> str:
        if not self.page:
            return ""

        try:
            label = await self.page.evaluate(
                "() => {"
                "  const el = document.querySelector(\"[class*='model-selector-text']\");"
                "  if (!el) return '';"
                "  return (el.textContent || '').toString().trim();"
                "}"
            )
        except Exception as e:
            Logger.debug(f"QwenLM: failed to read current model label: {e}")
            return ""

        return str(label or "").strip()

    async def _wait_for_qwen_model_label(self, desired_canon: str, timeout_s: float = 2.0) -> bool:
        if not self.page:
            return False
        safe = str(desired_canon or "").strip()
        if not safe:
            return False

        deadline = time.monotonic() + float(timeout_s or 0.0)
        while time.monotonic() < deadline:
            current = await self._read_current_qwen_model_label()
            if self._canonicalize_model_label(current) == safe:
                return True
            await asyncio.sleep(0.1)

        return False

    async def _open_qwen_model_dropdown(self, timeout_ms: int = 6000) -> bool:
        if not self.page:
            return False

        text_el = self.page.locator(self.MODEL_SELECTOR_TEXT_SELECTOR)
        if await text_el.count() == 0:
            Logger.warning("QwenLM: model selector not found.")
            return False

        candidates = [
            text_el.first.locator("xpath=.."),
            text_el.first.locator("xpath=../.."),
            text_el.first,
        ]
        last_error: str | None = None

        for cand in candidates:
            try:
                await cand.click(timeout=3000)
            except Exception as e:
                last_error = str(e)
                continue

            try:
                await self.page.wait_for_selector(
                    f"{self.MODEL_SELECTOR_POPUP_SELECTOR} div[class*='model-list']",
                    timeout=int(timeout_ms),
                    state="visible",
                )
                return True
            except Exception:
                continue

        if last_error:
            Logger.warning(f"QwenLM: failed to click model selector trigger: {last_error}")
        Logger.warning("QwenLM: model selector popup did not appear.")
        return False

    async def _close_qwen_model_dropdown(self) -> None:
        if not self.page:
            return

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

    @staticmethod
    def _extract_qwen_summary_parts(extra: dict[str, Any], key: str) -> List[str]:
        value = extra.get(key)
        if not isinstance(value, dict):
            return []

        content = value.get("content")
        if isinstance(content, list):
            return [str(item or "").strip() for item in content]
        if isinstance(content, str):
            return [content.strip()]
        return []

    @classmethod
    def _extract_qwen_thinking_summary_text(cls, delta: dict[str, Any]) -> str:
        extra = delta.get("extra")
        if not isinstance(extra, dict):
            return ""

        titles = cls._extract_qwen_summary_parts(extra, "summary_title")
        thoughts = cls._extract_qwen_summary_parts(extra, "summary_thought")
        section_count = max(len(titles), len(thoughts))
        sections: List[str] = []

        for idx in range(section_count):
            title = titles[idx] if idx < len(titles) else ""
            thought = thoughts[idx] if idx < len(thoughts) else ""
            if title and thought:
                sections.append(f"{title}\n{thought}")
            elif title or thought:
                sections.append(title or thought)

        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _compute_missing_suffix(emitted: str, candidate: str) -> str:
        return compute_missing_suffix(emitted, candidate)

    @classmethod
    def _compute_missing_thinking_summary_text(cls, emitted: str, candidate: str) -> str:
        missing = cls._compute_missing_suffix(emitted, candidate)
        if emitted and missing and missing == candidate and not missing.startswith(("\n", "\r")):
            return "\n\n" + missing
        return missing

    async def _click_model_option_in_popup(self, target_label: str) -> bool:
        if not self.page:
            return False

        wanted = str(target_label or "").strip()
        if not wanted:
            return False

        popup_sel = f"{self.MODEL_SELECTOR_POPUP_SELECTOR}, div.ant-dropdown, div[class*='ant-dropdown-menu'], div[class*='model-selector']"
        item_sel = "div[class*='model-item___']"
        name_span_sel = "div[class*='model-item-name'] span"

        async def _try_click_once() -> bool:
            try:
                return bool(
                    await self.page.evaluate(
                        "(popupSel, itemSel, nameSpanSel, wantedRaw) => {"
                        "  const canon = (v) => {"
                        "    try {"
                        "      return (v || '').toString().normalize('NFKC').replace(/[^a-z0-9]+/gi, '').toLowerCase();"
                        "    } catch (e) {"
                        "      return (v || '').toString().replace(/[^a-z0-9]+/gi, '').toLowerCase();"
                        "    }"
                        "  };"
                        "  const isVisible = (el) => {"
                        "    try {"
                        "      if (!el) return false;"
                        "      const r = el.getBoundingClientRect();"
                        "      if (!r || r.width <= 0 || r.height <= 0) return false;"
                        "      const style = window.getComputedStyle(el);"
                        "      if (!style) return false;"
                        "      if (style.visibility === 'hidden' || style.display === 'none') return false;"
                        "      return true;"
                        "    } catch (e) { return false; }"
                        "  };"
                        "  const wantedCanon = canon(wantedRaw);"
                        "  if (!wantedCanon) return false;"
                        "  const popups = Array.from(document.querySelectorAll(popupSel)).filter(isVisible);"
                        "  if (!popups.length) return false;"
                        "  for (const popup of popups) {"
                        "    let items = Array.from(popup.querySelectorAll(itemSel));"
                        "    if (!items.length) items = Array.from(popup.querySelectorAll(\"div[class*='model-item']\"));"
                        "    items = items.filter(isVisible);"
                        "    for (const item of items) {"
                        "      const nameEl = item.querySelector(\"div[class*='model-item-name']\");"
                        "      const span = (nameEl && nameEl.querySelector('span')) || item.querySelector(nameSpanSel) || item.querySelector('span');"
                        "      const raw = (span && span.textContent) ? span.textContent : ((nameEl && nameEl.textContent) ? nameEl.textContent : '');"
                        "      const labelCanon = canon(raw);"
                        "      if (!labelCanon) continue;"
                        "      if (labelCanon === wantedCanon) {"
                        "        try { item.scrollIntoView({block: 'center'}); } catch (e) {}"
                        "        try { item.click(); } catch (e) { try { (span || nameEl || item).click(); } catch (e2) {} }"
                        "        return true;"
                        "      }"
                        "    }"
                        "  }"
                        "  return false;"
                        "}",
                        popup_sel,
                        item_sel,
                        name_span_sel,
                        wanted,
                    )
                )
            except Exception:
                return False

        if await _try_click_once():
            return True

        # Expand "more models" list (it's hidden behind a hover/click target)
        try:
            revealed = await self.page.evaluate(
                "(popupSel) => {"
                "  const norm = (v) => (v || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();"
                "  const isVisible = (el) => {"
                "    try {"
                "      if (!el) return false;"
                "      const r = el.getBoundingClientRect();"
                "      if (!r || r.width <= 0 || r.height <= 0) return false;"
                "      const style = window.getComputedStyle(el);"
                "      if (!style) return false;"
                "      if (style.visibility === 'hidden' || style.display === 'none') return false;"
                "      return true;"
                "    } catch (e) { return false; }"
                "  };"
                "  const popups = Array.from(document.querySelectorAll(popupSel)).filter(isVisible);"
                "  if (!popups.length) return false;"
                "  for (const popup of popups) {"
                "    const candidates = Array.from(popup.querySelectorAll(\"span.ant-dropdown-trigger, div[class*='view-more'], [class*='more'], div, span, li, button\"));"
                "    for (const el of candidates) {"
                "      const t = norm(el.textContent || '');"
                "      if (!t.includes('expand more models') && !t.includes('more models')) continue;"
                "      try { el.dispatchEvent(new MouseEvent('mouseenter', {bubbles:true})); } catch (e) {}"
                "      try { el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true})); } catch (e) {}"
                "      try { el.click(); } catch (e) {}"
                "      return true;"
                "    }"
                "  }"
                "  return false;"
                "}",
                popup_sel,
            )
        except Exception:
            revealed = False

        if revealed:
            await asyncio.sleep(0.12)
            if await _try_click_once():
                return True

        # Scroll each list a bit to work around virtualization
        for _ in range(10):
            try:
                await self.page.evaluate(
                    f"() => {{"
                    f"  const isVisible = (el) => {{"
                    f"    try {{"
                    f"      if (!el) return false;"
                    f"      const r = el.getBoundingClientRect();"
                    f"      if (!r || r.width <= 0 || r.height <= 0) return false;"
                    f"      const style = window.getComputedStyle(el);"
                    f"      if (!style) return false;"
                    f"      if (style.visibility === 'hidden' || style.display === 'none') return false;"
                    f"      return true;"
                    f"    }} catch (e) {{ return false; }}"
                    f"  }};"
                    f"  const popups = Array.from(document.querySelectorAll('{self.MODEL_SELECTOR_POPUP_SELECTOR}')).filter(isVisible);"
                    f"  for (const popup of popups) {{"
                    f"    const lists = Array.from(popup.querySelectorAll(\"div[class*='model-list']\")).filter(isVisible);"
                    f"    for (const el of lists) {{ el.scrollTop = (el.scrollTop || 0) + 220; }}"
                    f"  }}"
                    f"}}"
                )
            except Exception:
                pass

            await asyncio.sleep(0.08)
            if await _try_click_once():
                return True

        return False

    async def _read_qwen_model_picker_labels(self) -> List[str]:
        if not self.page:
            return []

        try:
            labels = await self.page.evaluate(
                "(popupSel) => {"
                "  const isVisible = (el) => {"
                "    try {"
                "      if (!el) return false;"
                "      const r = el.getBoundingClientRect();"
                "      if (!r || r.width <= 0 || r.height <= 0) return false;"
                "      const style = window.getComputedStyle(el);"
                "      if (!style) return false;"
                "      if (style.visibility === 'hidden' || style.display === 'none') return false;"
                "      return true;"
                "    } catch (e) { return false; }"
                "  };"
                "  const popups = Array.from(document.querySelectorAll(popupSel)).filter(isVisible);"
                "  const out = [];"
                "  for (const popup of popups) {"
                "    let items = Array.from(popup.querySelectorAll(\"div[class*='model-item___']\"));"
                "    if (!items.length) items = Array.from(popup.querySelectorAll(\"div[class*='model-item']\"));"
                "    items = items.filter(isVisible);"
                "    for (const item of items) {"
                "      const nameEl = item.querySelector(\"div[class*='model-item-name']\");"
                "      const span = (nameEl && nameEl.querySelector('span')) || item.querySelector(\"div[class*='model-item-name'] span\") || item.querySelector('span');"
                "      const raw = (span && span.textContent) ? span.textContent : ((nameEl && nameEl.textContent) ? nameEl.textContent : '');"
                "      const text = (raw || '').toString().trim();"
                "      if (text) out.push(text);"
                "    }"
                "  }"
                "  return out;"
                "}",
                self.MODEL_SELECTOR_POPUP_SELECTOR,
            )
        except Exception as e:
            Logger.debug(f"QwenLM: failed to read model picker labels: {e}")
            return []

        if not isinstance(labels, list):
            return []

        out: List[str] = []
        seen: set[str] = set()
        for raw in labels:
            try:
                text = str(raw or "").strip()
            except Exception:
                continue
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append(text)

        return out

    async def _click_model_option_in_popup_locator(self, target_label: str) -> bool:
        if not self.page:
            return False

        wanted = str(target_label or "").strip()
        if not wanted:
            return False

        wanted_canon = self._canonicalize_model_label(wanted)
        if not wanted_canon:
            return False

        root = self.page.locator(self.MODEL_SELECTOR_POPUP_SELECTOR)
        popup_count = await root.count()
        if popup_count == 0:
            return False

        async def _try_click() -> bool:
            for pidx in range(min(popup_count, 6)):
                popup = root.nth(pidx)
                try:
                    if not await popup.is_visible():
                        continue
                except Exception:
                    pass

                spans = popup.locator("div[class*='model-item-name'] span")
                span_count = await spans.count()
                if span_count == 0:
                    spans = popup.locator("span")
                    span_count = await spans.count()

                for sidx in range(min(span_count, 80)):
                    cand = spans.nth(sidx)
                    try:
                        text = str(await cand.text_content() or "").strip()
                    except Exception:
                        continue
                    if self._canonicalize_model_label(text) != wanted_canon:
                        continue

                    item = cand.locator("xpath=ancestor::div[contains(@class,'model-item___')][1]")
                    if await item.count() == 0:
                        item = cand.locator("xpath=ancestor::div[contains(@class,'model-item')][1]")

                    try:
                        if await item.count() > 0:
                            await item.first.click(timeout=3000)
                        else:
                            await cand.click(timeout=3000)
                        return True
                    except Exception:
                        try:
                            if await item.count() > 0:
                                await item.first.click(timeout=3000, force=True)
                            else:
                                await cand.click(timeout=3000, force=True)
                            return True
                        except Exception:
                            continue

            return False

        if await _try_click():
            return True

        # Attempt to reveal expanded model list (hover/click) and retry
        try:
            trigger = self.page.locator(
                f"{self.MODEL_SELECTOR_POPUP_SELECTOR} div[class*='view-more']",
                has_text="Expand more models",
            )
            if await trigger.count() > 0:
                try:
                    await trigger.first.hover(timeout=1500)
                except Exception:
                    pass
                await asyncio.sleep(0.08)
                if await _try_click():
                    return True

                try:
                    await trigger.first.click(timeout=2000)
                except Exception:
                    pass
                await asyncio.sleep(0.12)
                if await _try_click():
                    return True
        except Exception:
            pass

        return False

    async def _ensure_qwen_model_selected(self, desired_label: str) -> None:
        desired = str(desired_label or "").strip()
        if not desired:
            return

        desired_canon = self._canonicalize_model_label(desired)

        current = await self._read_current_qwen_model_label()
        if desired_canon and self._canonicalize_model_label(current) == desired_canon:
            return

        if not await self._open_qwen_model_dropdown():
            return

        try:
            clicked = await self._click_model_option_in_popup(desired)
            if clicked:
                if desired_canon and await self._wait_for_qwen_model_label(desired_canon, timeout_s=2.0):
                    Logger.info(f"QwenLM: selected model '{desired}'.")
                    return

                Logger.warning(
                    f"QwenLM: clicked model '{desired}' but selection did not update. "
                    "Trying locator click fallback..."
                )
                fallback_clicked = await self._click_model_option_in_popup_locator(desired)
                if fallback_clicked:
                    if desired_canon and await self._wait_for_qwen_model_label(desired_canon, timeout_s=2.0):
                        Logger.info(f"QwenLM: selected model '{desired}'.")
                        return
                Logger.warning(f"QwenLM: failed to apply model selection '{desired}' after click attempts.")
                return

            fallback_clicked = await self._click_model_option_in_popup_locator(desired)
            if fallback_clicked:
                if desired_canon and await self._wait_for_qwen_model_label(desired_canon, timeout_s=2.0):
                    Logger.info(f"QwenLM: selected model '{desired}'.")
                    return

                Logger.warning(f"QwenLM: clicked model '{desired}' via locator fallback but selection did not update.")
                return

            try:
                labels = await self._read_qwen_model_picker_labels()
                if labels:
                    shown = labels[:20]
                    extra = len(labels) - len(shown)
                    suffix = f" (+{extra} more)" if extra > 0 else ""
                    Logger.debug(f"QwenLM: model picker options: {shown}{suffix}")

                    try:
                        if desired_canon and any(self._canonicalize_model_label(l) == desired_canon for l in labels):
                            Logger.warning(
                                f"QwenLM: model '{desired}' is present in the picker but could not be selected."
                            )
                            return
                    except Exception:
                        pass
                else:
                    Logger.debug("QwenLM: model picker options: (none detected)")
            except Exception:
                pass
            Logger.warning(f"QwenLM: target model '{desired}' not found in picker.")
        finally:
            await self._close_qwen_model_dropdown()

    async def set_sidebar_status(self, open: bool) -> None:
        if not self.page:
            return

        sidebar = self.page.locator(self.SIDEBAR_SELECTOR)
        if await sidebar.count() == 0:
            Logger.warning("QwenLM: sidebar container not found.")
            return

        state_attr = ""
        try:
            state_attr = str(await sidebar.first.get_attribute("data-state") or "").strip().lower()
        except Exception:
            state_attr = ""

        is_open = state_attr == "true"
        if open and is_open:
            return
        if (not open) and (not is_open):
            return

        if open:
            btn = self.page.locator(self.SIDEBAR_OPEN_BUTTON_SELECTOR)
            if await btn.count() == 0:
                Logger.warning("QwenLM: open-sidebar button not found.")
                return
            try:
                await btn.first.click(timeout=3000)
            except Exception as e:
                Logger.warning(f"QwenLM: failed to open sidebar: {e}")
                return
        else:
            btn = self.page.locator(self.SIDEBAR_CLOSE_BUTTON_SELECTOR)
            if await btn.count() == 0:
                Logger.warning("QwenLM: close-sidebar button not found.")
                return
            try:
                await btn.first.click(timeout=3000)
            except Exception as e:
                Logger.warning(f"QwenLM: failed to close sidebar: {e}")
                return

    async def _is_sidebar_open(self) -> bool:
        if not self.page:
            return False

        sidebar = self.page.locator(self.SIDEBAR_SELECTOR)
        if await sidebar.count() == 0:
            return False

        try:
            state_attr = str(await sidebar.first.get_attribute("data-state") or "").strip().lower()
        except Exception:
            return False

        return state_attr == "true"

    async def _click_new_chat_entry(self) -> bool:
        if not self.page:
            return False

        exact_label_matches = self.page.locator(self.NEW_CHAT_BUTTON_SELECTOR, has_text="New Chat")
        try:
            exact_count = await exact_label_matches.count()
        except Exception:
            exact_count = 0

        for idx in range(min(exact_count, 6)):
            label = exact_label_matches.nth(idx)
            try:
                if not await label.is_visible():
                    continue
            except Exception:
                continue

            targets = [
                label.locator("xpath=.."),
                label,
            ]

            for target in targets:
                try:
                    if await target.count() == 0:
                        continue
                except Exception:
                    continue

                candidate = target.first
                try:
                    await candidate.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass

                try:
                    await candidate.click(timeout=3000)
                    return True
                except Exception:
                    try:
                        await candidate.click(timeout=3000, force=True)
                        return True
                    except Exception:
                        continue

        try:
            clicked = await self.page.evaluate(
                "(fixedSel) => {"
                "  const isVisible = (el) => {"
                "    try {"
                "      if (!el) return false;"
                "      const rect = el.getBoundingClientRect();"
                "      if (!rect || rect.width <= 0 || rect.height <= 0) return false;"
                "      const style = window.getComputedStyle(el);"
                "      if (!style) return false;"
                "      if (style.visibility === 'hidden' || style.display === 'none') return false;"
                "      return true;"
                "    } catch (e) { return false; }"
                "  };"
                "  const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();"
                "  const readLabel = (el) => {"
                "    const bits = [];"
                "    const push = (value) => {"
                "      const text = normalize(value);"
                "      if (text) bits.push(text);"
                "    };"
                "    let current = el;"
                "    for (let depth = 0; current && depth < 3; depth += 1, current = current.parentElement) {"
                "      push(current.textContent);"
                "      push(current.getAttribute && current.getAttribute('aria-label'));"
                "      push(current.getAttribute && current.getAttribute('title'));"
                "    }"
                "    return bits.join(' ').trim();"
                "  };"
                "  const clickCandidate = (el) => {"
                "    const targets = ["
                "      el.parentElement,"
                "      el.closest('[role=\"button\"]'),"
                "      el.closest('a'),"
                "      el,"
                "    ].filter(Boolean);"
                "    for (const target of targets) {"
                "      try { target.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}"
                "      try { target.click(); return true; } catch (e) {}"
                "    }"
                "    return false;"
                "  };"
                "  const candidates = [];"
                "  for (const el of Array.from(document.querySelectorAll(fixedSel))) {"
                "    if (!isVisible(el)) continue;"
                "    candidates.push({ el, label: readLabel(el) });"
                "  }"
                "  if (!candidates.length) return false;"
                "  const scored = candidates"
                "    .map((candidate, index) => {"
                "      const label = candidate.label;"
                "      let score = 0;"
                "      if (label.includes('new chat')) score += 1000;"
                "      else if (label.includes('new')) score += 300;"
                "      if (label.includes('chat')) score += 150;"
                "      if (label.includes('community')) score -= 1200;"
                "      if (label.includes('discover')) score -= 800;"
                "      if (label.includes('explore')) score -= 600;"
                "      return { ...candidate, index, score };"
                "    })"
                "    .sort((left, right) => (right.score - left.score) || (left.index - right.index));"
                "  for (const candidate of scored) {"
                "    if (candidate.score < 0) continue;"
                "    if (clickCandidate(candidate.el)) return true;"
                "  }"
                "  return false;"
                "}",
                self.NEW_CHAT_BUTTON_SELECTOR,
            )
        except Exception as e:
            Logger.debug(f"QwenLM: failed to evaluate new-chat candidates: {e}")
            return False

        return bool(clicked)

    async def click_new_chat(self, source: str = "auto") -> None:
        if not self.page:
            return

        if source not in {"auto", "sidebar", "simple"}:
            Logger.warning(f"QwenLM: unknown new chat source '{source}'.")

        allow_sidebar_open = source in {"auto", "sidebar"}
        sidebar_open_attempted = False

        if source == "sidebar":
            await self.set_sidebar_status(open=True)
            sidebar_open_attempted = True

        deadline = time.monotonic() + (self.NEW_CHAT_LOOKUP_TIMEOUT_MS / 1000.0)
        while True:
            if await self._click_new_chat_entry():
                return

            # Qwen can render the fixed sidebar entries a moment after the rest of the chat UI
            if allow_sidebar_open and not sidebar_open_attempted:
                if not await self._is_sidebar_open():
                    await self.set_sidebar_status(open=True)
                sidebar_open_attempted = True
                continue

            if time.monotonic() >= deadline:
                break

            await asyncio.sleep(self.NEW_CHAT_LOOKUP_POLL_INTERVAL_S)

        Logger.warning(
            "QwenLM: New Chat button not found before timeout "
            f"({self.NEW_CHAT_LOOKUP_TIMEOUT_MS} ms). Trying route fallback..."
        )
        if await self._open_new_chat_route():
            Logger.info("QwenLM: opened a new chat via /c/new-chat route fallback.")
            return

        Logger.warning("QwenLM: route fallback for New Chat also failed.")

    async def _read_thinking_mode(self) -> str:
        if not self.page:
            return ""

        trigger = self.page.locator(self.THINKING_TRIGGER_SELECTOR)
        label = trigger.first.locator(self.THINKING_TRIGGER_LABEL_SELECTOR)
        if await label.count() == 0:
            return ""

        try:
            text = (await label.first.inner_text() or "").strip()
        except Exception:
            return ""

        return text

    async def _wait_for_thinking_mode_label(self, desired_canon: str, timeout_s: float = 2.0) -> bool:
        if not self.page:
            return False

        safe = str(desired_canon or "").strip()
        if not safe:
            return False

        deadline = time.monotonic() + float(timeout_s or 0.0)
        while time.monotonic() < deadline:
            current = await self._read_thinking_mode()
            if self._canonicalize_label_text(current) == safe:
                return True
            await asyncio.sleep(0.1)

        return False

    async def _read_visible_thinking_mode_candidates(self) -> List[str]:
        if not self.page:
            return []

        seen: set[str] = set()
        out: List[str] = []
        labels = self.page.locator(
            f"{self.THINKING_OPTIONS_SELECTOR} {self.THINKING_OPTION_LABEL_SELECTOR}"
        )

        count = 0
        try:
            count = await labels.count()
        except Exception:
            count = 0

        for idx in range(min(count, 20)):
            cand = labels.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
            except Exception:
                continue

            try:
                text = str(await cand.text_content() or "").strip()
            except Exception:
                continue
            if not text or text in seen:
                continue

            seen.add(text)
            out.append(text)

        return out

    async def _click_thinking_mode_option(self, target_label: str) -> bool:
        if not self.page:
            return False

        wanted = str(target_label or "").strip()
        wanted_canon = self._canonicalize_label_text(wanted)
        if not wanted_canon:
            return False

        async def _try_dom_click() -> bool:
            try:
                clicked = await self.page.evaluate(
                    "(rootSel, optionSel, labelSel, wantedCanon) => {"
                    "  const canon = (value) => {"
                    "    try {"
                    "      return (value || '').toString().normalize('NFKC')"
                    "        .replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '')"
                    "        .replace(/[^a-z0-9]+/gi, '')"
                    "        .toLowerCase();"
                    "    } catch (e) {"
                    "      return (value || '').toString().replace(/[^a-z0-9]+/gi, '').toLowerCase();"
                    "    }"
                    "  };"
                    "  const isVisible = (el) => {"
                    "    try {"
                    "      if (!el) return false;"
                    "      const rect = el.getBoundingClientRect();"
                    "      if (!rect || rect.width <= 0 || rect.height <= 0) return false;"
                    "      const style = window.getComputedStyle(el);"
                    "      if (!style) return false;"
                    "      if (style.display === 'none' || style.visibility === 'hidden') return false;"
                    "      return true;"
                    "    } catch (e) {"
                    "      return false;"
                    "    }"
                    "  };"
                    "  const roots = Array.from(document.querySelectorAll(rootSel)).filter(isVisible);"
                    "  for (const root of roots) {"
                    "    const labels = Array.from(root.querySelectorAll(labelSel)).filter(isVisible);"
                    "    for (const label of labels) {"
                    "      const raw = (label.textContent || '').toString().trim();"
                    "      if (canon(raw) !== wantedCanon) continue;"
                    "      const target = label.closest(optionSel) || label;"
                    "      try { target.scrollIntoView({ block: 'center' }); } catch (e) {}"
                    "      try { target.click(); return true; } catch (e) {}"
                    "      try { label.click(); return true; } catch (e) {}"
                    "    }"
                    "  }"
                    "  return false;"
                    "}",
                    self.THINKING_OPTIONS_SELECTOR,
                    self.THINKING_OPTION_SELECTOR,
                    self.THINKING_OPTION_LABEL_SELECTOR,
                    wanted_canon,
                )
            except Exception:
                clicked = False
            return bool(clicked)

        async def _try_locator_click() -> bool:
            labels = self.page.locator(
                f"{self.THINKING_OPTIONS_SELECTOR} {self.THINKING_OPTION_LABEL_SELECTOR}"
            )

            count = 0
            try:
                count = await labels.count()
            except Exception:
                count = 0

            for idx in range(min(count, 20)):
                cand = labels.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                except Exception:
                    continue

                try:
                    text = str(await cand.text_content() or "").strip()
                except Exception:
                    continue
                if self._canonicalize_label_text(text) != wanted_canon:
                    continue

                option = cand.locator("xpath=ancestor::div[contains(@class,'ant-select-item-option')][1]")

                target = option.first if await option.count() > 0 else cand

                try:
                    await target.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass

                try:
                    await target.click(timeout=2000)
                    return True
                except Exception:
                    pass

                try:
                    await target.click(timeout=2000, force=True)
                    return True
                except Exception:
                    pass

                try:
                    await target.evaluate("el => el.click()")
                    return True
                except Exception:
                    continue

            return False

        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if await _try_dom_click():
                return True
            if await _try_locator_click():
                return True
            await asyncio.sleep(0.1)

        return False

    async def _set_thinking_mode(self, mode: str) -> bool:
        if not self.page:
            return False

        wanted = str(mode or "").strip()
        if not wanted:
            return False

        wanted_canon = self._canonicalize_label_text(wanted)
        current = await self._read_thinking_mode()
        if self._canonicalize_label_text(current) == wanted_canon:
            return True

        trigger = self.page.locator(self.THINKING_TRIGGER_SELECTOR)
        if await trigger.count() == 0:
            Logger.warning("QwenLM: thinking selector trigger not found.")
            return False

        try:
            await trigger.first.click(timeout=3000)
        except Exception as e:
            Logger.warning(f"QwenLM: failed to open thinking dropdown: {e}")
            return False

        clicked = await self._click_thinking_mode_option(wanted)
        if clicked and await self._wait_for_thinking_mode_label(wanted_canon, timeout_s=2.0):
            return True

        visible = await self._read_visible_thinking_mode_candidates()
        visible_suffix = f" Visible options: {visible}" if visible else ""
        if clicked:
            Logger.warning(
                f"QwenLM: clicked thinking mode '{wanted}' but the selected label did not update.{visible_suffix}"
            )
        else:
            Logger.warning(f"QwenLM: failed to find thinking mode '{wanted}' in the dropdown.{visible_suffix}")
        return False

    async def set_deepthink_state(self, state: bool) -> None:
        # Qwen exposes Auto / Thinking / Fast. IntenseRP uses a boolean switch, so we map:
        # True -> Thinking, False -> Fast
        target = "Thinking" if state else "Fast"
        ok = await self._set_thinking_mode(target)
        if not ok:
            Logger.warning(f"QwenLM: could not set Thinking mode to '{target}'.")

    async def _is_search_enabled(self) -> bool:
        if not self.page:
            return False

        container = self.page.locator(self.SEARCH_MODE_CONTAINER_SELECTOR)
        if await container.count() == 0:
            return False

        try:
            text = str(await container.first.inner_text() or "").strip().lower()
            return "search" in text
        except Exception:
            pass

        # Fallback: JS query (avoid relying on volatile anchor IDs)
        try:
            enabled = await self.page.evaluate(
                f"() => {{"
                f"  const root = document.querySelector('{self.SEARCH_MODE_CONTAINER_SELECTOR}');"
                f"  if (!root) return false;"
                f"  const text = (root.textContent || '').toString().toLowerCase();"
                f"  return text.includes('search');"
                f"}}"
            )
        except Exception:
            enabled = False

        return bool(enabled)

    async def _click_first_visible(self, locator, timeout_ms: int = 3000) -> bool:
        if not self.page:
            return False

        count = 0
        try:
            count = await locator.count()
        except Exception:
            count = 0

        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                continue

            try:
                await candidate.scroll_into_view_if_needed(timeout=int(timeout_ms))
            except Exception:
                pass

            try:
                await candidate.click(timeout=int(timeout_ms))
                return True
            except Exception:
                pass

            try:
                await candidate.evaluate("el => el.click()")
                return True
            except Exception:
                continue

        return False

    async def _click_first_visible_playwright_click(self, locator, timeout_ms: int = 3000) -> bool:
        if not self.page:
            return False

        count = 0
        try:
            count = await locator.count()
        except Exception:
            count = 0

        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                continue

            try:
                await candidate.scroll_into_view_if_needed(timeout=int(timeout_ms))
            except Exception:
                pass

            try:
                await candidate.click(timeout=int(timeout_ms))
                return True
            except Exception:
                pass

            try:
                await candidate.click(timeout=int(timeout_ms), force=True)
                return True
            except Exception:
                continue

        return False

    def _desired_tools_state(self) -> bool:
        if self._tools_disabled_for_request:
            return False
        return bool(self.current_tools_enabled) if self.current_tools_enabled is not None else False

    async def _set_tools_switch_in_mode_select_menu(self, state: bool, menu=None) -> str:
        if not self.page:
            return "missing"

        wanted = bool(state)

        scope = menu
        if scope is None:
            scope = self.page.locator(self.MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR)
            if await scope.count() == 0:
                scope = self.page.locator("ul.ant-dropdown-menu-root")

        try:
            switches = scope.locator(self.MODE_SELECT_TOOLS_SWITCH_SELECTOR)
        except Exception:
            switches = self.page.locator(self.MODE_SELECT_TOOLS_SWITCH_SELECTOR)

        try:
            count = await switches.count()
        except Exception:
            count = 0

        if count == 0:
            switches = self.page.locator(self.MODE_SELECT_TOOLS_SWITCH_SELECTOR)
            try:
                count = await switches.count()
            except Exception:
                count = 0

        visible_switches = []
        tools_switches = []
        for idx in range(count):
            switch = switches.nth(idx)
            try:
                if not await switch.is_visible():
                    continue
            except Exception:
                continue

            label = ""
            try:
                label = str(
                    await switch.evaluate(
                        """(el) => {
                            const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();
                            let current = el;
                            for (let depth = 0; current && depth < 5; depth++, current = current.parentElement) {
                                const text = normalize(current.textContent);
                                if (text) return text;
                            }
                            return '';
                        }"""
                    )
                    or ""
                )
            except Exception:
                label = ""

            if "tools" in label:
                tools_switches.append(switch)
            else:
                visible_switches.append(switch)

        target_switches = tools_switches or visible_switches
        if not target_switches:
            return "missing"

        for switch in target_switches:
            try:
                checked = str(await switch.get_attribute("aria-checked") or "").strip().lower()
            except Exception:
                checked = ""
            is_enabled = checked == "true"

            if is_enabled == wanted:
                return "already"

            try:
                aria_disabled = str(await switch.get_attribute("aria-disabled") or "").strip().lower()
            except Exception:
                aria_disabled = ""

            if aria_disabled == "true":
                if wanted:
                    self._tools_disabled_for_request = True
                    self.current_tools_enabled = False
                    Logger.warning("QwenLM: Tools switch is disabled in the + menu; leaving it off.")
                elif is_enabled:
                    Logger.warning("QwenLM: Tools switch is enabled but disabled in the + menu.")
                return "disabled"

            try:
                await switch.click(timeout=3000)
            except Exception:
                try:
                    await switch.evaluate("el => el.click()")
                except Exception as e:
                    action = "enable" if wanted else "disable"
                    Logger.warning(f"QwenLM: failed to {action} Tools in the + menu: {e}")
                    return "failed"

            await asyncio.sleep(0.15)

            try:
                checked_after = str(await switch.get_attribute("aria-checked") or "").strip().lower()
            except Exception:
                checked_after = ""
            enabled_after = checked_after == "true"

            if enabled_after != wanted:
                action = "enabled" if wanted else "disabled"
                Logger.warning(f"QwenLM: Tools switch did not settle to {action} in the + menu.")
                return "mismatch"

            Logger.debug(f"QwenLM: {'enabled' if wanted else 'disabled'} Tools in the + menu.")
            return "changed"

        return "missing"

    async def _sync_tools_switch_in_mode_select_menu(self, menu=None) -> str:
        return await self._set_tools_switch_in_mode_select_menu(self._desired_tools_state(), menu=menu)

    async def _disable_tools_switch_in_mode_select_menu(self, menu=None) -> bool:
        result = await self._set_tools_switch_in_mode_select_menu(False, menu=menu)
        return result in {"already", "changed", "disabled", "missing"}

    async def _open_mode_select_dropdown_menu_root(self, *, sync_tools: bool = True):
        if not self.page:
            return None

        open_trigger = self.page.locator(self.MODE_SELECT_TRIGGER_OPEN_SELECTOR)
        base_trigger = self.page.locator(self.MODE_SELECT_TRIGGER_SELECTOR)
        any_trigger = self.page.locator(self.MODE_SELECT_TRIGGER_ANY_SELECTOR)

        clicked_target = None
        candidates = []
        if await open_trigger.count() > 0:
            candidates.append(open_trigger.locator("xpath=.."))
            candidates.append(open_trigger)
        if await base_trigger.count() > 0:
            candidates.append(base_trigger.locator("xpath=.."))
            candidates.append(base_trigger)
        if not candidates and await any_trigger.count() > 0:
            candidates.append(any_trigger)

        clicked = False
        for candidate in candidates:
            if await self._click_first_visible_playwright_click(candidate, timeout_ms=3000):
                clicked_target = candidate
                clicked = True
                break
            if await self._click_first_visible(candidate, timeout_ms=3000):
                clicked_target = candidate
                clicked = True
                break

        if not clicked:
            return None

        for attempt in range(2):
            try:
                await self.page.wait_for_selector(self.MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR, timeout=1200)
                break
            except Exception:
                if attempt != 0:
                    continue
                try:
                    fallback_target = clicked_target or any_trigger
                    if await fallback_target.count() == 0:
                        continue
                    await fallback_target.first.evaluate(
                        """(el) => {
                            if (!el) return;
                            const rect = el.getBoundingClientRect();
                            const x = rect.left + rect.width / 2;
                            const y = rect.top + rect.height / 2;
                            const top = document.elementFromPoint(x, y);
                            try { top && top.click(); } catch (e) {}
                        }"""
                    )
                except Exception:
                    pass

        try:
            await self.page.wait_for_selector(self.MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR, timeout=2500)
        except Exception:
            try:
                await self.page.wait_for_selector("ul.ant-dropdown-menu-root", timeout=2500)
            except Exception:
                pass

        menu = self.page.locator(self.MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR)
        if await menu.count() == 0:
            menu = self.page.locator("ul.ant-dropdown-menu-root")
        if await menu.count() == 0:
            return None

        if sync_tools:
            try:
                await self._sync_tools_switch_in_mode_select_menu(menu)
            except Exception:
                pass

        return menu

    async def _open_mode_select_common_submenu(self, *, sync_tools: bool = True) -> bool:
        if not self.page:
            return False

        open_trigger = self.page.locator(self.MODE_SELECT_TRIGGER_OPEN_SELECTOR)
        base_trigger = self.page.locator(self.MODE_SELECT_TRIGGER_SELECTOR)
        any_trigger = self.page.locator(self.MODE_SELECT_TRIGGER_ANY_SELECTOR)

        clicked_target = None
        candidates = []
        if await open_trigger.count() > 0:
            candidates.append(open_trigger.locator("xpath=.."))
            candidates.append(open_trigger)
        if await base_trigger.count() > 0:
            candidates.append(base_trigger.locator("xpath=.."))
            candidates.append(base_trigger)
        if not candidates and await any_trigger.count() > 0:
            candidates.append(any_trigger)

        clicked = False
        for candidate in candidates:
            if await self._click_first_visible_playwright_click(candidate, timeout_ms=3000):
                clicked_target = candidate
                clicked = True
                break
            if await self._click_first_visible(candidate, timeout_ms=3000):
                clicked_target = candidate
                clicked = True
                break

        if not clicked:
            return False

        # If the menu didn't render, try clicking the actual topmost element at the
        # trigger's center point (helps when the click handler is on a child element)
        try:
            await self.page.wait_for_selector(self.MODE_SELECT_COMMON_SUBMENU_SELECTOR, timeout=1200)
        except Exception:
            try:
                fallback_target = clicked_target or any_trigger
                if await fallback_target.count() == 0:
                    return False
                await fallback_target.first.evaluate(
                    """(el) => {
                        if (!el) return;
                        const rect = el.getBoundingClientRect();
                        const x = rect.left + rect.width / 2;
                        const y = rect.top + rect.height / 2;
                        const top = document.elementFromPoint(x, y);
                        try { top && top.click(); } catch (e) {}
                    }"""
                )
            except Exception:
                pass

        try:
            await self.page.wait_for_selector(self.MODE_SELECT_COMMON_SUBMENU_SELECTOR, timeout=3000)
        except Exception:
            pass

        if sync_tools:
            try:
                menu = self.page.locator(self.MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR)
                if await menu.count() == 0:
                    menu = self.page.locator("ul.ant-dropdown-menu-root")
                if await menu.count() > 0:
                    await self._sync_tools_switch_in_mode_select_menu(menu)
            except Exception:
                pass

        submenu = self.page.locator(self.MODE_SELECT_COMMON_SUBMENU_SELECTOR)
        if await submenu.count() == 0:
            return False

        title = submenu.first.locator("div.ant-dropdown-menu-submenu-title")
        if await title.count() > 0:
            if await self._click_first_visible(title, timeout_ms=3000):
                return True
            try:
                await title.first.hover(timeout=1500)
                return True
            except Exception:
                return False

        return await self._click_first_visible(submenu, timeout_ms=3000)

    def _mode_select_item_selector(self, suffix: str) -> str:
        wanted = str(suffix or "").strip().lower()
        if not wanted:
            return self.MODE_SELECT_MENU_ITEM_SELECTOR
        return f"{self.MODE_SELECT_MENU_ITEM_SELECTOR}[data-menu-id$='-{wanted}']"

    async def _click_mode_select_item(self, suffix: str) -> str:
        if not self.page:
            return "missing"

        item_selector = self._mode_select_item_selector(suffix)
        try:
            await self.page.wait_for_selector(item_selector, timeout=2500)
        except Exception:
            pass

        items = self.page.locator(item_selector)
        if await items.count() == 0:
            # Fallback: suffix match might not work if Qwen changes the id format.
            items = self.page.locator(
                f"{self.MODE_SELECT_MENU_ITEM_SELECTOR}[data-menu-id*='{str(suffix or '').strip().lower()}']"
            )

        clicked = False
        disabled = False
        count = 0
        try:
            count = await items.count()
        except Exception:
            count = 0

        for idx in range(count):
            candidate = items.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                continue

            try:
                aria_disabled = str(await candidate.get_attribute("aria-disabled") or "").strip().lower()
            except Exception:
                aria_disabled = ""

            try:
                disabled_attr = await candidate.get_attribute("disabled")
            except Exception:
                disabled_attr = None

            try:
                disabled_in_tree = bool(
                    await candidate.evaluate(
                        """(el) => {
                            if (!el) return false;
                            const className = (el.getAttribute('class') || '').toString().toLowerCase();
                            if (className.includes('disabled')) return true;
                            const nested = el.querySelector("[aria-disabled='true'], [disabled]");
                            return !!nested;
                        }"""
                    )
                )
            except Exception:
                disabled_in_tree = False

            if aria_disabled == "true" or disabled_attr is not None or disabled_in_tree:
                disabled = True
                break

            if await self._click_first_visible(candidate, timeout_ms=3000):
                clicked = True
                break

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        if clicked:
            return "clicked"
        if disabled:
            return "disabled"
        return "missing"

    async def _enable_search_via_mode_select_dropdown(self) -> str:
        if not self.page:
            return "missing"

        Logger.debug("QwenLM: enabling search via mode-select dropdown...")

        try:
            await self.page.wait_for_selector(self.MODE_SELECT_TRIGGER_ANY_SELECTOR, timeout=4000)
        except Exception:
            pass

        if not await self._open_mode_select_common_submenu():
            Logger.debug("QwenLM: mode-select submenu not found/opened.")
            return "missing"

        result = await self._click_mode_select_item("search")
        if result == "disabled":
            Logger.warning("QwenLM: Search is visible but disabled in the + menu.")
        elif result != "clicked":
            Logger.debug("QwenLM: mode-select search item not found/clicked.")
        return result

    async def _prime_upload_flow_via_mode_select_dropdown(self) -> bool:
        """
        Prime Qwen's upload flow without opening a native file chooser dialog.

        Qwen wires file uploads through the mode-select dropdown. Clicking the Upload item
        (a root dropdown entry, not a submenu item) can trigger a file picker. We suppress
        that picker by temporarily overriding the input's .click() method while still
        letting Qwen's handler run.
        """
        if not self.page:
            return False

        try:
            primed = await self.page.evaluate(
                "async (triggerSel, menuSel, itemSel, toolsSwitchSel, wantedTools) => {"
                "  const sleep = (ms) => new Promise(r => setTimeout(r, ms));"
                "  const clickEl = (el) => {"
                "    if (!el) return false;"
                "    try { el.click(); return true; } catch (e) {}"
                "    try { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); return true; } catch (e) {}"
                "    return false;"
                "  };"
                "  const syncTools = (root) => {"
                "    const switches = Array.from((root || document).querySelectorAll(toolsSwitchSel));"
                "    if (switches.length === 0 && root !== document) {"
                "      switches.push(...Array.from(document.querySelectorAll(toolsSwitchSel)));"
                "    }"
                "    const visible = switches.filter((el) => {"
                "      const rect = el.getBoundingClientRect();"
                "      return rect.width > 0 && rect.height > 0;"
                "    });"
                "    const target = visible.find((el) => {"
                "      let current = el;"
                "      for (let depth = 0; current && depth < 5; depth++, current = current.parentElement) {"
                "        const text = (current.textContent || '').toString().toLowerCase();"
                "        if (text.includes('tools')) return true;"
                "      }"
                "      return false;"
                "    }) || visible[0];"
                "    if (!target) return;"
                "    const checked = (target.getAttribute('aria-checked') || '').toLowerCase() === 'true';"
                "    const disabled = (target.getAttribute('aria-disabled') || '').toLowerCase() === 'true';"
                "    if (checked !== !!wantedTools && !disabled) clickEl(target);"
                "  };"
                ""
                "  const trigger = document.querySelector(triggerSel);"
                "  if (!trigger) return false;"
                "  const triggerTarget = trigger.parentElement || trigger;"
                "  clickEl(triggerTarget);"
                ""
                "  const deadline = Date.now() + 3000;"
                ""
                "  while (Date.now() < deadline) {"
                "    const menu = document.querySelector(menuSel) || document.querySelector('ul.ant-dropdown-menu-root');"
                "    if (menu) {"
                "      break;"
                "    }"
                "    await sleep(50);"
                "  }"
                ""
                "  const menu = document.querySelector(menuSel) || document.querySelector('ul.ant-dropdown-menu-root');"
                "  if (!menu) return false;"
                "  syncTools(menu);"
                ""
                "  while (Date.now() < deadline) {"
                "    const items = Array.from(menu.querySelectorAll(itemSel));"
                "    const target = items.find((el) => {"
                "      const id = (el.getAttribute('data-menu-id') || '').toString();"
                "      return id.endsWith('-upload') || id.includes('upload');"
                "    });"
                "    if (target) {"
                "      const orig = HTMLInputElement.prototype.click;"
                "      HTMLInputElement.prototype.click = function() {};"
                "      try {"
                "        clickEl(target);"
                "        return true;"
                "      } catch (e) {"
                "        return false;"
                "      } finally {"
                "        HTMLInputElement.prototype.click = orig;"
                "      }"
                "    }"
                "    await sleep(50);"
                "  }"
                ""
                "  return false;"
                "}",
                self.MODE_SELECT_TRIGGER_ANY_SELECTOR,
                self.MODE_SELECT_DROPDOWN_MENU_ROOT_SELECTOR,
                self.MODE_SELECT_MENU_ITEM_SELECTOR,
                self.MODE_SELECT_TOOLS_SWITCH_SELECTOR,
                self._desired_tools_state(),
            )
        except Exception:
            primed = False

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        return bool(primed)

    async def _enable_search(self) -> str:
        if not self.page:
            return "missing"

        # use the mode-select dropdown (more stable than the old anchor trick)
        # and it doesn't bug out in most cases
        dropdown_result = await self._enable_search_via_mode_select_dropdown()
        if dropdown_result in {"clicked", "disabled"}:
            return dropdown_result

        try:
            result = await self.page.evaluate(
                "(enableAnchor, currentModeSelector) => {"
                "  const clickAncestors = (span) => {"
                "    if (!span) return 'missing';"
                "    const isDisabled = (node) => {"
                "      let current = node;"
                "      for (let i = 0; current && i < 5; i++, current = current.parentElement) {"
                "        const ariaDisabled = (current.getAttribute && current.getAttribute('aria-disabled') || '').toString().toLowerCase();"
                "        if (ariaDisabled === 'true') return true;"
                "        if (current.disabled === true) return true;"
                "      }"
                "      return false;"
                "    };"
                "    if (isDisabled(span)) return 'disabled';"
                "    let el = span;"
                "    for (let i = 0; i < 3; i++) {"
                "      if (el.parentElement) el = el.parentElement;"
                "    }"
                "    if (isDisabled(el)) return 'disabled';"
                "    try { el.click(); return 'clicked'; } catch (e) {}"
                "    try { span.click(); return 'clicked'; } catch (e) {}"
                "    try { span.dispatchEvent(new MouseEvent('click', { bubbles: true })); return 'clicked'; } catch (e) {}"
                "    return 'missing';"
                "  };"
                ""
                "  try {"
                "    const byAnchor = document.querySelector(`span[data-spm-anchor-id='${enableAnchor}']`);"
                "    const byAnchorResult = clickAncestors(byAnchor);"
                "    if (byAnchorResult === 'clicked' || byAnchorResult === 'disabled') return byAnchorResult;"
                "  } catch (e) {}"
                ""
                "  const isInsideCurrent = (node) => {"
                "    try { return !!(node && node.closest && node.closest(currentModeSelector)); }"
                "    catch (e) { return false; }"
                "  };"
                ""
                "  const spans = Array.from(document.querySelectorAll('span[data-spm-anchor-id]'));"
                "  for (const s of spans) {"
                "    if (!s) continue;"
                "    if (isInsideCurrent(s)) continue;"
                "    const raw = (s.textContent || '').toString().trim().toLowerCase();"
                "    if (!raw) continue;"
                "    const text = raw.replace(/\\s+/g, ' ');"
                "    if (text === 'web search' || text.includes('web search') || (text.includes('web') && text.includes('search'))) {"
                "      const result = clickAncestors(s);"
                "      if (result === 'clicked' || result === 'disabled') return result;"
                "    }"
                "  }"
                ""
                "  return 'missing';"
                "}",
                self.SEARCH_ENABLE_ANCHOR,
                self.SEARCH_MODE_CONTAINER_SELECTOR,
            )
        except Exception:
            result = "missing"

        if result == "disabled":
            Logger.warning("QwenLM: Search is visible but disabled in the composer.")
        return str(result or "missing")

    async def _disable_search(self) -> bool:
        if not self.page:
            return False

        btn = self.page.locator(self.SEARCH_CLOSE_SELECTOR)
        if await btn.count() == 0:
            return False
        try:
            await btn.first.click(timeout=3000)
            return True
        except Exception:
            return False

    async def set_search_state(self, state: bool) -> None:
        wanted = bool(state)

        for _ in range(8):
            before = await self._is_search_enabled()
            if before == wanted:
                return

            if wanted:
                result = await self._enable_search()
                if result == "disabled":
                    Logger.warning("QwenLM: Search cannot be enabled because Qwen disabled the control.")
                    return
            else:
                await self._disable_search()

            await asyncio.sleep(0.25)

        after = await self._is_search_enabled()
        if after != wanted:
            Logger.warning(f"QwenLM: Search state mismatch after toggle (wanted={wanted}, actual={after}).")

    async def set_tools_state(self, state: bool) -> None:
        if not self.page:
            return

        wanted = bool(state)
        self.current_tools_enabled = wanted
        last_result = "missing"

        for attempt in range(2):
            menu = await self._open_mode_select_dropdown_menu_root(sync_tools=False)
            if menu is None:
                if wanted:
                    Logger.warning("QwenLM: Tools switch menu was not found.")
                return

            try:
                last_result = await self._set_tools_switch_in_mode_select_menu(wanted, menu=menu)
            finally:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass

            if last_result in {"already", "changed", "disabled"}:
                return
            if not wanted and last_result == "missing":
                return
            if attempt == 0:
                await asyncio.sleep(0.15)

        if wanted:
            Logger.warning(f"QwenLM: Tools could not be enabled ({last_result}).")

    async def upload_file(self, file_spec: Any) -> None:
        await self._upload_file(file_spec)

    async def _upload_file(self, file_spec: Any) -> None:
        if not self.page:
            return

        temp_dir: str | None = None
        temp_path: str | None = None

        def _materialize_payload(payload: dict) -> str | None:
            nonlocal temp_dir, temp_path
            try:
                buffer = payload.get("buffer")
                if not isinstance(buffer, (bytes, bytearray)) or not buffer:
                    return None

                name = str(payload.get("name") or "").strip()
                if name:
                    name = name.replace("\\", "/")
                safe_name = os.path.basename(name) if name else ""
                safe_name = safe_name.strip() or "upload.bin"

                temp_dir = tempfile.mkdtemp(prefix="irp-qwen-upload-")
                temp_path = os.path.join(temp_dir, safe_name)
                with open(temp_path, "wb") as f:
                    f.write(buffer)
                return temp_path
            except Exception:
                return None

        try:
            try:
                if isinstance(file_spec, dict):
                    name = file_spec.get("name", "<payload>")
                    buffer = file_spec.get("buffer")
                    size = len(buffer) if isinstance(buffer, (bytes, bytearray)) else None
                    size_info = f" ({size} bytes)" if size is not None else ""
                    Logger.debug(f"QwenLM: uploading file payload: {name}{size_info}")
                else:
                    Logger.debug(f"QwenLM: uploading file: {file_spec}")
            except Exception:
                Logger.debug("QwenLM: uploading file (details unavailable).")

            uploaded_via_chooser = False

            # click the root Upload item (no submenu) so Qwen wires up its upload callbacks,
            # then answer the file chooser programmatically
            try:
                Logger.debug("QwenLM: attempting upload via mode-select file chooser...")
                try:
                    await self.page.wait_for_selector(self.MODE_SELECT_TRIGGER_ANY_SELECTOR, timeout=4000)
                except Exception:
                    pass

                menu_root = await self._open_mode_select_dropdown_menu_root()
                if menu_root is not None:
                    upload_item = menu_root.locator(self._mode_select_item_selector("upload"))
                    if await upload_item.count() == 0:
                        upload_item = menu_root.locator(
                            f"{self.MODE_SELECT_MENU_ITEM_SELECTOR}[data-menu-id$='-upload']"
                        )
                    if await upload_item.count() == 0:
                        upload_item = menu_root.locator(
                            f"{self.MODE_SELECT_MENU_ITEM_SELECTOR}[data-menu-id*='upload']"
                        )

                    if await upload_item.count() > 0:
                        async with self.page.expect_file_chooser(timeout=6000) as fc_info:
                            if not await self._click_first_visible_playwright_click(upload_item, timeout_ms=3000):
                                raise RuntimeError("Upload menu item was not clickable.")
                        chooser = await fc_info.value

                        chooser_files = file_spec
                        if isinstance(file_spec, dict):
                            temp_candidate = _materialize_payload(file_spec)
                            if temp_candidate:
                                chooser_files = temp_candidate

                        await chooser.set_files(chooser_files)
                        uploaded_via_chooser = True
                    else:
                        Logger.debug("QwenLM: mode-select Upload item not found in dropdown menu.")
                else:
                    Logger.debug("QwenLM: mode-select dropdown menu root not found (upload).")
            except Exception as e:
                Logger.debug(f"QwenLM: mode-select file chooser upload failed: {e}")
            finally:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass

            if uploaded_via_chooser:
                try:
                    file_count = await self.page.evaluate(
                        "() => {"
                        "  const input = document.querySelector('#filesUpload') || document.querySelector(\"input[type='file']\");"
                        "  if (!input || !input.files) return 0;"
                        "  return input.files.length || 0;"
                        "}"
                    )
                    Logger.debug(f"QwenLM: file chooser set {int(file_count or 0)} file(s) on input.")
                except Exception:
                    pass
            else:
                Logger.debug("QwenLM: falling back to direct set_input_files() upload...")

                file_input = self.page.locator(self.FILE_INPUT_SELECTOR)
                if await file_input.count() == 0:
                    try:
                        await self.page.wait_for_selector(self.FILE_INPUT_SELECTOR, timeout=8000)
                    except Exception:
                        pass
                    file_input = self.page.locator(self.FILE_INPUT_SELECTOR)

                if await file_input.count() == 0:
                    file_input = self.page.locator("input[type='file']")
                    if await file_input.count() == 0:
                        try:
                            await self.page.wait_for_selector("input[type='file']", timeout=8000)
                        except Exception:
                            pass
                        file_input = self.page.locator("input[type='file']")

                if await file_input.count() == 0:
                    Logger.warning("QwenLM: file input not found.")
                    return

                async def _apply_files() -> bool:
                    try:
                        await file_input.first.set_input_files(file_spec)
                    except Exception as e:
                        Logger.warning(f"QwenLM: file upload failed: {e}")
                        return False

                    try:
                        await self.page.evaluate(
                            "() => {"
                            "  const inputs = Array.from(document.querySelectorAll(\"input[type='file']\"));"
                            "  for (const input of inputs) {"
                            "    try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}"
                            "    try { input.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}"
                            "  }"
                            "}"
                        )
                    except Exception:
                        pass

                    return True

                async def _send_button_exists() -> bool:
                    btn = self.page.locator(self.SEND_BUTTON_SELECTOR)
                    try:
                        return (await btn.count() > 0) and (await btn.first.is_visible())
                    except Exception:
                        return False

                if not await _apply_files():
                    return

                appeared = False
                for _ in range(20):
                    if await _send_button_exists():
                        appeared = True
                        break
                    await asyncio.sleep(0.1)

                if not appeared:
                    Logger.debug("QwenLM: upload did not register via direct input. Priming upload flow and retrying...")
                    try:
                        await self._prime_upload_flow_via_mode_select_dropdown()
                    except Exception:
                        pass

                    file_input = self.page.locator(self.FILE_INPUT_SELECTOR)
                    if await file_input.count() == 0:
                        file_input = self.page.locator("input[type='file']")
                    if await file_input.count() == 0:
                        Logger.warning("QwenLM: file input not found after priming upload flow.")
                        return

                    if not await _apply_files():
                        return

            # Wait for upload spinner to complete (must appear at least once)
            spinner = self.page.locator(".circle-spinner.vision-spinner")
            appeared = False
            for _ in range(60):
                try:
                    if await spinner.count() > 0 and await spinner.first.is_visible():
                        appeared = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.1)

            if appeared:
                for _ in range(600):
                    try:
                        if await spinner.count() == 0:
                            break
                        if not await spinner.first.is_visible():
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.1)
        finally:
            if temp_dir:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

    async def enter_message(self, message: str) -> None:
        await self._enter_message(message)

    async def send_message(self, timeout: int | None = None) -> None:
        await self._send_message(timeout=timeout)

    async def _enter_message(self, message: str) -> None:
        if not self.page:
            return

        textarea = self.page.locator(self.CHAT_TEXTAREA_SELECTOR)
        if await textarea.count() == 0:
            textarea = self.page.locator("textarea")
        if await textarea.count() == 0:
            Logger.warning("QwenLM: message textarea not found.")
            return

        preview = message[:50] + "..." if len(message) > 50 else message
        Logger.debug(f"QwenLM: entering message: {preview}")
        await textarea.first.fill(message)

    async def _send_message(self, timeout: int | None = None, arm_event: asyncio.Event | None = None) -> None:
        if not self.page:
            return

        if arm_event:
            try:
                arm_event.set()
            except Exception:
                pass

        max_wait_s = 0 if not timeout else max(int(timeout), 0)
        start = time.time()
        last_error: Optional[Exception] = None

        while True:
            btn = self.page.locator(self.SEND_BUTTON_SELECTOR)
            try:
                if await btn.count() > 0 and await btn.first.is_visible():
                    disabled_attr = None
                    try:
                        disabled_attr = await btn.first.get_attribute("disabled")
                    except Exception:
                        disabled_attr = None

                    aria_disabled = ""
                    try:
                        aria_disabled = str(await btn.first.get_attribute("aria-disabled") or "").strip().lower()
                    except Exception:
                        aria_disabled = ""

                    if disabled_attr is not None or aria_disabled == "true":
                        raise RuntimeError("Send button is disabled.")

                    is_enabled = True
                    try:
                        is_enabled = await btn.first.is_enabled()
                    except Exception:
                        is_enabled = True

                    if is_enabled:
                        await btn.first.click(timeout=3000)
                        return
            except Exception as e:
                last_error = e

            if max_wait_s <= 0:
                break
            if time.time() - start >= max_wait_s:
                break
            await asyncio.sleep(0.1)

        if last_error:
            Logger.warning(f"QwenLM: failed to click send button: {last_error}")
        else:
            Logger.warning("QwenLM: send button not found (it appears only after input/upload).")

    async def _click_regenerate(self, arm_event: asyncio.Event | None = None) -> bool:
        if not self.page:
            return False

        if arm_event:
            try:
                arm_event.set()
            except Exception:
                pass

        btn = self.page.locator("div.qwen-chat-package-comp-new-action-control-container-regenerate")
        count = 0
        try:
            count = await btn.count()
        except Exception:
            count = 0

        for idx in range(count - 1, -1, -1):
            candidate = btn.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
                await candidate.click(timeout=3000)
                return True
            except Exception:
                continue

        return False

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        return format_request_messages(self.config_manager, messages)

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "qwen-auto",
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

        await self.require_english_ui()
        self._refresh_quirks()
        try:
            await self._ensure_rp_friendly_settings()
        except Exception:
            pass

        self.thinking_active = False
        self.abort_requested = False
        self._tools_disabled_for_request = False
        self.current_abort_event = abort_event
        resolved_model = (model or "").strip() or "qwen-auto"
        self.current_model = resolved_model

        macros_overrides: Dict[str, bool] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = self._strip_qwen_macros_from_messages(message)
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = self._extract_qwen_macros_from_text(message)

        if macros_overrides:
            Logger.debug(f"QwenLM macros applied: {macros_overrides}")

        effective_settings = self._resolve_qwen_request_settings(resolved_model, overrides=macros_overrides)
        effective_deepthink = effective_settings["deepthink_enabled"]
        effective_send_deepthink = effective_settings["send_deepthink"]
        enable_search = effective_settings["search_enabled"]
        enable_tools = effective_settings["tools_enabled"]
        send_as_text_file = effective_settings["send_as_text_file"]
        ui_model_label = str(
            effective_settings.get("model_label") or self._get_qwen_model_label_for_request(resolved_model)
        )
        self.current_send_deepthink = effective_send_deepthink
        self.current_tools_enabled = bool(enable_tools)

        formatted_message = self._format_messages(message_for_formatting)
        qwen_extra_prompt_texts: Dict[str, str] = {}
        if send_as_text_file:
            try:
                qwen_text_file_message = str(
                    self.config_manager.get_setting("qwen_behavior", "text_file_message") or ""
                )
            except Exception:
                qwen_text_file_message = ""
            if qwen_text_file_message.strip():
                qwen_extra_prompt_texts["text_file_message"] = qwen_text_file_message
        self._capture_diagnostics_prompt_snapshot(
            formatted_message,
            extra_prompt_texts=qwen_extra_prompt_texts or None,
            metadata={
                "model": resolved_model,
                "ui_model": ui_model_label,
                "deepthink_enabled": bool(effective_deepthink),
                "send_deepthink": bool(effective_send_deepthink),
                "search_enabled": bool(enable_search),
                "tools_enabled": bool(enable_tools),
                "send_as_text_file": bool(send_as_text_file),
            },
        )

        try:
            count_tokens_setting = self.config_manager.get_setting("qwen_behavior", "count_tokens")
        except Exception:
            count_tokens_setting = None
        count_tokens_enabled = True if count_tokens_setting is None else bool(count_tokens_setting)
        stream_consumer = _QwenStreamConsumer(
            driver=self,
            response_queue=response_queue,
            count_tokens_enabled=count_tokens_enabled,
        )

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
        intercepted_request_finished = asyncio.Event()
        provider_activity_count = 0

        def get_provider_activity_count() -> int:
            return provider_activity_count

        async def handle_route(route):
            nonlocal completion_claimed, provider_activity_count
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

            Logger.info("Intercepting QwenLM API request...")
            Logger.debug(f"Intercepted request to: {request_url}")

            headers = await request.all_headers()
            headers.pop("content-length", None)
            headers.pop("host", None)

            cookies = await self.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            response_headers: Dict[str, str] = {}
            full_response_body = bytearray()
            aborted = False

            json_body = None
            raw_post_data = request.post_data
            if raw_post_data:
                try:
                    json_body = json.loads(raw_post_data)
                except Exception:
                    json_body = None

            try:
                client = await self._get_http_client()
                async with client.stream(
                    "POST",
                    request_url,
                    headers=headers,
                    cookies=cookie_dict,
                    json=json_body,
                    timeout=90.0,
                ) as response:
                    for k, v in response.headers.items():
                        response_headers[k] = v

                    async for chunk in response.aiter_bytes():
                        if chunk:
                            provider_activity_count += 1
                        if self.abort_requested or (abort_event and abort_event.is_set()):
                            Logger.debug("Abort detected during QwenLM streaming, stopping...")
                            aborted = True
                            break

                        full_response_body.extend(chunk)
                        stream_consumer.feed_bytes(chunk)
                        if stream_consumer.encountered_error:
                            break

                    stream_consumer.finish()

            except httpx.ReadError as e:
                if not aborted and not self.abort_requested:
                    Logger.error(f"QwenLM: read error during intercepted request: {e}")
                    await response_queue.put({"error": str(e)})
            except Exception as e:
                if not aborted and not self.abort_requested:
                    Logger.error(f"QwenLM: error during intercepted request: {e}")
                    await response_queue.put({"error": str(e)})

            if aborted or self.abort_requested:
                Logger.warning("QwenLM generation aborted by user.")

            try:
                await route.fulfill(body=bytes(full_response_body), status=200, headers=response_headers)
            except Exception as e:
                Logger.error(f"QwenLM: error fulfilling route: {e}")

            await response_queue.put(None)
            intercepted_request_finished.set()
            if not aborted and not stream_consumer.encountered_error and not self.abort_requested:
                Logger.success("QwenLM response streaming completed.")

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"QwenLM: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"QwenLM: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        def request_aborted() -> bool:
            return bool(self.abort_requested or (abort_event and abort_event.is_set()))

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
            stream_consumer.finish()
            await response_queue.put(None)
            intercepted_request_finished.set()
            request_methods.pop(request_id, None)
            cdp_pending_response_urls.pop(request_id, None)

            if (
                not aborted
                and not encountered_error
                and not stream_consumer.encountered_error
                and not request_aborted()
            ):
                Logger.success("QwenLM CDP stream completed.")

        async def feed_cdp_stream_chunk(request_id: str, data: bytes) -> None:
            nonlocal provider_activity_count, cdp_had_data
            if request_id != cdp_active_request_id or not data or cdp_stream_finished:
                return

            cdp_had_data = True
            provider_activity_count += 1
            if request_aborted():
                await finish_cdp_stream(request_id, aborted=True)
                return

            stream_consumer.feed_bytes(data)
            if stream_consumer.encountered_error:
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
            Logger.info("Teeing QwenLM API response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message_text = f"QwenLM CDP response streaming failed: {exc}"
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

            Logger.info("Observing QwenLM API request via CDP...")
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
                    "QwenLM CDP stream ended with net::ERR_ABORTED after data arrived; "
                    "treating it as complete."
                )
                await finish_cdp_stream(request_id)
                return

            message_text = f"QwenLM CDP stream failed: {error_text}"
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
                    message_text = "QwenLM CDP setup failed: browser context is not available."
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
                    Logger.info("QwenLM Request Capture Mode: CDP Teeing.")
                except Exception as exc:
                    message_text = f"QwenLM CDP setup failed: {exc}"
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
            else:
                await self.page.route(self.COMPLETIONS_ROUTE_GLOB, handle_route)
                route_handlers_registered = True
                Logger.info("QwenLM Request Capture Mode: Replay.")

            clean_regeneration = bool(self.config_manager.get_setting("qwen_behavior", "clean_regeneration"))
            multi_slot_cache_enabled = bool(
                clean_regeneration
                and self.config_manager.get_setting("qwen_behavior", "multi_slot_cache")
            )
            try:
                auto_delete_requested = bool(
                    self.config_manager.get_setting("qwen_behavior", "auto_delete_chats")
                )
            except Exception:
                auto_delete_requested = False
            auto_delete_enabled = bool(auto_delete_requested and (not clean_regeneration))
            if auto_delete_requested and clean_regeneration:
                Logger.warning(
                    "QwenLM: Delete Chat After Reply is skipped for this request because "
                    "Reuse Matching Chat is enabled."
                )
            regenerated = False
            clean_regen_state: Dict[str, Any] | None = None
            multi_slot_state: Dict[str, Any] | None = None
            current_cache_matched = False
            should_record_multi_slot = False

            if clean_regeneration:
                clean_regen_state = {
                    "deepthink_enabled": bool(effective_deepthink),
                    "search_enabled": bool(enable_search),
                    "tools_enabled": bool(enable_tools),
                    "send_as_text_file": bool(send_as_text_file),
                    "ui_model": ui_model_label,
                }
                multi_slot_state = self._build_multi_slot_cache_state(
                    effective_deepthink=bool(effective_deepthink),
                    enable_search=bool(enable_search),
                    enable_tools=bool(enable_tools),
                    send_as_text_file=bool(send_as_text_file),
                    ui_model_label=ui_model_label,
                )

                last_message = self.cache_manager.read_cache(self.clean_regen_message_cache_key)
                last_state = self._read_clean_regeneration_state()

                message_matches = last_message == formatted_message
                state_matches = last_state == clean_regen_state

                if message_matches and state_matches:
                    current_cache_matched = True
                    Logger.info(
                        "Clean Regeneration (QwenLM): Message and settings match cache. Attempting to regenerate..."
                    )

                    try:
                        await self.set_tools_state(enable_tools)
                        await self.set_deepthink_state(effective_deepthink)
                        await self.set_search_state(enable_search)
                        await asyncio.sleep(0.25)
                    except Exception:
                        pass

                    if await self._click_regenerate(arm_event=completion_armed):
                        Logger.info("Clean Regeneration (QwenLM): Regenerate clicked. Regenerating...")
                        try:
                            await asyncio.wait_for(
                                completion_started.wait(),
                                timeout=self._completion_request_timeout_s,
                            )
                        except asyncio.TimeoutError:
                            Logger.warning(
                                "Clean Regeneration (QwenLM): completion request not observed after clicking "
                                "Regenerate. Falling back to new chat."
                            )
                        else:
                            regenerated = True
                            self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                            self._write_clean_regeneration_state(clean_regen_state)
                    else:
                        Logger.warning(
                            "Clean Regeneration (QwenLM): Regenerate button not found/visible. Falling back to new chat."
                        )

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
                Logger.info("QwenLM: preparing new chat session...")
                await self.click_new_chat(source="auto")
                await asyncio.sleep(0.35)

                await self.apply_configured_model(model=resolved_model)

                await self.set_tools_state(enable_tools)
                await self.set_deepthink_state(effective_deepthink)
                await self.set_search_state(enable_search)
                await asyncio.sleep(0.25)

                if send_as_text_file:
                    Logger.info("QwenLM: sending message as text file...")
                    file_payload = build_prompt_text_file_payload(formatted_message)
                    await self._upload_file(file_payload)

                    try:
                        file_message = str(
                            self.config_manager.get_setting("qwen_behavior", "text_file_message") or ""
                        )
                    except Exception:
                        file_message = ""

                    if file_message.strip():
                        await self._enter_message(file_message)
                        await asyncio.sleep(0.1)

                    upload_timeout = int(self.config_manager.get_setting("qwen_behavior", "file_upload_timeout") or 20)
                    Logger.info("QwenLM: sending request...")
                    await self._send_message(timeout=upload_timeout, arm_event=completion_armed)
                else:
                    await self._enter_message(formatted_message)
                    await asyncio.sleep(0.1)
                    msg_send_timeout = int(self.config_manager.get_setting("qwen_behavior", "message_send_timeout") or 8)
                    Logger.info("QwenLM: sending request...")
                    await self._send_message(timeout=msg_send_timeout, arm_event=completion_armed)

                if clean_regeneration and clean_regen_state:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(clean_regen_state)
                    should_record_multi_slot = bool(multi_slot_cache_enabled and multi_slot_state)

            if not completion_started.is_set():
                try:
                    await asyncio.wait_for(
                        completion_started.wait(),
                        timeout=self._completion_request_timeout_s,
                    )
                except asyncio.TimeoutError:
                    Logger.error(
                        "QwenLM: completion request was not observed. "
                        "The UI may have swallowed the click or the endpoint changed."
                    )
                    yield f"data: {json.dumps({'error': 'QwenLM: completion request not observed'})}\n\n"
                    return

            stream_had_error = False
            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self._first_chunk_timeout_s,
                idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                on_timeout=self._abort_generation_ui,
                activity_counter=get_provider_activity_count,
            ):
                if isinstance(item, dict) and "error" in item:
                    stream_had_error = True
                    yield f"data: {json.dumps(item)}\n\n"
                    break

                yield item

            if self.abort_requested or (abort_event and abort_event.is_set()):
                await self._abort_generation_ui()

            if should_record_multi_slot and (not stream_had_error) and (not self.abort_requested):
                conversation_info = await self._wait_for_current_conversation_info(timeout_ms=6000)
                if conversation_info is None:
                    Logger.debug(
                        "Multi-Slot Cache (QwenLM): could not resolve conversation URL after "
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
                        log_label="Multi-Slot Cache (QwenLM)",
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
                    Logger.debug("QwenLM: timed out waiting for intercepted request cleanup.")
            self.current_abort_event = None
            self.abort_requested = False
            self.current_model = None
            self.current_send_deepthink = None
            self.current_tools_enabled = None
            self._tools_disabled_for_request = False
            if route_handlers_registered:
                try:
                    await self.page.unroute(self.COMPLETIONS_ROUTE_GLOB, handle_route)
                except Exception:
                    try:
                        await self.page.unroute(self.COMPLETIONS_ROUTE_GLOB)
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
                    Logger.debug(f"QwenLM: CDP detach failed: {exc}")
