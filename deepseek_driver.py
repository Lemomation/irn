import base64
import codecs
import time
import json
import asyncio
import re
import httpx
from typing import List, Union, Any, Dict, Callable, Optional
from urllib.parse import urlsplit
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
    remove_multi_slot_cache_entry,
    strip_macros_from_messages,
    upsert_multi_slot_cache_entry,
    read_multi_slot_cache_payload,
    write_clean_regeneration_state,
)
from utils.cache_manager import CacheManager
from utils.logger import Logger
from utils.model_ids import (
    DEEPSEEK_MODEL_TYPE_DEFAULT,
    DEEPSEEK_MODEL_TYPE_EXPERT,
    MODE_CHAT,
    MODE_REASONER,
    resolve_behavior_mode,
    resolve_deepseek_model_type,
)

load_dotenv()

class DeepSeekDriver(BaseDriver):
    REQUEST_CAPTURE_MODE_REPLAY = "replay"
    REQUEST_CAPTURE_MODE_CDP_TEEING = "cdp_teeing"
    INTERCEPT_FIRST_CHUNK_TIMEOUT_S = 45.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    COMPLETION_ROUTE_GLOB = "**/api/v0/chat/completion"
    REGENERATE_ROUTE_GLOB = "**/api/v0/chat/regenerate"
    COMPLETION_URL_PATHS = {
        "/api/v0/chat/completion",
        "/api/v0/chat/regenerate",
    }
    CONVERSATION_URL_RE = re.compile(r"^https://chat\.deepseek\.com/a/chat/s/([^/?#]+)", re.IGNORECASE)
    MODEL_TYPE_PICKER_NEW = "new-radio"
    MODEL_TYPE_PICKER_LEGACY = "legacy-inline"
    MODEL_TYPE_PICKER_ORDER = (MODEL_TYPE_PICKER_NEW, MODEL_TYPE_PICKER_LEGACY)
    MODEL_TYPE_PICKER_LABELS = {
        MODEL_TYPE_PICKER_NEW: "new radio picker",
        MODEL_TYPE_PICKER_LEGACY: "legacy inline picker",
    }
    # Extended model type values to try for "Instant" - website may have changed
    MODEL_TYPE_INSTANT_VALUES = (
        DEEPSEEK_MODEL_TYPE_DEFAULT,  # "default"
        "instant",
        "instant-v3",
    )
    MODEL_TYPE_PICKER_LABELS = {
        MODEL_TYPE_PICKER_NEW: "new radio picker",
        MODEL_TYPE_PICKER_LEGACY: "legacy inline picker",
    }
    FOLLOWUP_REQUEST_HEADER_ALLOWLIST = {
        "accept",
        "accept-language",
        "authorization",
        "content-type",
        "dnt",
        "origin",
        "referer",
        "user-agent",
    }
    REGENERATION_LIMIT_ERROR_MESSAGE = "DeepSeek: regeneration limit reached. Request aborted."
    CHAT_READY_SELECTORS = [
        "textarea[placeholder='Message DeepSeek']",
        "textarea",
    ]
    SIGN_IN_SELECTORS = [
        ".ds-sign-in-form__main",
        ".ds-sign-in-form-wrapper",
        ".ds-auth-form-wrapper",
        ".ds-sign-up-form__main",
        "input[autocomplete='current-password']",
        "input[type='password']",
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
    ]
    SEND_CONTROL_SELECTORS = [
        "[role='button'].ds-button._52c986b:visible",
        ".ds-button._52c986b.ds-button--circle:visible",
        "div.ds-icon-button._52c986b:visible",
    ]

    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.DEEPSEEK)
        self.cache_manager = CacheManager()

        self.current_model = None
        self.current_send_deepthink = None
        self.clean_regen_message_cache_key = "last_message.txt"
        self.clean_regen_state_cache_key = "last_message_state.json"
        self.multi_slot_cache_key = "deepseek_multi_slot_cache.json"
        self._last_generation_censored = False
        self._last_followup_request_headers: Dict[str, str] = {}
        self._deepseek_model_type_picker_kind: Optional[str] = None
        self._reset_stream_parser()

    def _reset_stream_parser(self) -> None:
        self._stream_text_decoder = codecs.getincrementaldecoder("utf-8")()
        self._stream_text_buffer = ""
        self._stream_text_buffer_pos = 0
        self._stream_active_fragment_type: Optional[str] = None
        self._stream_active_fragment_base_path: Optional[str] = None
        self._stream_provider_abort_requested = False

    def get_start_url(self) -> str:
        return "https://chat.deepseek.com/"

    async def after_start(self, status_callback: Optional[Callable[[str], None]] = None) -> None:
        # Detect DeepSeek UI language early (warn only; hard-requirement is enforced per-request)
        await self.check_ui_language(status_callback=status_callback)

        # Invalidate cache on start
        clear_clean_regeneration_cache(
            self.cache_manager,
            self.clean_regen_message_cache_key,
            self.clean_regen_state_cache_key,
        )
        try:
            await self._remember_send_control_signature(self._locate_send_control())
        except Exception:
            pass
        await self._detect_model_type_picker_kind(log_result=True)

    def _locate_send_control(self):
        selector = ", ".join(self.SEND_CONTROL_SELECTORS)
        return self.page.locator(selector).first

    async def _is_deepseek_control_disabled(self, locator: Any) -> bool:
        try:
            return bool(
                await locator.evaluate(
                    """(el) => {
                        const ariaDisabled = (el.getAttribute('aria-disabled') || '').trim().toLowerCase();
                        const classTokens = (el.getAttribute('class') || '').toString().split(/\\s+/);
                        const style = window.getComputedStyle(el);

                        return Boolean(
                            el.disabled ||
                            ariaDisabled === 'true' ||
                            el.hasAttribute('disabled') ||
                            el.hasAttribute('data-disabled') ||
                            classTokens.some((token) => token === 'disabled' || token.endsWith('--disabled')) ||
                            ((style.pointerEvents || '').toLowerCase() === 'none')
                        );
                    }"""
                )
            )
        except Exception:
            try:
                aria_disabled = str(await locator.get_attribute("aria-disabled") or "").strip().lower()
                disabled_attr = await locator.get_attribute("disabled")
                data_disabled = await locator.get_attribute("data-disabled")
                class_attr = await locator.get_attribute("class") or ""
                class_tokens = class_attr.split()
            except Exception:
                return False

            return (
                aria_disabled == "true"
                or disabled_attr is not None
                or data_disabled is not None
                or any(token == "disabled" or token.endswith("--disabled") for token in class_tokens)
            )

    async def _has_visible_selector(
        self,
        selectors: List[str],
        *,
        max_candidates: int = 8,
    ) -> bool:
        if not self.page:
            return False

        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
            except Exception:
                continue

            for idx in range(min(count, max_candidates)):
                try:
                    if await locator.nth(idx).is_visible():
                        return True
                except Exception:
                    continue

        return False

    async def _detect_deepseek_shell_state(self) -> str:
        if not self.page:
            return "unknown"

        try:
            url_lower = str(self.page.url or "").lower()
        except Exception:
            url_lower = ""

        if any(token in url_lower for token in ("sign_in", "signin", "sign-in")):
            return "auth"
        if await self._has_visible_selector(self.SIGN_IN_SELECTORS):
            return "auth"
        if await self._has_visible_selector(self.CHAT_READY_SELECTORS):
            return "chat"
        return "unknown"

    async def _wait_for_deepseek_shell_state(
        self,
        timeout_ms: int = 60000,
        poll_interval_s: float = 0.25,
    ) -> str:
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)

        while True:
            state = await self._detect_deepseek_shell_state()
            if state != "unknown":
                return state

            if time.time() >= deadline:
                return "unknown"

            await asyncio.sleep(max(0.05, float(poll_interval_s)))

    async def login(self):
        """
        Handles the login process if redirected to the sign-in page.
        """
        try:
            await self.page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass

        shell_state = await self._wait_for_deepseek_shell_state(timeout_ms=60000)
        if shell_state == "unknown":
            Logger.debug(
                "DeepSeek: chat/auth UI did not stabilize before auth check. "
                "Proceeding with best-effort state detection."
            )
            shell_state = await self._detect_deepseek_shell_state()

        is_sign_in_page = shell_state == "auth"

        # Check if we were redirected to sign in
        if is_sign_in_page:
            Logger.info("Redirected to sign in page.")
            
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
            
            if auto_login:
                email = ""
                password = ""
                pair = self.ece_active_pair()
                if not pair:
                    Logger.warning(
                        "Auto-login is enabled but no DeepSeek accounts are configured in Credential Manager. "
                        "Waiting for manual login..."
                    )
                    try:
                        await self.page.wait_for_selector("textarea", timeout=0)
                        Logger.success("Manual login detected.")
                    except Exception as e:
                        Logger.error(f"Error waiting for manual login: {e}")
                    return

                email = pair.email
                password = pair.password

                if not email or not password:
                    Logger.error("DeepSeek account is missing an email or password.")
                    return

                Logger.info("Auto-login enabled. Attempting to log in...")

                try:
                    # Wait for the form to appear
                    await self.page.wait_for_selector(
                        ".ds-sign-in-form__main, .ds-sign-in-form-wrapper, .ds-auth-form-wrapper, .ds-sign-up-form__main",
                        timeout=30000,
                    )

                    form_root = self.page.locator(
                        ".ds-sign-in-form-wrapper, .ds-sign-up-form-wrapper, .ds-auth-form-wrapper"
                    )
                    if await form_root.count() == 0:
                        form_root = self.page.locator("body")
                    
                    # Fill email
                    Logger.debug(f"Entering email: {email}")
                    email_input = form_root.first.locator(
                        "input[autocomplete='username'], "
                        "input[type='text'][placeholder*='email'], "
                        "input[type='text'][placeholder*='Email'], "
                        "input[type='text'][placeholder*='Phone']"
                    )
                    if await email_input.count() == 0:
                        email_input = form_root.first.locator("input[type='text']")
                    await email_input.first.fill(email)
                    
                    # Fill password
                    Logger.debug("Entering password...")
                    password_input = form_root.first.locator(
                        "input[autocomplete='current-password'], input[type='password']"
                    )
                    await password_input.first.fill(password)
                    
                    # Click login button
                    Logger.debug("Clicking login button...")
                    login_button = form_root.first.locator("button", has_text="Log in")
                    if await login_button.count() == 0:
                        login_button = form_root.first.locator("button.ds-basic-button--primary")
                    if await login_button.count() == 0:
                        login_button = self.page.locator("button", has_text="Log in")
                    await login_button.first.click()
                    
                    # Wait for navigation back to the chat page
                    await self.page.wait_for_selector("textarea", timeout=60000)
                    Logger.success("Login successful.")

                    self.ece_mark_used(email)
                    
                except Exception as e:
                    Logger.error(f"Error during auto-login: {e}")
            else:
                Logger.info("Auto-login disabled. Waiting for manual login...")
                # Wait indefinitely (or until closed) for the user to log in and reach the chat page
                try:
                    await self.page.wait_for_selector("textarea", timeout=0)
                    Logger.success("Manual login detected.")
                except Exception as e:
                    Logger.error(f"Error waiting for manual login: {e}")
        else:
            Logger.info("DeepSeek chat UI detected (or sign-in UI not found). Continuing...")
            self._mark_active_ece_pair_used()

    def _resolve_deepthink_flags(self, model: str) -> tuple[bool, bool]:
        enable_deepthink = bool(self.config_manager.get_setting("deepseek_behavior", "enable_deepthink"))
        send_deepthink = bool(self.config_manager.get_setting("deepseek_behavior", "send_deepthink"))

        mode = resolve_behavior_mode(model, self.provider)
        if mode == MODE_CHAT:
            return False, False
        if mode == MODE_REASONER:
            return True, send_deepthink

        return enable_deepthink, send_deepthink

    def _resolve_deepseek_request_settings(self, model: str, overrides: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        resolved_model = (model or "").strip() or "deepseek-auto"
        deepthink_enabled, send_deepthink = self._resolve_deepthink_flags(resolved_model)
        search_enabled = bool(self.config_manager.get_setting("deepseek_behavior", "enable_search"))
        send_as_text_file = bool(self.config_manager.get_setting("deepseek_behavior", "send_as_text_file"))
        model_type = resolve_deepseek_model_type(resolved_model, self.provider)

        settings = {
            "deepthink_enabled": bool(deepthink_enabled),
            "send_deepthink": bool(send_deepthink),
            "search_enabled": bool(search_enabled),
            "send_as_text_file": bool(send_as_text_file),
            "model_type": model_type,
        }

        if overrides:
            for key in ("deepthink_enabled", "send_deepthink", "search_enabled", "send_as_text_file"):
                if key in overrides:
                    settings[key] = bool(overrides[key])

        return settings

    def _get_first_chunk_timeout_s(self) -> float:
        try:
            value = float(
                self.config_manager.get_setting("deepseek_behavior", "first_chunk_timeout")
                or self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S
            )
        except Exception:
            value = self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S
        return max(value, 5.0)

    def _get_request_capture_mode(self) -> str:
        try:
            mode = str(
                self.config_manager.get_setting("deepseek_behavior", "request_capture_mode")
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

    def _extract_deepseek_macros_from_text(self, text: str) -> tuple[str, Dict[str, bool]]:
        return extract_macro_overrides(text, macro_actions=COMMON_REQUEST_MACRO_ACTIONS)

    def _strip_deepseek_macros_from_messages(self, messages: List[Any]) -> tuple[List[Any], Dict[str, bool]]:
        return strip_macros_from_messages(messages, macro_actions=COMMON_REQUEST_MACRO_ACTIONS)

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, Any]]:
        state = read_clean_regeneration_state(
            self.cache_manager,
            self.clean_regen_state_cache_key,
            log_label="Clean Regeneration",
        )
        if state is not None:
            return self._normalize_clean_regeneration_state(state)

        raw = self.cache_manager.read_cache(self.clean_regen_state_cache_key)
        if raw is None:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        return self._normalize_clean_regeneration_state(data)

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
        send_as_text_file: bool,
        model_type: str,
    ) -> Dict[str, Any]:
        return {
            "deepthink_enabled": bool(effective_deepthink),
            "search_enabled": bool(enable_search),
            "tools_enabled": False,
            "send_as_text_file": bool(send_as_text_file),
            "ui_model": self._model_type_display_name(model_type),
        }

    def _normalize_clean_regeneration_state(
        self,
        state: Optional[Dict[str, Any]],
        *,
        current_state: Optional[Dict[str, Any]] = None,
        search_button_found: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(state, dict):
            return None

        if not any(
            key in state
            for key in (
                "deepthink_enabled",
                "search_enabled",
                "send_as_text_file",
                "ui_model",
                "model_type",
            )
        ):
            return None

        ui_model = str(state.get("ui_model") or "").strip()
        if not ui_model and "model_type" in state:
            ui_model = self._model_type_display_name(str(state.get("model_type") or ""))
        if not ui_model and current_state:
            ui_model = str(current_state.get("ui_model") or "").strip()

        return {
            "deepthink_enabled": bool(state.get("deepthink_enabled")),
            "search_enabled": bool(state.get("search_enabled")) if search_button_found else False,
            "tools_enabled": bool(state.get("tools_enabled", False)),
            "send_as_text_file": bool(state.get("send_as_text_file")),
            "ui_model": ui_model,
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
            "conversation_url": f"https://chat.deepseek.com/a/chat/s/{conversation_id}",
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

    @classmethod
    def _build_followup_request_headers(cls, source_headers: Any) -> Dict[str, str]:
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
            if name in cls.FOLLOWUP_REQUEST_HEADER_ALLOWLIST or name.startswith("x-"):
                forwarded[name] = text
        return forwarded

    @staticmethod
    def _is_deepseek_success_code(value: Any) -> bool:
        if value is None:
            return False
        try:
            return int(value) == 0
        except Exception:
            return str(value).strip() == "0"

    @classmethod
    def _is_delete_response_success(cls, status: int, response_text: str) -> bool:
        if not (200 <= int(status or 0) < 300):
            return False

        try:
            payload = json.loads(response_text or "")
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        if not cls._is_deepseek_success_code(payload.get("code")):
            return False

        data = payload.get("data")
        if isinstance(data, dict) and "biz_code" in data:
            return cls._is_deepseek_success_code(data.get("biz_code"))
        return True

    @staticmethod
    def _format_delete_response_preview(response_text: str, limit: int = 1000) -> str:
        response_preview = str(response_text or "").strip()
        if not response_preview:
            return "<empty>"

        try:
            response_preview = json.dumps(
                json.loads(response_preview),
                ensure_ascii=True,
                separators=(",", ":"),
            )
        except Exception:
            pass

        if len(response_preview) > limit:
            return response_preview[:limit] + "...<truncated>"
        return response_preview

    async def _delete_conversation_by_id(self, conversation_id: str) -> bool:
        normalized_id = str(conversation_id or "").strip()
        if not normalized_id:
            return False

        headers = self._build_followup_request_headers(
            getattr(self, "_last_followup_request_headers", {}) or {}
        )
        headers.setdefault("accept", "application/json, text/plain, */*")
        headers.setdefault("content-type", "application/json")
        headers.setdefault("origin", "https://chat.deepseek.com")
        headers.setdefault("referer", "https://chat.deepseek.com/")
        cookies = await self._get_context_cookie_dict()
        auth_state = "present" if str(headers.get("authorization") or "").strip() else "missing"

        Logger.extra_debug(
            "DeepSeek: auto-delete request -> "
            f"POST /api/v0/chat_session/delete chat_session_id={normalized_id}; "
            f"authorization={auth_state}; cookies={len(cookies)}"
        )
        try:
            client = await self._get_http_client()
            response = await client.post(
                "https://chat.deepseek.com/api/v0/chat_session/delete",
                headers=headers,
                cookies=cookies,
                json={"chat_session_id": normalized_id},
                timeout=20.0,
            )
        except Exception as e:
            Logger.warning(f"DeepSeek: failed to auto-delete chat {normalized_id}: {e}")
            return False

        status = int(response.status_code or 0)
        response_text = str(response.text or "").strip()
        ok = self._is_delete_response_success(status, response_text)
        response_preview = self._format_delete_response_preview(response_text)
        response_error = ""

        error_preview = response_error or "<none>"
        Logger.extra_debug(
            "DeepSeek: auto-delete response -> "
            f"chat_session_id={normalized_id}; ok={ok}; status={status}; "
            f"error={error_preview}; body={response_preview}"
        )

        if ok:
            return True

        detail = str(response_error or response_text).strip()
        suffix = f" ({detail[:180]})" if detail else ""
        Logger.warning(
            f"DeepSeek: failed to auto-delete chat {normalized_id} (status={status}){suffix}"
        )
        return False

    async def _auto_delete_current_chat(self) -> bool:
        current_info = await self._get_current_conversation_info()
        if current_info is None:
            Logger.debug("DeepSeek: auto-delete skipped because the current chat ID was not available.")
            return False

        conversation_id = str(current_info.get("conversation_id") or "").strip()
        if not conversation_id:
            Logger.debug("DeepSeek: auto-delete skipped because the current chat ID was empty.")
            return False

        try:
            await self._click_new_chat()
            await asyncio.sleep(0.5)
        except Exception as e:
            Logger.warning(
                f"DeepSeek: auto-delete skipped because a replacement chat could not be prepared: {e}"
            )
            return False

        if await self._delete_conversation_by_id(conversation_id):
            Logger.info("DeepSeek: auto-deleted the completed chat.")
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
            Logger.warning(f"Multi-Slot Cache (DeepSeek): failed to open cached chat URL: {e}")
            return False

        try:
            shell_state = await self._wait_for_deepseek_shell_state(timeout_ms=60000)
        except Exception as e:
            Logger.warning(f"Multi-Slot Cache (DeepSeek): chat shell did not become ready: {e}")
            return False

        return shell_state == "chat"

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
            log_label="Multi-Slot Cache (DeepSeek)",
        )
        entry = find_multi_slot_cache_entry(payload, account_key, formatted_message, multi_slot_state)
        if entry is None:
            return False

        current_info = await self._get_current_conversation_info()
        if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
            Logger.info("Multi-Slot Cache (DeepSeek): opening cached conversation for regeneration...")
            opened = await self._open_cached_conversation(entry["conversation_url"])
            if not opened:
                return False
            current_info = await self._get_current_conversation_info()
            if current_info is None or current_info["conversation_id"] != entry["conversation_id"]:
                Logger.warning(
                    "Multi-Slot Cache (DeepSeek): cached conversation URL opened, but the expected "
                    "chat ID was not available. Falling back to a new chat."
                )
                return False

        try:
            multi_slot_model_type = self._model_type_from_display_text(
                str(multi_slot_state.get("ui_model") or "")
            )
            if multi_slot_model_type:
                await self.set_model_type_state(multi_slot_model_type)
            await self.set_deepthink_state(bool(multi_slot_state.get("deepthink_enabled")))
            await self.set_search_state(bool(multi_slot_state.get("search_enabled")))
            await asyncio.sleep(0.25)
        except Exception:
            pass

        Logger.info("Multi-Slot Cache (DeepSeek): cached prompt match found. Attempting to regenerate...")
        completion_armed.set()
        if not await self._click_regenerate():
            completion_armed.clear()
            Logger.warning(
                "Multi-Slot Cache (DeepSeek): regenerate button unavailable. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (DeepSeek)",
            )
            return False

        try:
            await asyncio.wait_for(completion_started.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            completion_armed.clear()
            Logger.warning(
                "Multi-Slot Cache (DeepSeek): completion request not observed after clicking "
                "Regenerate. Removing cached entry."
            )
            remove_multi_slot_cache_entry(
                self.cache_manager,
                self.multi_slot_cache_key,
                account_key,
                entry["conversation_id"],
                log_label="Multi-Slot Cache (DeepSeek)",
            )
            return False

        return True

    async def generate_response(self, message: Union[str, List[Any]], model: str = "deepseek-auto", stream: bool = False, temperature: float = None, top_p: float = None, max_tokens: int | None = None, abort_event: asyncio.Event = None):
        """
        Generates a response from DeepSeek.
        This function intercepts the network request to support streaming.
        """
        _ = max_tokens
        response_queue = asyncio.Queue()
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

        def get_intercepted_activity_count() -> int:
            return intercepted_activity_count

        async def abort_intercepted_request() -> None:
            intercepted_request_abort.set()
            response = intercepted_response
            if response is not None:
                try:
                    await response.aclose()
                except Exception as e:
                    Logger.debug(f"DeepSeek: failed to close intercepted response: {e}")
            try:
                await self._click_stop_button()
            except Exception as e:
                Logger.debug(f"DeepSeek: failed to click Stop during timeout handling: {e}")

        # Some selectors rely on English UI text; fail fast with a clear error instead of hanging.
        await self.require_english_ui()
        
        # Reset state for new generation
        self.fragment_types_list = []
        self.thinking_active = False
        self.abort_requested = False
        self.current_abort_event = abort_event
        self._reset_stream_parser()
        self._last_generation_censored = False
        resolved_model = (model or "").strip() or "deepseek-auto"
        self.current_model = resolved_model

        macros_overrides: Dict[str, bool] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = self._strip_deepseek_macros_from_messages(message)
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = self._extract_deepseek_macros_from_text(message)

        if macros_overrides:
            Logger.debug(f"DeepSeek macros applied: {macros_overrides}")

        effective_settings = self._resolve_deepseek_request_settings(resolved_model, overrides=macros_overrides)
        effective_deepthink = effective_settings["deepthink_enabled"]
        effective_send_deepthink = effective_settings["send_deepthink"]
        enable_search = effective_settings["search_enabled"]
        send_as_text_file = effective_settings["send_as_text_file"]
        effective_model_type = effective_settings["model_type"]
        enable_search, search_button_found = await self._resolve_available_search_state(enable_search)
        effective_settings["search_enabled"] = bool(enable_search)
        self.current_send_deepthink = effective_send_deepthink
        
        async def handle_route(route):
            nonlocal completion_claimed, intercepted_activity_count, intercepted_response
            request = route.request
            if not completion_armed.is_set():
                await route.continue_()
                return

            async with completion_claim_lock:
                if completion_claimed:
                    await route.continue_()
                    return
                completion_claimed = True
                completion_started.set()

            Logger.info("Intercepting DeepSeek API request...")
            Logger.debug(f"Intercepted request to: {request.url}")
            
            # Prepare headers and cookies
            headers = await request.all_headers()
            # Remove headers auto-generated by httpx
            headers.pop("content-length", None)
            headers.pop("host", None)
            self._last_followup_request_headers = self._build_followup_request_headers(headers)
            
            # Get cookies from the context
            cookies = await self.context.cookies()
            cookie_dict = {c['name']: c['value'] for c in cookies}
            
            # Get the original post data
            post_data = request.post_data_json
            
            # Don't touch the original post data, as the ui needs what it sent
            # But we could modify it here if needed
            full_response_body = bytearray()
            response_headers = {}
            aborted = False
            
            try:
                client = await self._get_http_client()
                try:
                    Logger.info("Streaming response from DeepSeek...")
                    async with client.stream("POST", request.url, headers=headers, cookies=cookie_dict, json=post_data, timeout=60.0) as response:
                        intercepted_response = response
                        # Capture headers to forward them later
                        # We specifically need Content-Type so the frontend knows it's an SSE stream
                        for k, v in response.headers.items():
                            response_headers[k] = v

                        async for chunk in response.aiter_bytes():
                            intercepted_activity_count += 1
                            # Check if abort was requested
                            if (
                                intercepted_request_abort.is_set()
                                or self.abort_requested
                                or self._stream_provider_abort_requested
                                or (abort_event and abort_event.is_set())
                            ):
                                Logger.debug("Abort detected during streaming, stopping...")
                                aborted = True
                                break

                            full_response_body.extend(chunk)
                            # Process chunk for streaming
                            await self._process_chunk(chunk, response_queue)
                            if (
                                intercepted_request_abort.is_set()
                                or self.abort_requested
                                or self._stream_provider_abort_requested
                                or (abort_event and abort_event.is_set())
                            ):
                                Logger.debug("Abort requested while processing DeepSeek stream chunk.")
                                aborted = True
                                break

                        if (
                            not aborted
                            and (not intercepted_request_abort.is_set())
                            and not self.abort_requested
                            and not self._stream_provider_abort_requested
                        ):
                            await self._process_chunk(b"", response_queue, final=True)

                except httpx.ReadError as e:
                    if (
                        not aborted
                        and (not intercepted_request_abort.is_set())
                        and not self.abort_requested
                        and not self._stream_provider_abort_requested
                    ):
                        Logger.error(f"Read error during intercepted request: {e}")
                        await response_queue.put({"error": str(e)})
                except Exception as e:
                    if (
                        not aborted
                        and (not intercepted_request_abort.is_set())
                        and not self.abort_requested
                        and not self._stream_provider_abort_requested
                    ):
                        Logger.error(f"Error during intercepted request: {e}")
                        await response_queue.put({"error": str(e)})
            except RuntimeError as e:
                # Ignore RuntimeError from async generator cleanup during abort
                if "async generator" in str(e) or "cancel scope" in str(e):
                    Logger.debug(f"Ignored expected error during abort: {e}")
                else:
                    raise
            finally:
                intercepted_response = None
            
            # Log the aborted state; the timeout/abort path already attempted a UI stop click.
            if (
                aborted
                or intercepted_request_abort.is_set()
                or self.abort_requested
                or self._stream_provider_abort_requested
            ):
                Logger.warning("DeepSeek generation was aborted before completion.")
            
            # Fulfill the original request so the UI updates
            try:
                if (
                    aborted
                    or intercepted_request_abort.is_set()
                    or self.abort_requested
                    or self._stream_provider_abort_requested
                ):
                    await route.abort()
                else:
                    # Forward the captured headers, especially Content-Type
                    await route.fulfill(body=bytes(full_response_body), status=200, headers=response_headers)
            except Exception as e:
                Logger.error(f"Error finalizing route: {e}")
            
            # Signal end of stream
            await response_queue.put(None)
            intercepted_request_finished.set()
            if (
                not aborted
                and (not intercepted_request_abort.is_set())
                and not self.abort_requested
                and not self._stream_provider_abort_requested
            ):
                Logger.success("Response streaming completed.")

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"DeepSeek: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"DeepSeek: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        def request_aborted() -> bool:
            return bool(
                intercepted_request_abort.is_set()
                or self.abort_requested
                or self._stream_provider_abort_requested
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
            if not aborted and not encountered_error:
                await self._process_chunk(b"", response_queue, final=True)

            await response_queue.put(None)
            intercepted_request_finished.set()
            request_methods.pop(request_id, None)
            cdp_pending_response_urls.pop(request_id, None)

            if not aborted and not encountered_error and not request_aborted():
                Logger.success("DeepSeek CDP stream completed.")

        async def feed_cdp_stream_chunk(request_id: str, data: bytes) -> None:
            nonlocal intercepted_activity_count, cdp_had_data
            if request_id != cdp_active_request_id or not data or cdp_stream_finished:
                return

            cdp_had_data = True
            intercepted_activity_count += 1
            if request_aborted():
                await finish_cdp_stream(request_id, aborted=True)
                return

            await self._process_chunk(data, response_queue)
            if request_aborted():
                if self._stream_provider_abort_requested:
                    try:
                        await self._click_stop_button()
                    except Exception as exc:
                        Logger.debug(f"DeepSeek: failed to click Stop after provider abort: {exc}")
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
            Logger.info("Teeing DeepSeek API response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message_text = f"DeepSeek CDP response streaming failed: {exc}"
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

            self._last_followup_request_headers = self._build_followup_request_headers(
                request.get("headers")
            )
            Logger.info("Observing DeepSeek API request via CDP...")
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
                    "DeepSeek CDP stream ended with net::ERR_ABORTED after data arrived; "
                    "treating it as complete."
                )
                await finish_cdp_stream(request_id)
                return

            message_text = f"DeepSeek CDP stream failed: {error_text}"
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

        # Set up interception
        try:
            if use_cdp_teeing:
                try:
                    cdp_session = await self.context.new_cdp_session(self.page)
                    await cdp_session.send("Network.enable", {})
                    cdp_session.on("Network.requestWillBeSent", on_cdp_request_will_be_sent)
                    cdp_session.on("Network.responseReceived", on_cdp_response_received)
                    cdp_session.on("Network.dataReceived", on_cdp_data_received)
                    cdp_session.on("Network.loadingFinished", on_cdp_loading_finished)
                    cdp_session.on("Network.loadingFailed", on_cdp_loading_failed)
                    cdp_listeners_registered = True
                    Logger.info("DeepSeek Request Capture Mode: CDP Teeing.")
                except Exception as exc:
                    message_text = f"DeepSeek CDP setup failed: {exc}"
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
            else:
                await self.page.route(self.COMPLETION_ROUTE_GLOB, handle_route)
                await self.page.route(self.REGENERATE_ROUTE_GLOB, handle_route)
                route_handlers_registered = True
                Logger.info("DeepSeek Request Capture Mode: Replay.")

            # Apply formatting
            formatted_message = self._format_messages(message_for_formatting)
            self._capture_diagnostics_prompt_snapshot(
                formatted_message,
                metadata={
                    "model": resolved_model,
                    "model_type": effective_model_type,
                    "deepthink_enabled": bool(effective_deepthink),
                    "search_enabled": bool(enable_search),
                    "send_as_text_file": bool(send_as_text_file),
                },
            )
            
            # Check for Clean Regeneration
            clean_regeneration = bool(
                self.config_manager.get_setting("deepseek_behavior", "clean_regeneration")
            )
            multi_slot_cache_enabled = bool(
                clean_regeneration
                and self.config_manager.get_setting("deepseek_behavior", "multi_slot_cache")
            )
            try:
                auto_delete_requested = bool(
                    self.config_manager.get_setting("deepseek_behavior", "auto_delete_chats")
                )
            except Exception:
                auto_delete_requested = False
            auto_delete_enabled = bool(auto_delete_requested and (not clean_regeneration))
            if auto_delete_requested and clean_regeneration:
                Logger.warning(
                    "DeepSeek: Delete Chat After Reply is skipped for this request because "
                    "Reuse Matching Chat is enabled."
                )
            regenerated = False
            current_cache_matched = False
            should_record_multi_slot = False
            clean_regen_state = None
            multi_slot_state = None
            
            if clean_regeneration:
                clean_regen_state = self._build_multi_slot_cache_state(
                    effective_deepthink=bool(effective_deepthink),
                    enable_search=bool(enable_search),
                    send_as_text_file=bool(send_as_text_file),
                    model_type=effective_model_type,
                )
                multi_slot_state = dict(clean_regen_state)

                last_message = self.cache_manager.read_cache(self.clean_regen_message_cache_key)
                last_state = self._read_clean_regeneration_state()
                last_state = self._normalize_clean_regeneration_state(
                    last_state,
                    current_state=clean_regen_state,
                    search_button_found=search_button_found,
                )

                message_matches = last_message == formatted_message
                state_matches = last_state == clean_regen_state

                if message_matches and state_matches:
                    current_cache_matched = True
                    Logger.info("Clean Regeneration: Message and settings match cache. Attempting to regenerate...")
                    completion_armed.set()
                    if await self._click_regenerate():
                        Logger.info("Clean Regeneration: Button clicked. Regenerating...")
                        try:
                            await asyncio.wait_for(completion_started.wait(), timeout=20.0)
                        except asyncio.TimeoutError:
                            Logger.warning(
                                "Clean Regeneration: completion request not observed after clicking "
                                "Regenerate. Falling back to new chat."
                            )
                        else:
                            regenerated = True
                            self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                            self._write_clean_regeneration_state(clean_regen_state)
                    else:
                        Logger.warning("Clean Regeneration: Button not found or disabled. Falling back to new chat.")
                elif message_matches and not state_matches:
                    Logger.debug(
                        f"Clean Regeneration: cached state {last_state} != requested state {clean_regen_state}"
                    )
                    Logger.info("Clean Regeneration: Message matches cache but settings changed. Creating new chat.")
                else:
                    Logger.debug("Clean Regeneration: Message differs from cache. Creating new chat.")

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
                # Trigger UI interaction
                # Clear previous chat by clicking New Chat
                Logger.info("Preparing new chat session...")
                await self._click_new_chat()
                # Small wait for the UI to update
                await asyncio.sleep(0.5)

                # Apply settings before sending
                await self.set_model_type_state(effective_model_type)
                applied_model_type = await self._read_visible_model_type()
                if applied_model_type:
                    applied_ui_model = self._model_type_display_name(applied_model_type)
                    if clean_regen_state is not None:
                        clean_regen_state["ui_model"] = applied_ui_model
                    if multi_slot_state is not None:
                        multi_slot_state["ui_model"] = applied_ui_model
                await self.set_deepthink_state(effective_deepthink)
                await self.set_search_state(enable_search)
                
                # Small delay for the toggles to take effect
                await asyncio.sleep(0.5)
                
                # Check if we should send as text file
                if send_as_text_file:
                    Logger.info("Sending message as text file...")
                    file_payload = build_prompt_text_file_payload(formatted_message)
                    uploaded = await self._upload_file(file_payload)

                    if uploaded:
                        # Get timeout from settings
                        upload_timeout = self.config_manager.get_setting("deepseek_behavior", "file_upload_timeout")
                        Logger.info("Sending request to DeepSeek...")
                        completion_armed.set()
                        await self._send_message(timeout=upload_timeout)
                    else:
                        Logger.warning("DeepSeek: text-file upload unavailable; falling back to pasted text.")
                        await self._enter_message(formatted_message)
                        Logger.info("Sending request to DeepSeek...")
                        completion_armed.set()
                        await self._send_message()
                else:
                    await self._enter_message(formatted_message)
                    Logger.info("Sending request to DeepSeek...")
                    completion_armed.set()
                    await self._send_message()
                
                # Update cache
                if clean_regeneration:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(clean_regen_state)
                    should_record_multi_slot = bool(multi_slot_cache_enabled and multi_slot_state)

            if not completion_started.is_set():
                try:
                    await asyncio.wait_for(completion_started.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    Logger.error(
                        "DeepSeek: completion request was not observed. "
                        "The UI may have swallowed the click or the endpoint changed."
                    )
                    yield f"data: {json.dumps({'error': 'DeepSeek: completion request not observed'})}\n\n"
                    return
            
            # Yield responses from queue
            stream_had_error = False
            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self._get_first_chunk_timeout_s(),
                idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                on_timeout=abort_intercepted_request,
                activity_counter=get_intercepted_activity_count,
            ):
                if item is None:
                    break
                if isinstance(item, dict) and "error" in item:
                    stream_had_error = True
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                
                yield item

            if should_record_multi_slot and (not stream_had_error) and (not self.abort_requested):
                if self._last_generation_censored:
                    Logger.info(
                        "Multi-Slot Cache (DeepSeek): skipping cache save because the conversation "
                        "was censored."
                    )
                else:
                    conversation_info = await self._wait_for_current_conversation_info(timeout_ms=6000)
                    if conversation_info is None:
                        Logger.debug(
                            "Multi-Slot Cache (DeepSeek): could not resolve conversation URL after "
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
                            log_label="Multi-Slot Cache (DeepSeek)",
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
                    Logger.debug("DeepSeek: timed out waiting for intercepted request cleanup.")
            # Cleanup interception
            self.current_abort_event = None
            self.abort_requested = False
            self.current_model = None
            self.current_send_deepthink = None
            self._last_generation_censored = False
            self._reset_stream_parser()
            if route_handlers_registered:
                try:
                    await self.page.unroute(self.COMPLETION_ROUTE_GLOB, handle_route)
                except Exception:
                    pass
                try:
                    await self.page.unroute(self.REGENERATE_ROUTE_GLOB, handle_route)
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
                    Logger.debug(f"DeepSeek: CDP detach failed: {exc}")

    async def abort_generation(self):
        """
        Aborts the current generation request.
        Called by the API when client disconnects.
        """
        Logger.info("Abort generation requested...")
        self.abort_requested = True
        if self.current_abort_event:
            self.current_abort_event.set()
        # Click the stop button in DeepSeek UI
        await self._click_stop_button()

    def _deepseek_signature_looks_like_stop(self, signature: str | None) -> bool:
        if not signature:
            return False
        lowered = signature.lower()
        return any(token in lowered for token in ("stop", "cancel", "abort", "pause", "square"))

    async def _click_stop_button(self):
        """
        Clicks the Stop button in DeepSeek UI to cancel the ongoing generation.
        The Stop button appears in place of the Send button during generation.
        """
        try:
            stop_button = self._locate_send_control()
            if await stop_button.count() == 0:
                Logger.debug("Stop button not found.")

                return False

            if await self._is_deepseek_control_disabled(stop_button):
                Logger.debug("Stop button is disabled (generation may have already stopped).")
                return False

            current_signature = await self._read_control_signature(stop_button)
            cached_send_signature = getattr(self, "_send_control_signature", None)
            if cached_send_signature and current_signature == cached_send_signature:
                Logger.debug("Composer control matches send mode. Skipping stop click.")
                return False
            if (not cached_send_signature) and (not self._deepseek_signature_looks_like_stop(current_signature)):
                Logger.debug("Composer control could not be verified as stop mode.")
                return False

            Logger.debug("Clicking Stop button...")
            await stop_button.click()
            Logger.debug("Stop button clicked successfully.")
            return True
        except Exception as e:
            Logger.error(f"Error clicking stop button: {e}")
            return False

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        return format_request_messages(self.config_manager, messages)

    @staticmethod
    def _get_stream_fragment_base_path(path: str | None) -> Optional[str]:
        normalized = str(path or "").strip()
        if not normalized:
            return None

        if normalized.startswith("response/fragments/"):
            parts = normalized.split("/")
            if len(parts) >= 3:
                return "/".join(parts[:3])
            return None

        if normalized.startswith("fragments/"):
            parts = normalized.split("/")
            if len(parts) >= 2:
                return "/".join(parts[:2])

        return None

    def _resolve_fragment_type_for_path(self, path: str | None) -> Optional[str]:
        normalized = str(path or "").strip()
        if not normalized or not hasattr(self, "fragment_types_list"):
            return None

        parts = normalized.split("/")
        try:
            if normalized.startswith("response/fragments/") and len(parts) >= 3:
                index = int(parts[2])
            elif normalized.startswith("fragments/") and len(parts) >= 2:
                index = int(parts[1])
            else:
                return None
        except ValueError:
            return None

        if not self.fragment_types_list:
            return None

        try:
            return str(self.fragment_types_list[index] or "").upper() or None
        except (IndexError, TypeError):
            return None

    def _remember_active_fragment(self, path: str | None) -> Optional[str]:
        base_path = self._get_stream_fragment_base_path(path)
        if base_path is None:
            return None

        self._stream_active_fragment_base_path = base_path
        fragment_type = self._resolve_fragment_type_for_path(base_path)
        if fragment_type:
            self._stream_active_fragment_type = fragment_type
        return fragment_type

    @staticmethod
    def _is_stream_text_fragment(fragment_type: str | None) -> bool:
        normalized = str(fragment_type or "").strip().upper()
        return normalized not in {"", "SEARCH", "TOOL_SEARCH"}

    def _append_stream_text(self, value: Any, *, send_deepthink: bool) -> str:
        text = str(value or "")
        fragment_type = str(self._stream_active_fragment_type or "").strip().upper()
        if not fragment_type:
            if getattr(self, "thinking_active", False):
                return text if send_deepthink else ""
            return text

        if fragment_type == "THINK":
            return text if send_deepthink else ""
        if not self._is_stream_text_fragment(fragment_type):
            return ""
        return text

    def _expand_relative_stream_ops(
        self,
        value: Any,
        *,
        base_path: str | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []

        out: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue

            normalized_item = dict(item)
            item_path = str(normalized_item.get("p") or "").strip()
            if (
                base_path
                and item_path
                and not item_path.startswith("response/")
                and not item_path.startswith("fragments/")
            ):
                normalized_item["p"] = f"{base_path}/{item_path}"
            out.append(normalized_item)

        return out

    @staticmethod
    def _looks_like_stream_op_list(value: Any) -> bool:
        if not isinstance(value, list) or not value:
            return False
        return any(isinstance(item, dict) and ("p" in item) for item in value)

    def _is_regeneration_limit_payload(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False

        finish_reason = str(data.get("finish_reason") or "").strip().lower()
        if finish_reason == "regeneration_limit":
            return True

        content = str(data.get("content") or "").strip().lower()
        payload_type = str(data.get("type") or "").strip().lower()
        return payload_type == "error" and "regeneration limit" in content

    async def _process_sse_line(self, line: str, queue: asyncio.Queue) -> None:
        if not line.startswith("data:"):
            return

        data_str = line[len("data:") :].strip()
        if data_str == "[DONE]":
            return

        try:
            data = json.loads(data_str)
            if self._is_regeneration_limit_payload(data):
                self._stream_provider_abort_requested = True
                Logger.warning(self.REGENERATION_LIMIT_ERROR_MESSAGE)
                await queue.put({"error": self.REGENERATION_LIMIT_ERROR_MESSAGE})
                return

            # Cache settings once per chunk processing
            anti_censorship = self.config_manager.get_setting("deepseek_behavior", "anti_censorship")
            send_deepthink = (
                self.current_send_deepthink
                if self.current_send_deepthink is not None
                else self.config_manager.get_setting("deepseek_behavior", "send_deepthink")
            )
            content = ""
            finish_reason = None
            
            # Normalize updates to a list of operations
            ops = []
            
            if "v" in data:
                v = data["v"]
                p = data.get("p")
                o = data.get("o")
                relative_base_path = p or self._stream_active_fragment_base_path

                # Case 1: Batch update (v is list of ops)

                # ---------------- RYAN!! ----------------
                    # TO RYAN: STOP REMOVING COMMENTS
                    # I KNOW YOU HATE THEIR COLOR BUT THEY'RE FOR CONTRIBUTORS
                    # YOU CAN JUST USE A DARK THEME IF IT BOTHERS YOU
                # ---------------- RYAN!! ----------------

                if p is None or (p == "response" and o == "BATCH"):
                    if self._looks_like_stream_op_list(v):
                        ops = self._expand_relative_stream_ops(v, base_path=relative_base_path)
                    elif isinstance(v, str):
                        # Direct content update (inconsistent here - once I caught this happening but it seems like a bug on their side)
                        content = self._append_stream_text(v, send_deepthink=bool(send_deepthink))
                    elif isinstance(v, dict):
                        # Initial response payload: {"response": {"fragments": [...]}}
                        # DeepSeek now sends the first fragment inline in the
                        # opening payload rather than as a separate APPEND event
                        response_obj = v.get("response", v)
                        if isinstance(response_obj, dict):
                            fragments = response_obj.get("fragments")
                            if isinstance(fragments, list) and fragments:
                                ops = [{"p": "response/fragments", "o": "APPEND", "v": fragments}]
            
                # Case 2: Single Path-based update
                else:
                    if self._looks_like_stream_op_list(v):
                        expanded_ops = self._expand_relative_stream_ops(v, base_path=p)
                        ops = expanded_ops or [{"p": p, "o": o, "v": v}]
                    else:
                        ops = [{"p": p, "o": o, "v": v}]

            # Process all operations
            should_stop_processing = False
            
            for item in ops:
                if not isinstance(item, dict):
                    continue
                    
                item_p = item.get("p")
                item_o = item.get("o")
                item_v = item.get("v")
                

                
                # Check for Anti-Censorship (CONTENT_FILTER)
                if item_p in ("status", "response/status") and item_v == "CONTENT_FILTER":
                    self._last_generation_censored = True

                if anti_censorship:
                    if item_p in ("status", "response/status") and item_v == "CONTENT_FILTER":
                        Logger.info("Anti-Censorship triggered: Suppressing refusal message.")
                        finish_reason = "stop"
                        if getattr(self, "thinking_active", False):
                            if send_deepthink:
                                content += "</think>"
                            self.thinking_active = False
                        should_stop_processing = True
                        break

                # Status update
                if item_p in ("status", "response/status"):
                    if item_v == "FINISHED":
                        finish_reason = "stop"
                        # Close think tag if open
                        if getattr(self, "thinking_active", False):
                            if send_deepthink:
                                content += "</think>"
                            self.thinking_active = False
                
                # Fragments append (New Fragment)
                # Handle both 'fragments' and 'response/fragments'
                elif (item_p == "fragments" or item_p == "response/fragments") and item_o == "APPEND":
                    fragments = item_v
                    if isinstance(fragments, list):
                        for frag in fragments:
                            if isinstance(frag, dict):
                                frag_type = str(frag.get("type") or "").upper()
                                # Store type by index (len of list before append)
                                if not hasattr(self, "fragment_types_list"):
                                    self.fragment_types_list = []
                                self.fragment_types_list.append(frag_type)
                                self._stream_active_fragment_type = frag_type
                                self._stream_active_fragment_base_path = (
                                    "response/fragments/-1"
                                    if str(item_p).startswith("response/")
                                    else "fragments/-1"
                                )
                                

                                
                                # Handle THINK start
                                if frag_type == "THINK":
                                    if send_deepthink:
                                        content += "<think>"
                                    self.thinking_active = True
                                
                                # Handle RESPONSE start (end of THINK if active)
                                if frag_type == "RESPONSE" and getattr(self, "thinking_active", False):
                                    if send_deepthink:
                                        content += "</think>"
                                    self.thinking_active = False
                                
                                # Initial content
                                if "content" in frag:
                                    content += self._append_stream_text(
                                        frag["content"],
                                        send_deepthink=bool(send_deepthink),
                                    )

                # Content update: response/fragments/0/content OR fragments/0/content
                elif item_p and (item_p.startswith("response/fragments/") or item_p.startswith("fragments/")) and item_p.endswith("/content"):
                    self._remember_active_fragment(item_p)
                    content += self._append_stream_text(
                        item_v,
                        send_deepthink=bool(send_deepthink),
                    )
                        
                # Status update: response/fragments/0/status
                elif item_p and (item_p.startswith("response/fragments/") or item_p.startswith("fragments/")) and item_p.endswith("/status"):
                    self._remember_active_fragment(item_p)
                    if item_v == "FINISHED":
                        pass

            if should_stop_processing:
                pass

            if content or finish_reason:
                model_name = self.current_model or "deepseek-auto"
                await queue.put(
                    make_openai_delta_sse(
                        model_name,
                        content,
                        finish_reason=finish_reason,
                    )
                )
                
        except json.JSONDecodeError:
            pass
        except Exception as e:
            Logger.error(f"Error processing stream line: {e}")

    async def _process_chunk(self, chunk: bytes, queue: asyncio.Queue, *, final: bool = False):
        try:
            decoded = self._stream_text_decoder.decode(chunk, final=final)
            if decoded:
                self._stream_text_buffer += decoded

            while True:
                newline_idx = self._stream_text_buffer.find("\n", self._stream_text_buffer_pos)
                if newline_idx == -1:
                    break

                line = self._stream_text_buffer[self._stream_text_buffer_pos:newline_idx].rstrip("\r")
                self._stream_text_buffer_pos = newline_idx + 1
                await self._process_sse_line(line, queue)

            if self._stream_text_buffer_pos > 8192:
                self._stream_text_buffer = self._stream_text_buffer[self._stream_text_buffer_pos :]
                self._stream_text_buffer_pos = 0

            if final:
                tail = self._stream_text_buffer[self._stream_text_buffer_pos :]
                self._stream_text_buffer = ""
                self._stream_text_buffer_pos = 0
                if tail.strip():
                    await self._process_sse_line(tail.rstrip("\r"), queue)
        except Exception as e:
            Logger.error(f"Error processing chunk: {e}")

    async def set_deepthink_state(self, state: bool):
        """
        Toggles the DeepThink mode to the desired state.
        """
        # this toggle used to be a <button>, but is now often a <div role="button">.
        button = self.page.locator(".ds-toggle-button", has_text="DeepThink")
        if await button.count() == 0:
            button = self.page.locator("[role='button']", has_text="DeepThink")
        
        if await button.count() == 0:
            Logger.warning("DeepThink button not found.")
            return

        class_attr = await button.first.get_attribute("class") or ""
        is_selected = "ds-toggle-button--selected" in class_attr
        
        if is_selected != state:
            Logger.debug(f"Toggling DeepThink to {state}...")
            await button.first.click()
        else:
            Logger.debug(f"DeepThink is already {state}.")

    async def _find_search_button(self):
        # this toggle used to be a <button>, but is now often a <div role="button">.
        button = self.page.locator(".ds-toggle-button", has_text="Search")
        if await button.count() == 0:
            button = self.page.locator("[role='button']", has_text="Search")
        if await button.count() == 0:
            return None
        return button.first

    async def _resolve_available_search_state(self, requested_state: bool) -> tuple[bool, bool]:
        button = await self._find_search_button()
        if button is not None:
            return bool(requested_state), True

        if requested_state:
            Logger.warning("DeepSeek: Search button not found; assuming Search is disabled.")
        else:
            Logger.debug("DeepSeek: Search button not found; assuming Search is disabled.")
        return False, False

    async def set_search_state(self, state: bool):
        """
        Toggles the Search mode to the desired state.
        """
        button = await self._find_search_button()
        if button is None:
            if state:
                Logger.warning("DeepSeek: Search button not found; assuming Search is disabled.")
            else:
                Logger.debug("DeepSeek: Search button not found; assuming Search is disabled.")
            return

        class_attr = await button.get_attribute("class") or ""
        is_selected = "ds-toggle-button--selected" in class_attr
        
        if is_selected != state:
            Logger.debug(f"Toggling Search to {state}...")
            await button.click()
        else:
            Logger.debug(f"Search is already {state}.")

    def _model_type_picker_label(self, picker_kind: str) -> str:
        return self.MODEL_TYPE_PICKER_LABELS.get(picker_kind, str(picker_kind or "unknown"))

    @staticmethod
    def _format_picker_error(exc: Exception) -> str:
        name = exc.__class__.__name__
        detail = str(exc or "").strip().splitlines()
        if not detail:
            return name
        return f"{name}: {detail[0][:200]}"

    @staticmethod
    def _model_type_from_display_text(text: str) -> Optional[str]:
        normalized = str(text or "").strip().lower()
        if "expert" in normalized:
            return DEEPSEEK_MODEL_TYPE_EXPERT
        if "instant" in normalized:
            return DEEPSEEK_MODEL_TYPE_DEFAULT
        return None

    @staticmethod
    def _model_type_display_name(model_type: str) -> str:
        normalized = str(model_type or "").strip().lower()
        if normalized == DEEPSEEK_MODEL_TYPE_EXPERT:
            return "Expert"
        if normalized == "vision":
            return "Vision"
        return "Instant"

    async def _read_visible_model_type(self) -> Optional[str]:
        if not self.page:
            return None

        label = self.page.locator("span._46a12ab")
        try:
            if await label.count() == 0:
                return None
            text = await label.first.text_content(timeout=1000)
        except Exception:
            return None

        return self._model_type_from_display_text(str(text or ""))

    def _model_type_option_selector(self, picker_kind: str, model_type: str) -> str:
        normalized = str(model_type or "").strip().lower()
        if picker_kind == self.MODEL_TYPE_PICKER_NEW:
            # For Instant, try multiple possible data-model-type values
            if normalized in (DEEPSEEK_MODEL_TYPE_DEFAULT, "instant", "instant-v3"):
                # Create selector that matches any of the possible instant values
                return "div[role='radiogroup'] div[role='radio'][data-model-type='default'], div[role='radiogroup'] div[role='radio'][data-model-type='instant'], div[role='radiogroup'] div[role='radio'][data-model-type='instant-v3']"
            return f"div[role='radiogroup'] div[role='radio'][data-model-type='{normalized}']"
        return f"div._9f2341b._7ac2123[data-model-type='{normalized}']"

    async def _has_visible_model_type_option(self, picker_kind: str, model_type: str) -> bool:
        selector = self._model_type_option_selector(picker_kind, model_type)
        return await self._has_visible_selector([selector])

    async def _detect_model_type_picker_kind(self, *, log_result: bool = False) -> Optional[str]:
        if not self.page:
            return None

        previous_kind = self._deepseek_model_type_picker_kind
        detected_kind: Optional[str] = None
        for picker_kind in self.MODEL_TYPE_PICKER_ORDER:
            # Base required types for the picker
            required_types = [
                DEEPSEEK_MODEL_TYPE_DEFAULT,
                DEEPSEEK_MODEL_TYPE_EXPERT,
            ]
            if picker_kind == self.MODEL_TYPE_PICKER_NEW:
                # Vision identifies the new rollout; also check for alternative instant values
                required_types.append("vision")
                # Also accept alternative instant values that some rollouts might use
                required_types.extend(["instant", "instant-v3"])

            has_all_options = True
            for model_type in required_types:
                if not await self._has_visible_model_type_option(picker_kind, model_type):
                    has_all_options = False
                    break
            if has_all_options:
                detected_kind = picker_kind
                break

        self._deepseek_model_type_picker_kind = detected_kind
        if log_result:
            if detected_kind:
                label = self._model_type_picker_label(detected_kind)
                if detected_kind != previous_kind:
                    Logger.debug(f"DeepSeek model type picker detected: {label}.")
            else:
                Logger.debug(
                    "DeepSeek model type picker was not detected at startup. "
                    "It will be checked again before sending requests."
                )
        return detected_kind

    def _model_type_picker_attempt_order(self) -> List[str]:
        attempts: List[str] = []
        current = self._deepseek_model_type_picker_kind
        if current in self.MODEL_TYPE_PICKER_ORDER:
            attempts.append(current)
        for picker_kind in self.MODEL_TYPE_PICKER_ORDER:
            if picker_kind not in attempts:
                attempts.append(picker_kind)
        return attempts

    async def _is_model_type_option_selected(self, option) -> bool:
        aria_checked = await option.get_attribute("aria-checked")
        if aria_checked is not None:
            return str(aria_checked or "").strip().lower() == "true"

        class_attr = await option.get_attribute("class") or ""
        return "_31a22b0" in class_attr

    async def _try_set_model_type_with_picker(
        self,
        picker_kind: str,
        desired_type: str,
    ) -> tuple[bool, str]:
        selector = self._model_type_option_selector(picker_kind, desired_type)
        label = self._model_type_picker_label(picker_kind)
        option = self.page.locator(selector)
        display_name = self._model_type_display_name(desired_type)

        try:
            await option.first.wait_for(state="visible", timeout=4000)
        except Exception as exc:
            reason = self._format_picker_error(exc)
            return False, f"{label}: {display_name} option was not visible ({reason})"

        try:
            disabled = str(await option.first.get_attribute("aria-disabled") or "").strip().lower()
            if disabled == "true":
                return False, f"{label}: {display_name} option was disabled"

            if await self._is_model_type_option_selected(option.first):
                Logger.debug(f"DeepSeek model type is already '{desired_type}' via {label}.")
                return True, f"{label}: already selected"

            Logger.debug(f"Switching DeepSeek model type to '{desired_type}' via {label}...")
            await option.first.click(timeout=2000)
        except Exception as exc:
            reason = self._format_picker_error(exc)
            return False, f"{label}: {display_name} click failed ({reason})"

        try:
            await self.page.wait_for_function(
                """
                ([selector, selectedClass]) => {
                    const option = document.querySelector(selector);
                    if (!option) return false;
                    const ariaChecked = option.getAttribute('aria-checked');
                    if (ariaChecked !== null) {
                        return ariaChecked.toLowerCase() === 'true';
                    }
                    return option.classList.contains(selectedClass);
                }
                """,
                arg=[selector, "_31a22b0"],
                timeout=3000,
            )
        except Exception as exc:
            reason = self._format_picker_error(exc)
            Logger.debug(
                f"DeepSeek model type '{desired_type}' click completed via {label}, "
                "but selection confirmation timed out."
            )
            return False, f"{label}: {display_name} click did not confirm ({reason})"

        return True, f"{label}: clicked"

    async def set_model_type_state(self, model_type: str) -> None:
        desired_type = (
            DEEPSEEK_MODEL_TYPE_EXPERT
            if str(model_type or "").strip().lower() == DEEPSEEK_MODEL_TYPE_EXPERT
            else DEEPSEEK_MODEL_TYPE_DEFAULT
        )

        if not self.page:
            return

        if self._deepseek_model_type_picker_kind is None:
            await self._detect_model_type_picker_kind()

        previous_kind = self._deepseek_model_type_picker_kind
        failures: List[str] = []
        for picker_kind in self._model_type_picker_attempt_order():
            success, detail = await self._try_set_model_type_with_picker(picker_kind, desired_type)
            if success:
                self._deepseek_model_type_picker_kind = picker_kind
                if previous_kind and picker_kind != previous_kind:
                    Logger.warning(
                        "DeepSeek model type picker recovered with "
                        f"{self._model_type_picker_label(picker_kind)} after "
                        f"{self._model_type_picker_label(previous_kind)} failed."
                    )
                return
            failures.append(detail)

        tried = "; ".join(failures) if failures else "no picker attempts were available"
        Logger.warning(
            f"DeepSeek model type '{desired_type}' could not be applied. "
            f"Tried: {tried}. Continuing with the current DeepSeek mode."
        )

    async def set_sidebar_status(self, open: bool):
        """
        Sets the sidebar status to open or closed.
        """
        # Sidebar visibility is controlled via hashed classes that DeepSeek changes frequently.
        # when hidden, the sidebar container gains "a02af2e6" (removed when visible).

        sidebar_wrapper_selector = "div.dc04ec1d"
        sidebar_hidden_class = "a02af2e6"
        close_button_selector = "div.ds-icon-button._7d1f5e2"
        open_button_selector = "div.e5bf614e >> div.ds-icon-button._4f3769f >> nth=0"

        async def _is_sidebar_hidden() -> Optional[bool]:
            wrapper = self.page.locator(sidebar_wrapper_selector)
            if await wrapper.count() > 0:
                class_attr = await wrapper.first.get_attribute("class") or ""
                return sidebar_hidden_class in class_attr

            # Fallback heuristics if wrapper selector changes.
            close_btn = self.page.locator(close_button_selector)
            if await close_btn.count() > 0 and await close_btn.first.is_visible():
                return False

            open_btn = self.page.locator(open_button_selector)
            if await open_btn.count() > 0 and await open_btn.first.is_visible():
                return True

            return None

        is_hidden = await _is_sidebar_hidden()

        if open:
            if is_hidden is False:
                Logger.debug("Sidebar is already open.")
                return

            Logger.debug("Opening sidebar...")
            open_btn = self.page.locator(open_button_selector)
            if await open_btn.count() == 0:
                Logger.warning("Open sidebar button not found.")
                return

            try:
                await open_btn.click(timeout=2000)
            except Exception as e:
                Logger.warning(f"Failed to click open sidebar button: {e}")
                return

        else:
            if is_hidden is True:
                Logger.debug("Sidebar is already closed.")
                return

            Logger.debug("Closing sidebar...")
            close_btn = self.page.locator(close_button_selector)
            if await close_btn.count() == 0:
                Logger.warning("Close sidebar button not found.")
                return

            try:
                await close_btn.click(timeout=2000)
            except Exception as e:
                Logger.warning(f"Failed to click close sidebar button: {e}")
                return

    async def click_new_chat(self, source: str = "auto"):
        """
        Clicks the New Chat button.
        """
        # DeepSeek changes their UI classes often; still relying on hashes because the structure is messed up.
        # Sidebar open: "New chat" is a text button.
        # Sidebar closed: quick actions show 2 icon buttons.
        # Note: quick actions are removed from DOM when sidebar is open. The sidebar is NOT removed when closed, it just gets a new class.

        sidebar_wrapper_selector = "div.dc04ec1d"
        sidebar_hidden_class = "a02af2e6"
        quick_action_container_selector = "div.e5bf614e"
        quick_action_button_class = "_4f3769f"
        sidebar_new_chat_selector = "div._5a8ac7a.a084f19e"

        quick_new_chat_selector = (
            f"{quick_action_container_selector} >> div.ds-icon-button.{quick_action_button_class} >> nth=1"
        )

        async def _is_sidebar_hidden() -> Optional[bool]:
            wrapper = self.page.locator(sidebar_wrapper_selector)
            if await wrapper.count() > 0:
                class_attr = await wrapper.first.get_attribute("class") or ""
                return sidebar_hidden_class in class_attr

            # Fallback heuristics if wrapper selector changes.
            close_btn = self.page.locator("div.ds-icon-button._7d1f5e2")
            if await close_btn.count() > 0 and await close_btn.first.is_visible():
                return False

            quick_actions = self.page.locator(quick_action_container_selector)
            if await quick_actions.count() > 0 and await quick_actions.first.is_visible():
                return True

            return None

        async def _click_sidebar_new_chat() -> bool:
            btn = self.page.locator(sidebar_new_chat_selector)
            if await btn.count() > 0 and await btn.first.is_visible():
                is_disabled = (await btn.first.get_attribute("aria-disabled")) == "true"
                if not is_disabled:
                    Logger.debug("Clicking New Chat (Sidebar)...")
                    await btn.first.click(timeout=2000)
                    return True

            # Fallback: locate by visible text
            btn = self.page.locator("div[tabindex='0']", has_text="New chat")
            if await btn.count() > 0 and await btn.first.is_visible():
                Logger.debug("Clicking New Chat (Sidebar text)...")
                await btn.first.click(timeout=2000)
                return True

            return False

        async def _click_quick_new_chat() -> bool:
            btn = self.page.locator(quick_new_chat_selector)
            if await btn.count() > 0 and await btn.first.is_visible():
                is_disabled = (await btn.first.get_attribute("aria-disabled")) == "true"
                if not is_disabled:
                    Logger.debug("Clicking New Chat (Quick action)...")
                    await btn.first.click(timeout=2000)
                    return True
            return False

        try:
            if source == "sidebar":
                if not await _click_sidebar_new_chat():
                    Logger.warning("New Chat (Sidebar) button not found.")
                return

            if source == "simple":
                if not await _click_quick_new_chat():
                    Logger.warning("New Chat (Quick action) button not found.")
                return

            if source != "auto":
                Logger.warning(f"Unknown source: {source}")
                return

            Logger.debug("Attempting to click New Chat (Auto)...")
            is_hidden = await _is_sidebar_hidden()

            # If sidebar is open, quick actions are removed; prefer the sidebar button.
            if is_hidden is False:
                if await _click_sidebar_new_chat():
                    return
                # If our state detection is wrong or UI changed, try quick action as a fallback.
                if await _click_quick_new_chat():
                    return
                Logger.warning("Sidebar appears open, but New Chat button was not found.")
                return

            # If sidebar is hidden, quick actions should exist; prefer quick action.
            if is_hidden is True:
                if await _click_quick_new_chat():
                    return
                # Fallback: open sidebar and try the sidebar button.
                await self.set_sidebar_status(open=True)
                if await _click_sidebar_new_chat():
                    return
                Logger.warning("Sidebar appears hidden, but New Chat button was not found.")
                return

            # Unknown state: try both, then try opening sidebar as a last resort.
            if await _click_quick_new_chat():
                return
            if await _click_sidebar_new_chat():
                return

            await self.set_sidebar_status(open=True)
            if await _click_sidebar_new_chat():
                return

            Logger.warning("Could not find New Chat button in either mode.")
        except Exception as e:
            Logger.error(f"Error clicking New Chat: {e}")

    async def enter_message(self, message: str):
        """
        Public wrapper for entering a message.
        """
        await self._enter_message(message)

    async def send_message(self, timeout: int = None):
        """
        Public wrapper for sending a message.
        """
        await self._send_message(timeout=timeout)

    async def _enter_message(self, message: str):
        """
        Enters the message into the chat input textarea.
        """
        # The textarea has placeholder "Message DeepSeek"
        textarea = self.page.locator("textarea[placeholder='Message DeepSeek']")
        if await textarea.count() == 0:
            textarea = self.page.locator("textarea")
            if await textarea.count() == 0:
                Logger.warning("Message textarea not found.")
                return
        Logger.debug(f"Entering message: {message[:50]}..." if len(message) > 50 else f"Entering message: {message}")
        await textarea.first.fill(message)

    async def _send_message(self, timeout: int = None):
        """
        Clicks the send button if it is enabled.
        Waits up to timeout seconds for the button to become enabled.
        """
        # The send button hash in current DeepSeek UI.
        send_button = self._locate_send_control()
        
        if await send_button.count() > 0:
            await self._remember_send_control_signature(send_button)
            # If timeout is provided, wait for the button to be enabled
            if timeout and timeout > 0:
                Logger.debug(f"Waiting up to {timeout} seconds for send button to be enabled...")
                start_time = time.time()
                while time.time() - start_time < timeout:
                    if not await self._is_deepseek_control_disabled(send_button):
                        break
                    await asyncio.sleep(0.5)
            
            if not await self._is_deepseek_control_disabled(send_button):
                Logger.debug("Clicking send button...")
                await send_button.click()
            else:
                Logger.warning("Send button is disabled. Cannot send message.")
        else:
            Logger.warning("Send button could not be located.")

    async def _click_new_chat(self):
        """
        Clicks the New Chat button.
        """
        await self.click_new_chat(source="auto")

    async def _click_regenerate(self) -> bool:
        """
        Clicks the regenerate button.
        Returns True if successful, False otherwise.
        """
        try:
            strict_result = await self._click_regenerate_from_action_bar()
            if strict_result is not None:
                return strict_result

            # regenerate is the 2nd "control" icon-button
            # in the control bar that appears right after the last assistant message
            assistant_messages = self.page.locator("div.ds-message:has(.ds-markdown)")
            assistant_count = await assistant_messages.count()

            candidate_containers = []

            if assistant_count > 0:
                last_assistant = assistant_messages.nth(assistant_count - 1)
                # The control bar is typically the first ds-flex sibling after the message that contains icon buttons.
                candidate_containers.append(
                    last_assistant.locator(
                        "xpath=following-sibling::*[self::div[contains(@class,'ds-flex')] and .//div[contains(@class,'ds-icon-button')]][1]"
                    )
                )

            # Fallback: scan for the last visible ds-flex that has at least 2 icon buttons (excluding the input area).
            candidate_containers.append(self.page.locator("div.ds-flex:has(.ds-icon-button)"))

            for container in candidate_containers:
                container_count = await container.count()
                for i in range(container_count - 1, -1, -1):
                    bar = container.nth(i)
                    # Skip the composer/input area.
                    if await bar.locator("textarea").count() > 0:
                        continue

                    buttons = bar.locator(".ds-icon-button")
                    if await buttons.count() < 2:
                        continue

                    regen_button = buttons.nth(1)
                    if not await regen_button.is_visible():
                        continue

                    is_disabled = (await regen_button.get_attribute("aria-disabled")) == "true"
                    if is_disabled:
                        Logger.warning("Regenerate button is disabled (likely due to censorship).")
                        return False

                    Logger.debug("Clicking regenerate button...")
                    await regen_button.click()
                    return True

            Logger.warning("Regenerate button not found.")
            return False
        except Exception as e:
            Logger.error(f"Error clicking regenerate button: {e}")
            return False

    async def _click_regenerate_from_action_bar(self) -> Optional[bool]:
        """
        Click DeepSeek's current regenerate action.

        Newer DeepSeek builds leave unrelated icon-button ghosts in the DOM, so this
        path scopes the lookup to the real action bar container first.
        """
        containers = self.page.locator("div._965abe9:has(.db183363)")
        container_count = await containers.count()

        for i in range(container_count - 1, -1, -1):
            bar = containers.nth(i)

            if await bar.locator("textarea").count() > 0:
                continue
            if not await bar.is_visible():
                continue

            buttons = bar.locator(".db183363")
            if await buttons.count() < 2:
                continue

            regen_button = buttons.nth(1)
            if not await regen_button.is_visible():
                continue

            is_disabled = (await regen_button.get_attribute("aria-disabled")) == "true"
            if is_disabled:
                Logger.warning("Regenerate button is disabled (likely due to censorship).")
                return False

            Logger.debug("Clicking regenerate button from DeepSeek action bar...")
            await regen_button.click()
            return True

        return None

    async def upload_file(self, file_spec: Any) -> None:
        """
        Public wrapper for uploading a file.
        """
        await self._upload_file(file_spec)

    async def _upload_file(self, file_spec: Any) -> bool:
        """
        Uploads a file to the chat.

        file_spec can be a path (str/Path) or a file payload dict supported by Playwright
        (e.g. {"name": "...", "mimeType": "...", "buffer": b"..."}).

        Returns True when the file payload was attached, or False when upload is unavailable
        and the caller should use the regular text path.
        """
        try:
            if isinstance(file_spec, dict):
                name = file_spec.get("name", "<payload>")
                buffer = file_spec.get("buffer")
                size = len(buffer) if isinstance(buffer, (bytes, bytearray)) else None
                size_info = f" ({size} bytes)" if size is not None else ""
                Logger.debug(f"Uploading file payload: {name}{size_info}")
            else:
                Logger.debug(f"Uploading file: {file_spec}")
        except Exception:
            Logger.debug("Uploading file (details unavailable).")
        
        # The file input is hidden or styled, but we can target it by type="file"
        file_input = self.page.locator("input[type='file']")
        
        if await file_input.count() > 0:
            await file_input.set_input_files(file_spec)
            Logger.debug("File set to input.")
            
            # Wait a bit for the upload to be processed by the UI
            # You might need to wait for a specific indicator that the file is ready
            await asyncio.sleep(1.0) 
            return True
        else:
            Logger.warning("File input not found.")
            return False
