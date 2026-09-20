"""Browser driver for Google AI Studio, including UI control and stream interception."""

import asyncio
import base64
import codecs
import json
import math
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv

from drivers.base_driver import BaseDriver
from drivers.providers import DriverProvider
from drivers.shared_utils import (
    build_prompt_text_file_payload,
    clear_clean_regeneration_cache,
    extract_macro_overrides,
    format_request_messages,
    make_openai_delta_sse,
    resolve_rendered_injection,
    split_leading_system_messages,
    strip_macros_from_messages,
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


class _AiStudioJsonEventStreamParser:
    """Incrementally parse AI Studio's XSSI-prefixed nested JSON event stream."""

    def __init__(self) -> None:
        self._text_decoder = codecs.getincrementaldecoder("utf-8")()
        self._json_decoder = json.JSONDecoder()
        self._buffer = ""
        self._pos = 0
        self._outer_started = False
        self._inner_started = False
        self._inner_closed = False
        self._xssi_handled = False

    def feed(self, chunk: bytes) -> list[Any]:
        """Decode a response chunk and return any fully parsed JSON events."""
        self._buffer += self._text_decoder.decode(chunk, final=False)
        return self._drain(final=False)

    def finish(self) -> list[Any]:
        """Flush buffered decoder state and return any remaining parsed events."""
        self._buffer += self._text_decoder.decode(b"", final=True)
        return self._drain(final=True)

    def _skip_trivia(self) -> None:
        while self._pos < len(self._buffer) and self._buffer[self._pos] in " \r\n\t,":
            self._pos += 1

    def _handle_xssi_prefix(self) -> bool:
        if self._xssi_handled:
            return False

        self._skip_trivia()
        prefix = ")]}'"
        if self._buffer[self._pos : self._pos + len(prefix)] != prefix:
            self._xssi_handled = True
            return False

        self._pos += len(prefix)
        while self._pos < len(self._buffer) and self._buffer[self._pos] in "\r\n":
            self._pos += 1
        self._xssi_handled = True
        return True

    def _maybe_compact(self) -> None:
        if self._pos <= 0:
            return
        if self._pos < 65536 and self._pos < (len(self._buffer) // 2):
            return
        self._buffer = self._buffer[self._pos :]
        self._pos = 0

    def _drain(self, final: bool) -> list[Any]:
        """Drain as many complete JSON events as possible from the internal buffer."""
        out: list[Any] = []

        while True:
            if self._handle_xssi_prefix():
                continue

            if not self._outer_started:
                self._skip_trivia()
                if self._pos >= len(self._buffer):
                    break
                if self._buffer[self._pos] != "[":
                    if final:
                        self._pos += 1
                        continue
                    break
                self._outer_started = True
                self._pos += 1
                continue

            if not self._inner_started:
                self._skip_trivia()
                if self._pos >= len(self._buffer):
                    break
                if self._buffer[self._pos] == "]":
                    self._inner_closed = True
                    self._pos += 1
                    break
                if self._buffer[self._pos] != "[":
                    if final:
                        self._pos += 1
                        continue
                    break
                self._inner_started = True
                self._pos += 1
                continue

            if self._inner_closed:
                break

            self._skip_trivia()
            if self._pos >= len(self._buffer):
                break

            if self._buffer[self._pos] == "]":
                self._inner_closed = True
                self._pos += 1
                break

            try:
                value, end = self._json_decoder.raw_decode(self._buffer, self._pos)
            except json.JSONDecodeError:
                break

            out.append(value)
            self._pos = end

        self._maybe_compact()
        return out


class AIStudioDriver(BaseDriver):
    """Drive the Google AI Studio web UI and expose OpenAI-style streaming output."""

    REQUEST_CAPTURE_MODE_REPLAY = "replay"
    REQUEST_CAPTURE_MODE_CDP_TEEING = "cdp_teeing"
    START_URL = "https://aistudio.google.com/prompts/new_chat?temporary=true"
    AISTUDIO_HOST = "aistudio.google.com"
    AUTH_HOST_MARKER = "accounts.google.com"
    TEMPORARY_CHAT_QUERY_PARAM = "temporary"
    TEMPORARY_CHAT_QUERY_VALUE = "true"
    LEGACY_TEMPORARY_CHAT_QUERY_PARAM = "incognito"
    TEMPORARY_CHAT_BADGE_SELECTOR = "span[data-test-id='main-text']"
    TEMPORARY_CHAT_BADGE_LABEL = "Temporary chat"
    TEMPORARY_CHAT_BADGE_TIMEOUT_MS = 10000
    DEFAULT_MODEL_LABEL = "Gemini 3.1 Pro"
    DEFAULT_MAX_OUTPUT_TOKENS = 65536
    GEMINI_25_PAID_MODEL_ERROR = (
        "Gemini 2.5 models have become paid in Google AI Studio, and IRP can't serve them."
    )
    MODEL_FAMILY_FILTER_LABEL = "All"
    GENERATE_ROUTE_GLOB = (
        "**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent*"
    )
    GENERATE_URL_SUBSTRING = (
        "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"
    )
    CHAT_READY_SELECTORS = [
        "textarea[cdktextareaautosize]",
        "textarea[cdktextareaautosize='']",
    ]
    MODEL_SELECTOR_CARD_SELECTOR = "button.model-selector-card"
    THINKING_FORM_FIELD_SELECTOR = (
        ".mat-mdc-form-field.mat-mdc-form-field-type-mat-select."
        "mat-form-field-appearance-outline.mat-primary.mat-form-field-animations-enabled"
    )
    THINKING_FORM_FIELD_GRANDPARENT_ARIA_DESCRIBEDBY = "cdk-describedby-message-ng-1-167"
    THINKING_LISTBOX_SELECTOR = "div[role='listbox'] mat-option"
    THINKING_SELECT_LABELS = ("Thinking Level", "Thinking Effort")
    SEARCH_TOGGLE_LABEL = "Grounding with Google Search"
    URL_CONTEXT_TOGGLE_LABEL = "Browse the url context"
    THINKING_MODE_TOGGLE_LABEL = "Toggle thinking mode"
    THINKING_BUDGET_TOGGLE_LABEL = "Toggle thinking budget between auto and manual"
    THINKING_BUDGET_WRAPPER_SELECTOR = "[data-test-id='user-setting-budget-animation-wrapper']"
    TOP_P_TOOLTIP = "Probability threshold for top-p sampling"
    MAX_OUTPUT_TOKENS_INPUT_LABEL = "Maximum output tokens"
    ADVANCED_SETTINGS_LABEL = "Expand or collapse advanced settings"
    TERMS_DIALOG_SELECTOR = "div.mat-mdc-dialog-surface.mdc-dialog__surface"
    UPLOAD_ACK_DIALOG_TITLE_SELECTOR = "div.mat-mdc-dialog-title.mdc-dialog__title.shared-dialog-header"
    UPLOAD_ACK_DIALOG_CONTAINER_SELECTOR = "mat-dialog-container#copyright-acknowledgement-dialog"
    UPLOAD_ACK_ACCEPT_BUTTON_SELECTOR = (
        "mat-dialog-container#copyright-acknowledgement-dialog "
        "button[aria-label='Agree to the copyright acknowledgement']"
    )
    PROMPT_MEDIA_CONTAINER_SELECTOR = "[data-test-id='prompt-media-container']"
    ADD_MEDIA_BUTTON_SELECTORS = [
        "button[data-test-id='add-media-button']",
        "[data-test-id='add-media-button']",
        "button[aria-label='Insert images, videos, audio, or files']",
        "button.mat-mdc-menu-trigger[aria-label*='files']",
    ]
    UPLOAD_FILE_MENU_ITEM_SELECTORS = [
        "button.upload-file-menu-item",
        ".upload-file-menu-item",
        "button[role='menuitem']:has-text('Upload files')",
        "[role='menuitem']:has-text('Upload files')",
        "button:has-text('Upload files')",
    ]
    UPLOAD_FILE_INPUT_SELECTORS = [
        "input.file-input[type='file']",
        "input[type='file'][accept*='text/*']",
        "input[type='file']",
    ]
    ASSISTANT_TURN_SELECTOR = "div.chat-turn-container.code-block-aligner.model.render.ng-star-inserted"
    ASSISTANT_EDIT_BUTTON_SELECTORS = [
        "button.toggle-edit-button",
        "button[class*='toggle-edit-button']",
    ]
    ASSISTANT_EDIT_TEXTAREA_SELECTORS = [
        "textarea.textarea",
        "textarea[class*='textarea']",
        "textarea",
    ]
    ASSISTANT_EDIT_SAVE_BUTTON_SELECTORS = [
        "button[data-test-id*='save']",
        "button[name*='save']",
        "button[aria-label='Save']",
        "button[aria-label*='Save']",
        "button:has-text('Save')",
        "button:has-text('Done')",
        "button:has-text('Update')",
    ]
    SAFETY_RATINGS_BUTTON_SELECTOR = "button[aria-label*='Safety Ratings']"
    RUN_SAFETY_SETTINGS_PANEL_SELECTOR = "div.run-safety-settings"
    RUN_SAFETY_SETTINGS_CLOSE_SELECTORS = [
        "button[aria-label='Close Run Safety Settings']",
    ]
    SYSTEM_INSTRUCTIONS_STORAGE_KEY = "aistudio_all_system_instructions"
    SYSTEM_INSTRUCTIONS_CARD_SELECTORS = [
        "button.system-instructions-card",
        "[data-test-id='system-instructions-card']",
        ".system-instructions-card button",
        ".system-instructions-card",
    ]
    SYSTEM_INSTRUCTIONS_TITLE_INPUT_SELECTORS = [
        ".title-row input",
        ".title-row > input",
    ]
    SYSTEM_INSTRUCTIONS_TEXTAREA_SELECTORS = [
        "textarea.mat-mdc-tooltip-trigger.in-run-settings",
        "textarea.in-run-settings.mat-mdc-tooltip-trigger",
        "textarea.in-run-settings",
    ]
    SYSTEM_INSTRUCTIONS_SAVE_STATUS_SELECTOR = "div.saving-status"
    SYSTEM_INSTRUCTIONS_CLOSE_SELECTORS = [
        "button[data-test-close-button]",
        "[data-test-close-button]",
    ]
    SEND_BUTTON_SELECTORS = [
        "button[mattooltipclass='run-button-tooltip']",
        "[mattooltipclass='run-button-tooltip']",
        "button[data-test-id='run-button-tooltip']",
        "[data-test-id='run-button-tooltip'] button",
        "button[name='run-button']",
        "button[name='run-button-tooltip']",
    ]
    STOP_BUTTON_SELECTORS = [
        "button[mattooltipclass='run-button-tooltip']",
        "[mattooltipclass='run-button-tooltip']",
        "button[data-test-id='run-button-tooltip']",
        "[data-test-id='run-button-tooltip'] button",
        "button[name='run-button']",
        "button[name='run-button-tooltip']",
    ]
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
    INTERCEPT_FIRST_CHUNK_TIMEOUT_S = 180.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    SEND_CLICK_CONFIRM_TIMEOUT_MS = 2200
    SEND_CLICK_MAX_ATTEMPTS = 3
    MAX_ANTI_CENSORSHIP_NUDGES = 3
    CAARS_MEANINGFUL_CHUNK_TARGET = 5
    BACKGROUND_RESILIENCE_BROWSER_ARGS: tuple[str, ...] = (
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    )
    RATE_LIMIT_HINT_RE = re.compile(
        r"(rate\s*limit|too\s*many\s*requests|\b429\b|quota|limit\s*reached)",
        flags=re.IGNORECASE,
    )
    LOWEST_LEVEL_BY_MODEL: Dict[str, str] = {
        "gemini-3.8-flash": "Low",
        "gemini-3.8-flash-preview": "Low",
        "gemini-3.7-flash": "Low",
        "gemini-3.7-flash-preview": "Low",
        "gemini-3.6-flash": "Low",
        "gemini-3.6-flash-preview": "Low",
        "gemini-3.5-flash": "Minimal",
        "gemini-3.1-pro-preview": "Low",
        "gemini-3.1-flash-lite": "Minimal",
        "gemini-3-flash-preview": "Minimal",
        "gemini-2.5-pro": "Minimal",
        "gemini-2.5-flash": "Minimal",
        "gemini-2.5-flash-lite": "Minimal",
        "gemma-4-26b-a4b-it": "Minimal",
        "gemma-4-31b-it": "Minimal",
    }
    THINKING_LEVELS_BY_MODEL: Dict[str, tuple[str, ...]] = {
        "gemini-3.8-flash": ("Low", "Medium", "High"),
        "gemini-3.8-flash-preview": ("Low", "Medium", "High"),
        "gemini-3.7-flash": ("Low", "Medium", "High"),
        "gemini-3.7-flash-preview": ("Low", "Medium", "High"),
        "gemini-3.6-flash": ("Low", "Medium", "High"),
        "gemini-3.6-flash-preview": ("Low", "Medium", "High"),
        "gemini-3.5-flash": ("Minimal", "Low", "Medium", "High"),
        "gemini-3.1-pro-preview": ("Low", "Medium", "High"),
        "gemini-3.1-flash-lite": ("Minimal", "Low", "Medium", "High"),
        "gemini-3-flash-preview": ("Minimal", "Low", "Medium", "High"),
        "gemini-2.5-pro": ("Minimal", "Low", "Medium", "High"),
        "gemini-2.5-flash": ("Minimal", "Low", "Medium", "High"),
        "gemini-2.5-flash-lite": ("Minimal", "Low", "Medium", "High"),
        "gemma-4-26b-a4b-it": ("Minimal", "High"),
        "gemma-4-31b-it": ("Minimal", "High"),
    }
    THINKING_LEVEL_ORDER = ("Minimal", "Low", "Medium", "High")
    THINKING_MODE_TOGGLE_MODELS = {"gemini-2.5-flash", "gemini-2.5-flash-lite"}
    THINKING_BUDGET_BY_MODEL: Dict[str, Dict[str, Optional[int]]] = {
        "gemini-2.5-pro": {
            "Minimal": 128,
            "Low": 8192,
            "Medium": 16384,
            "High": 32768,
        },
        "gemini-2.5-flash": {
            "Minimal": None,
            "Low": 8192,
            "Medium": 16384,
            "High": 24576,
        },
        "gemini-2.5-flash-lite": {
            "Minimal": None,
            "Low": 8192,
            "Medium": 16384,
            "High": 24576,
        },
    }
    THINKING_BUDGET_RANGE_BY_MODEL: Dict[str, tuple[int, int]] = {
        "gemini-2.5-pro": (128, 32768),
        "gemini-2.5-flash": (1, 24576),
        "gemini-2.5-flash-lite": (512, 24576),
    }
    MODEL_CONFIGS: Dict[str, Dict[str, Any]] = {
        "Gemini 3.8 Flash": {
            "base_id": "gemini-3.8-flash",
            "selector_id": "model-carousel-row-models/gemini-3.8-flash",
            "supports_temperature": False,
            "supports_top_p": False,
        },
        "Gemini 3.7 Flash": {
            "base_id": "gemini-3.7-flash",
            "selector_id": "model-carousel-row-models/gemini-3.7-flash",
            "supports_temperature": False,
            "supports_top_p": False,
        },
        "Gemini 3.6 Flash": {
            "base_id": "gemini-3.6-flash",
            "selector_id": "model-carousel-row-models/gemini-3.6-flash",
            "supports_temperature": False,
            "supports_top_p": False,
        },
        "Gemini 3.5 Flash": {
            "base_id": "gemini-3.5-flash",
            "selector_id": "model-carousel-row-models/gemini-3.5-flash",
            "supports_temperature": False,
            "supports_top_p": False,
        },
        "Gemini 3.1 Pro": {
            "base_id": "gemini-3.1-pro-preview",
            "selector_id": "model-carousel-row-models/gemini-3.1-pro-preview",
        },
        "Gemini 3.1 Flash Lite": {
            "base_id": "gemini-3.1-flash-lite",
            "selector_id": "model-carousel-row-models/gemini-3.1-flash-lite",
            "supports_temperature": False,
            "supports_top_p": False,
        },
        "Gemini 3 Flash": {
            "base_id": "gemini-3-flash-preview",
            "selector_id": "model-carousel-row-models/gemini-3-flash-preview",
        },
        "Gemini 2.5 Pro": {
            "base_id": "gemini-2.5-pro",
            "selector_id": "model-carousel-row-models/gemini-2.5-pro",
        },
        "Gemini 2.5 Flash": {
            "base_id": "gemini-2.5-flash",
            "selector_id": "model-carousel-row-models/gemini-2.5-flash",
        },
        "Gemini 2.5 Flash Lite": {
            "base_id": "gemini-2.5-flash-lite",
            "selector_id": "model-carousel-row-models/gemini-2.5-flash-lite",
        },
        "Gemma 4 26B-A4B": {
            "base_id": "gemma-4-26b-a4b-it",
            "selector_id": "model-carousel-row-models/gemma-4-26b-a4b-it",
            "force_search": True,
            "supports_url_context": False,
            "max_output_tokens": 32768,
        },
        "Gemma 4 31B": {
            "base_id": "gemma-4-31b-it",
            "selector_id": "model-carousel-row-models/gemma-4-31b-it",
            "force_search": True,
            "supports_url_context": False,
            "max_output_tokens": 32768,
        },
    }
    CLEAN_REGEN_STATE_KEYS = (
        "deepthink_enabled",
        "thinking_level",
        "send_deepthink",
        "search_enabled",
        "url_context_enabled",
        "use_system_prompt_field",
        "system_prompt_text",
        "send_as_text_file",
        "text_file_message",
        "anti_censorship",
        "anti_censorship_replacement_message",
        "anti_censorship_continue_nudge",
        "caars_enabled",
        "caars_savior_model",
        "temperature",
        "top_p",
        "max_output_tokens",
        "model_base_id",
    )
    AI_STUDIO_MACRO_ACTIONS: Dict[str, tuple[str, Any]] = {
        "think": ("deepthink_enabled", True),
        "nothink": ("deepthink_enabled", False),
        "no_think": ("deepthink_enabled", False),
        "r0": ("deepthink_enabled", False),
        "r1": ("thinking_level_macro", "r1"),
        "r2": ("thinking_level_macro", "r2"),
        "r3": ("thinking_level_macro", "r3"),
        "r4": ("thinking_level_macro", "r4"),
        "search": ("search_enabled", True),
        "nosearch": ("search_enabled", False),
        "no_search": ("search_enabled", False),
        "no-search": ("search_enabled", False),
        "url": ("url_context_enabled", True),
        "urlcontext": ("url_context_enabled", True),
        "url_context": ("url_context_enabled", True),
        "nourl": ("url_context_enabled", False),
        "no_url": ("url_context_enabled", False),
        "no-url": ("url_context_enabled", False),
        "nocaars": ("caars_enabled", False),
        "nocars": ("caars_enabled", False),
    }

    def __init__(self, config_manager):
        """Initialize the driver and session-scoped AI Studio state."""
        super().__init__(config_manager=config_manager, provider=DriverProvider.AI_STUDIO)
        self.cache_manager = CacheManager()
        self.current_model: Optional[str] = None
        self.current_send_deepthink: Optional[bool] = None
        self.thinking_active = False
        self.clean_regen_message_cache_key = "aistudio_last_message.txt"
        self.clean_regen_state_cache_key = "aistudio_last_message_state.json"
        self._safety_filters_initialized = False
        self._system_instructions_storage_reset_done = False
        self._preflight_task: Optional[asyncio.Task] = None
        self._preflight_ready = False
        self._preflight_state: Optional[Dict[str, Any]] = None
        self._preflight_system_prompt_text = ""
        self._assume_english_ui_notice_logged = False
        self._minimized_hmm_notice_sent = False
        self._last_mouse_position: tuple[float, float] | None = None

    def get_start_url(self) -> str:
        return self.START_URL

    def _get_browser_launch_args(self) -> list[str]:
        """Add AI Studio-only launch flags that keep hidden windows responsive."""
        args = super()._get_browser_launch_args()
        for arg in self.BACKGROUND_RESILIENCE_BROWSER_ARGS:
            if arg not in args:
                args.append(arg)
        return args

    def should_apply_configured_model_before_request(self) -> bool:
        """Let the AI Studio request flow own model selection."""
        return False

    @classmethod
    def _is_aistudio_url(cls, url: str) -> bool:
        try:
            hostname = (urlsplit(str(url or "")).hostname or "").lower()
        except Exception:
            return False
        return hostname == cls.AISTUDIO_HOST

    @classmethod
    def _url_has_temporary_enabled(cls, url: str) -> bool:
        try:
            query_pairs = parse_qsl(urlsplit(str(url or "")).query, keep_blank_values=True)
        except Exception:
            return False
        return any(
            key == cls.TEMPORARY_CHAT_QUERY_PARAM
            and str(value).strip().lower() == cls.TEMPORARY_CHAT_QUERY_VALUE
            for key, value in query_pairs
        )

    @classmethod
    def _with_temporary_enabled(cls, url: str) -> str:
        parts = urlsplit(str(url or ""))
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        updated_pairs = []
        temporary_applied = False

        for key, value in query_pairs:
            if key == cls.LEGACY_TEMPORARY_CHAT_QUERY_PARAM:
                continue
            if key == cls.TEMPORARY_CHAT_QUERY_PARAM:
                if temporary_applied:
                    continue
                temporary_applied = True
                updated_pairs.append((key, cls.TEMPORARY_CHAT_QUERY_VALUE))
                continue
            updated_pairs.append((key, value))

        if not temporary_applied:
            updated_pairs.append(
                (cls.TEMPORARY_CHAT_QUERY_PARAM, cls.TEMPORARY_CHAT_QUERY_VALUE)
            )

        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(updated_pairs), parts.fragment)
        )

    async def _ensure_temporary_chat_url(self, timeout_ms: int = 60000) -> None:
        """Keep AI Studio on a temporary-chat URL after redirects that can strip it."""
        if not self.page:
            return

        current_url = await self._current_url()
        if not self._is_aistudio_url(current_url):
            return

        target_url = self._with_temporary_enabled(current_url)
        if not self._url_has_temporary_enabled(current_url):
            Logger.info(
                "Google AI Studio: current chat URL is missing ?temporary=true. "
                "Re-opening with temporary chat enabled..."
            )
            try:
                await self._navigate_to_start_url(target_url)
            except Exception as e:
                Logger.warning(
                    f"Google AI Studio: failed to re-open current URL with ?temporary=true: {e}. "
                    "Retrying the default temporary chat URL..."
                )
                try:
                    await self._navigate_to_start_url(self.START_URL)
                except Exception as fallback_error:
                    Logger.warning(
                        "Google AI Studio: default temporary chat navigation also failed: "
                        f"{fallback_error}"
                    )
                    return

        if not await self._wait_for_chat_ready(timeout_ms=timeout_ms):
            return

        if await self._wait_for_temporary_chat_badge(
            timeout_ms=self.TEMPORARY_CHAT_BADGE_TIMEOUT_MS
        ):
            return

        Logger.warning(
            "Google AI Studio: temporary chat URL loaded, but the Temporary chat badge "
            "was not detected."
        )

    async def after_start(self, status_callback: Optional[Callable[[str], None]] = None) -> None:
        """Run post-start checks and one-time UI initialization for AI Studio."""
        await self._ensure_temporary_chat_url(timeout_ms=60000)
        await self._accept_terms_of_service_if_present(timeout_ms=1500)
        await self.check_ui_language(status_callback=status_callback)
        clear_clean_regeneration_cache(
            self.cache_manager,
            self.clean_regen_message_cache_key,
            self.clean_regen_state_cache_key,
        )
        await self._clear_persisted_system_instructions_if_needed()
        await self._ensure_safety_filters_initialized()

    def _use_system_prompt_field_enabled(self) -> bool:
        try:
            return bool(self.config_manager.get_setting("aistudio_behavior", "use_system_prompt_field"))
        except Exception:
            return False

    async def _clear_persisted_system_instructions_if_needed(self) -> None:
        """Clear AI Studio's locally persisted system instructions once per driver start."""
        if self._system_instructions_storage_reset_done:
            return
        if not self._use_system_prompt_field_enabled():
            return
        if not self.page:
            return

        try:
            storage_info = await self.page.evaluate(
                """(storageKey) => {
                    try {
                        const raw = window.localStorage.getItem(storageKey);
                        if (raw === null) {
                            return { exists: false, count: 0, titles: [] };
                        }

                        let parsed = null;
                        try {
                            parsed = JSON.parse(raw);
                        } catch (e) {}

                        const titles = Array.isArray(parsed)
                            ? parsed
                                .filter((item) => item && typeof item === 'object')
                                .slice(0, 5)
                                .map((item) => (item.title || '').toString())
                            : [];

                        return {
                            exists: true,
                            count: Array.isArray(parsed) ? parsed.length : 0,
                            titles,
                            rawLength: raw.length,
                        };
                    } catch (e) {
                        return {
                            exists: false,
                            error: (e && e.message) ? e.message.toString() : String(e),
                        };
                    }
                }""",
                self.SYSTEM_INSTRUCTIONS_STORAGE_KEY,
            )
        except Exception as e:
            Logger.debug(f"Google AI Studio: failed to inspect system-instruction storage: {e}")
            return

        if not isinstance(storage_info, dict):
            return

        error_message = str(storage_info.get("error") or "").strip()
        if error_message:
            Logger.debug(
                "Google AI Studio: could not inspect persisted system instructions "
                f"before cleanup: {error_message}"
            )
            return

        if not bool(storage_info.get("exists")):
            self._system_instructions_storage_reset_done = True
            return

        titles = [
            str(title or "").strip()
            for title in list(storage_info.get("titles") or [])
            if str(title or "").strip()
        ]
        titles_preview = f" titles={titles}" if titles else ""
        Logger.info(
            "Google AI Studio: clearing persisted system instructions from local storage "
            f"(count={int(storage_info.get('count') or 0)}, rawLength={int(storage_info.get('rawLength') or 0)})"
            f"{titles_preview}."
        )

        try:
            cleared = await self.page.evaluate(
                """(storageKey) => {
                    try {
                        window.localStorage.removeItem(storageKey);
                        return true;
                    } catch (e) {
                        return false;
                    }
                }""",
                self.SYSTEM_INSTRUCTIONS_STORAGE_KEY,
            )
        except Exception as e:
            Logger.warning(
                f"Google AI Studio: failed to clear persisted system instructions from local storage: {e}"
            )
            return

        if not cleared:
            Logger.warning(
                "Google AI Studio: local-storage cleanup for persisted system instructions "
                "did not complete."
            )
            return

        Logger.info("Google AI Studio: refreshing the page after system-instruction cleanup...")
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            Logger.warning(
                f"Google AI Studio: page reload after system-instruction cleanup failed: {e}. "
                "Retrying via normal navigation..."
            )
            try:
                await self._navigate_to_start_url(self.START_URL)
            except Exception as nav_error:
                Logger.warning(
                    f"Google AI Studio: fallback navigation after storage cleanup failed: {nav_error}"
                )
                return

        await self._accept_terms_of_service_if_present(timeout_ms=1500)
        await self._ensure_temporary_chat_url(timeout_ms=60000)
        await self._wait_for_chat_ready(timeout_ms=60000)
        await self.check_ui_language()
        self._system_instructions_storage_reset_done = True

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    @classmethod
    def _canonicalize_text(cls, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", cls._normalize_text(value))

    async def _find_first_visible(
        self,
        selectors: List[str],
        timeout_ms: int = 0,
        poll_interval_s: float = 0.15,
    ):
        """Return the first visible locator that matches any selector in the list.

        Args:
            selectors: Candidate selectors checked in order.
            timeout_ms: Maximum time to keep polling for a visible element.
            poll_interval_s: Delay between polling rounds when waiting.

        Returns:
            The first visible locator, or ``None`` if nothing becomes visible.
        """
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

    async def _find_first_visible_within(
        self,
        root,
        selectors: List[str],
        timeout_ms: int = 0,
        poll_interval_s: float = 0.15,
    ):
        """Return the first visible child locator inside ``root`` that matches any selector."""
        if root is None:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = root.locator(selector)
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

    async def _wait_for_chat_ready(self, timeout_ms: int = 60000) -> bool:
        """Return whether the chat composer becomes visible within the timeout."""
        return await self._find_first_visible(self.CHAT_READY_SELECTORS, timeout_ms=timeout_ms) is not None

    async def _wait_for_temporary_chat_badge(self, timeout_ms: int = 10000) -> bool:
        """Return whether AI Studio shows its visible Temporary chat indicator."""
        if not self.page:
            return False

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        wanted_label = self._normalize_text(self.TEMPORARY_CHAT_BADGE_LABEL)

        while True:
            locator = self.page.locator(self.TEMPORARY_CHAT_BADGE_SELECTOR)
            try:
                count = await locator.count()
            except Exception:
                count = 0

            for idx in range(min(count, 10)):
                item = locator.nth(idx)
                try:
                    if not await item.is_visible():
                        continue
                    text = str(await item.inner_text() or "")
                except Exception:
                    continue

                if self._normalize_text(text) == wanted_label:
                    return True

            if timeout_ms <= 0 or time.time() >= deadline:
                return False

            await asyncio.sleep(0.2)

    async def _current_url(self) -> str:
        if not self.page:
            return ""
        try:
            return str(self.page.url or "")
        except Exception:
            return ""

    async def _is_logged_in(self) -> bool:
        return bool(await self._wait_for_chat_ready(timeout_ms=0))

    async def _detect_login_state(self) -> str:
        """Classify the current page as chat, auth, or unknown."""
        if await self._is_logged_in():
            return "chat"

        current_url = await self._current_url()
        if self.AUTH_HOST_MARKER in current_url:
            return "auth"

        auth_fields = await self._find_first_visible(
            self.GOOGLE_EMAIL_SELECTORS + self.GOOGLE_PASSWORD_SELECTORS,
            timeout_ms=0,
        )
        if auth_fields is not None:
            return "auth"

        return "unknown"

    async def _wait_for_login_state(self, timeout_ms: int = 15000) -> str:
        """Poll until the page clearly looks like chat or Google auth."""
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        last_state = "unknown"
        while True:
            state = await self._detect_login_state()
            last_state = state
            if state != "unknown":
                return state
            if time.time() >= deadline:
                return last_state
            await asyncio.sleep(0.35)

    async def _wait_until_logged_in(self, timeout_ms: int = 0) -> bool:
        """Wait until the chat composer appears, optionally with a timeout."""
        start = time.time()
        timeout_s = 0.0 if timeout_ms <= 0 else (float(timeout_ms) / 1000.0)
        while True:
            if await self._is_logged_in():
                return True
            if timeout_s > 0.0 and (time.time() - start) >= timeout_s:
                return False
            await asyncio.sleep(0.4)

    async def _human_delay(self, delay_s: float = 0.8) -> None:
        delay = max(0.0, float(delay_s))
        if self._humanize_mouse_movements_enabled() and delay > 0.0:
            delay = (delay * random.uniform(0.85, 1.25)) + random.uniform(0.03, 0.16)
        await asyncio.sleep(delay)

    def _humanize_mouse_movements_enabled(self) -> bool:
        """Return whether AI Studio should use slower, pointer-visible UI actions."""
        try:
            return bool(
                self.config_manager.get_setting(
                    "aistudio_behavior",
                    "humanize_mouse_movements",
                )
            )
        except Exception:
            return False

    async def _restore_minimized_window_for_hmm(self) -> None:
        """Restore AI Studio if Chromium is minimized before humanized pointer work."""
        if not self.page or not self.context:
            return
        if not self._humanize_mouse_movements_enabled():
            return

        cdp_session = None
        try:
            cdp_session = await self.context.new_cdp_session(self.page)
            window_info = await cdp_session.send("Browser.getWindowForTarget")
            bounds = window_info.get("bounds") if isinstance(window_info, dict) else None
            if not isinstance(bounds, dict):
                return

            window_state = str(bounds.get("windowState") or "").strip().lower()
            if window_state != "minimized":
                return

            window_id = window_info.get("windowId")
            await cdp_session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"windowState": "normal"},
                },
            )
            try:
                await self.page.bring_to_front()
            except Exception:
                pass

            Logger.warning(
                "Google AI Studio: browser window was minimized while Humanize Mouse "
                "Movements was enabled; restored it before a pointer action."
            )
            if not self._minimized_hmm_notice_sent:
                self._minimized_hmm_notice_sent = True
                self.notify_user(
                    "Google AI Studio Browser Restored",
                    "AI Studio was minimized with Humanize Mouse Movements enabled, so IntenseRP restored the browser window. Check the app for more info.",
                    level="warning",
                    dialog_message=(
                        "AI Studio was minimized while Humanize Mouse Movements was enabled, "
                        "so IntenseRP restored the browser window before clicking.\n\n"
                        "Minimized AI Studio windows can miss clicks, hover actions, and "
                        "file-picker actions. You can still leave the browser unfocused: "
                        "just open another window or focus a different one instead of "
                        "minimizing the AI Studio browser."
                    ),
                )
            await asyncio.sleep(0.15)
        except Exception as e:
            Logger.debug(
                "Google AI Studio: minimized-window restore check failed before "
                f"humanized action: {e}"
            )
        finally:
            if cdp_session is not None:
                try:
                    await cdp_session.detach()
                except Exception:
                    pass

    async def _humanized_action_pause(
        self,
        min_s: float = 0.04,
        max_s: float = 0.14,
    ) -> None:
        """Add a tiny optional pause around UI actions when paced interactions are enabled."""
        if not self._humanize_mouse_movements_enabled():
            return

        low = max(0.0, float(min_s))
        high = max(low, float(max_s))
        await asyncio.sleep(random.uniform(low, high))

    @staticmethod
    def _click_timeout_for_probe(timeout: Any, cap_ms: float = 1500.0) -> float:
        try:
            value = float(timeout)
        except Exception:
            value = cap_ms
        if value <= 0:
            return 0.0
        return min(value, cap_ms)

    def _mouse_steps_for_target(self, target: tuple[float, float] | None) -> int:
        if target is None or self._last_mouse_position is None:
            return random.randint(12, 24)

        distance = math.hypot(
            float(target[0]) - float(self._last_mouse_position[0]),
            float(target[1]) - float(self._last_mouse_position[1]),
        )
        base_steps = int(distance / 18.0) + random.randint(4, 10)
        return max(8, min(48, base_steps))

    async def _humanized_click_position(
        self,
        locator,
        *,
        timeout: Any = 3000,
    ) -> tuple[dict[str, float] | None, tuple[float, float] | None]:
        """Pick a slightly varied visible click point inside a locator."""
        try:
            await locator.scroll_into_view_if_needed(
                timeout=self._click_timeout_for_probe(timeout)
            )
        except Exception:
            pass

        try:
            box = await locator.bounding_box(
                timeout=self._click_timeout_for_probe(timeout)
            )
        except Exception:
            box = None
        if not box:
            return None, None

        try:
            left = float(box.get("x") or 0.0)
            top = float(box.get("y") or 0.0)
            width = float(box.get("width") or 0.0)
            height = float(box.get("height") or 0.0)
        except Exception:
            return None, None
        if width <= 0.0 or height <= 0.0:
            return None, None

        x_margin = min(width * 0.25, 14.0)
        y_margin = min(height * 0.25, 10.0)
        rel_x = (
            width / 2.0
            if width <= (x_margin * 2.0)
            else random.uniform(x_margin, width - x_margin)
        )
        rel_y = (
            height / 2.0
            if height <= (y_margin * 2.0)
            else random.uniform(y_margin, height - y_margin)
        )
        return {"x": rel_x, "y": rel_y}, (left + rel_x, top + rel_y)

    async def _click_locator(
        self,
        locator,
        *,
        timeout: Any = 3000,
        force: bool | None = None,
        position: dict[str, float] | None = None,
        button: str | None = None,
        click_count: int | None = None,
        no_wait_after: bool | None = None,
    ) -> None:
        """Click a locator, optionally adding native Playwright pointer movement and pacing."""
        kwargs: dict[str, Any] = {"timeout": timeout}
        if force is not None:
            kwargs["force"] = force
        if button is not None:
            kwargs["button"] = button
        if click_count is not None:
            kwargs["click_count"] = click_count
        if no_wait_after is not None:
            kwargs["no_wait_after"] = no_wait_after

        if not self._humanize_mouse_movements_enabled():
            if position is not None:
                kwargs["position"] = position
            await locator.click(**kwargs)
            return

        await self._restore_minimized_window_for_hmm()

        target = None
        click_position = position
        if click_position is None:
            click_position, target = await self._humanized_click_position(
                locator,
                timeout=timeout,
            )
        if click_position is not None:
            kwargs["position"] = click_position

        kwargs["delay"] = random.uniform(45.0, 140.0)
        kwargs["steps"] = self._mouse_steps_for_target(target)

        await self._humanized_action_pause(0.04, 0.16)
        try:
            await self._restore_minimized_window_for_hmm()
            await locator.click(**kwargs)
            if target is not None:
                self._last_mouse_position = target
        finally:
            await self._humanized_action_pause(0.06, 0.20)

    async def _move_mouse_to_point(self, x: float, y: float) -> None:
        if not self.page:
            return

        x = float(x)
        y = float(y)
        if not self._humanize_mouse_movements_enabled():
            await self.page.mouse.move(x, y)
            self._last_mouse_position = (x, y)
            return

        await self._restore_minimized_window_for_hmm()

        start = self._last_mouse_position
        if start is not None:
            distance = math.hypot(x - float(start[0]), y - float(start[1]))
            if distance > 70.0:
                dx = x - float(start[0])
                dy = y - float(start[1])
                px = -dy / distance
                py = dx / distance
                bend = min(distance * 0.18, 42.0) * random.uniform(-1.0, 1.0)
                t = random.uniform(0.35, 0.65)
                mid_x = float(start[0]) + (dx * t) + (px * bend)
                mid_y = float(start[1]) + (dy * t) + (py * bend)
                await self.page.mouse.move(
                    mid_x,
                    mid_y,
                    steps=max(4, self._mouse_steps_for_target((mid_x, mid_y)) // 2),
                )
                self._last_mouse_position = (mid_x, mid_y)

        await self.page.mouse.move(x, y, steps=self._mouse_steps_for_target((x, y)))
        self._last_mouse_position = (x, y)

    async def _click_mouse_point(self, x: float, y: float) -> None:
        if not self.page:
            return

        if not self._humanize_mouse_movements_enabled():
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.04)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.08)
            self._last_mouse_position = (float(x), float(y))
            return

        await self._humanized_action_pause(0.03, 0.12)
        await self._move_mouse_to_point(float(x), float(y))
        await self._humanized_action_pause(0.04, 0.13)
        await self._restore_minimized_window_for_hmm()
        await self.page.mouse.click(
            float(x),
            float(y),
            delay=random.uniform(45.0, 130.0),
        )
        self._last_mouse_position = (float(x), float(y))
        await self._humanized_action_pause(0.06, 0.18)

    async def _accept_terms_of_service_if_present(self, timeout_ms: int = 3500) -> None:
        """Pause automation while the AI Studio legal acknowledgement dialog is open."""
        if not self.page:
            return

        dialog = await self._find_first_visible([self.TERMS_DIALOG_SELECTOR], timeout_ms=timeout_ms)
        if dialog is None:
            return

        Logger.warning(
            "Google AI Studio: ToS acknowledgement dialog detected. "
            "Waiting for the user to review and accept it manually..."
        )
        self.notify_user(
            "Google AI Studio Terms",
            "Please review and accept the AI Studio terms/legal acknowledgement in the browser window. "
            "IntenseRP will continue automatically once the modal disappears.",
            level="warning",
        )

        while True:
            dialog = await self._find_first_visible([self.TERMS_DIALOG_SELECTOR], timeout_ms=0)
            if dialog is None:
                break
            await asyncio.sleep(0.1)

        await self._ui_settle_pause(0.35)

    async def _enter_google_login_value(self, selectors: List[str], value: str) -> bool:
        """Fill one Google sign-in step and submit it with Enter."""
        field = await self._find_first_visible(selectors, timeout_ms=15000)
        if field is None:
            return False

        try:
            await self._click_locator(field, timeout=3000)
        except Exception:
            pass

        await self._human_delay(0.55)

        try:
            await self.page.keyboard.press("Control+A")
        except Exception:
            pass
        try:
            await self.page.keyboard.press("Backspace")
        except Exception:
            pass

        try:
            await self.page.keyboard.insert_text(str(value or ""))
        except Exception:
            try:
                await field.fill(str(value or ""))
            except Exception:
                return False

        await self._human_delay(0.75)

        try:
            await self.page.keyboard.press("Enter")
        except Exception:
            return False

        return True

    async def _perform_google_auto_login(self, email: str, password: str) -> bool:
        """Attempt a full Google login flow with the configured credentials."""
        entered_email = await self._enter_google_login_value(self.GOOGLE_EMAIL_SELECTORS, email)
        if not entered_email and not await self._is_logged_in():
            return False

        if await self._is_logged_in():
            return True

        await self._human_delay(1.0)

        password_ready = await self._find_first_visible(self.GOOGLE_PASSWORD_SELECTORS, timeout_ms=15000)
        if password_ready is None and await self._is_logged_in():
            return True
        if password_ready is None:
            return False

        entered_password = await self._enter_google_login_value(self.GOOGLE_PASSWORD_SELECTORS, password)
        if not entered_password:
            return False

        timeout_s = self._get_auto_login_redirect_timeout_s()
        return await self._wait_until_logged_in(timeout_ms=int(timeout_s * 1000.0))

    def _get_auto_login_redirect_timeout_s(self) -> float:
        try:
            value = int(self.config_manager.get_setting("aistudio_behavior", "auto_login_redirect_timeout") or 15)
        except Exception:
            value = 15
        return float(min(max(value, 5), 120))

    def _assume_english_ui_enabled(self) -> bool:
        try:
            return bool(self.config_manager.get_setting("aistudio_behavior", "assume_english_ui"))
        except Exception:
            return False

    async def login(self) -> None:
        """Ensure the browser reaches an authenticated AI Studio chat session.

        The flow first detects whether the user is already on the chat page, then
        attempts Auto Login when configured, and finally falls back to a manual
        login wait if Google requires additional user interaction.
        """
        if not self.page:
            return

        login_state = await self._wait_for_login_state(timeout_ms=12000)
        if login_state == "chat":
            await self._ensure_temporary_chat_url(timeout_ms=60000)
            await self._accept_terms_of_service_if_present(timeout_ms=2500)
            Logger.info("Google AI Studio: already signed in.")
            self._mark_active_ece_pair_used()
            return

        if login_state == "unknown":
            try:
                await self._navigate_to_start_url(self.START_URL)
            except Exception:
                pass
            login_state = await self._wait_for_login_state(timeout_ms=12000)
            if login_state == "chat":
                await self._ensure_temporary_chat_url(timeout_ms=60000)
                await self._accept_terms_of_service_if_present(timeout_ms=2500)
                Logger.info("Google AI Studio: already signed in.")
                self._mark_active_ece_pair_used()
                return

        auto_login = False
        try:
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        except Exception:
            auto_login = False

        if auto_login:
            pair = self.ece_active_pair()
            if pair and str(pair.email or "").strip() and str(pair.password or "").strip():
                Logger.info("Google AI Studio: Auto Login enabled. Attempting Google sign-in...")
                ok = await self._perform_google_auto_login(pair.email, pair.password)
                if ok:
                    try:
                        await self._navigate_to_start_url(self.START_URL)
                    except Exception:
                        pass
                    await self._wait_until_logged_in(timeout_ms=60000)
                    await self._ensure_temporary_chat_url(timeout_ms=60000)
                    await self._accept_terms_of_service_if_present(timeout_ms=4000)
                    Logger.success("Google AI Studio: login detected.")
                    self.ece_mark_used(pair.email)
                    return

                Logger.warning(
                    "Google AI Studio: Auto Login could not complete cleanly. "
                    "Falling back to manual completion in the browser."
                )
                self._warn_profile_compatibility_after_auth_failure(
                    "Google AI Studio Auto Login could not complete cleanly."
                )
                self.notify_user(
                    "Google AI Studio Login",
                    "Auto Login filled what it could, but Google still needs manual completion. "
                    "Finish the login flow in the browser, then return to IntenseRP.",
                    level="warning",
                )
            elif auto_login:
                Logger.warning(
                    "Google AI Studio: Auto-login is enabled but no Google AI Studio account is configured "
                    "in Credential Manager. Waiting for manual login..."
                )

        if not auto_login:
            Logger.info("Google AI Studio: Auto Login disabled. Waiting for manual Google login...")
            self.notify_user(
                "Google AI Studio Login",
                "Please complete the Google login flow in the browser window, then come back here.",
                level="info",
            )

        await self._wait_until_logged_in(timeout_ms=0)
        await self._accept_terms_of_service_if_present(timeout_ms=4000)
        Logger.success("Google AI Studio: login detected.")
        try:
            await self._navigate_to_start_url(self.START_URL)
        except Exception:
            pass
        await self._ensure_temporary_chat_url(timeout_ms=60000)

    async def _get_document_lang(self) -> str:
        """Read the active document language, allowing Google auth pages temporarily."""
        if not self.page:
            return ""

        current_url = await self._current_url()
        if self.AUTH_HOST_MARKER in current_url:
            # Google login can temporarily use whatever language the account/browser is already in
            # We only enforce English on the actual AI Studio page
            return "en"

        if self._assume_english_ui_enabled():
            if not self._assume_english_ui_notice_logged:
                self._assume_english_ui_notice_logged = True
                Logger.warning(
                    "Google AI Studio: Assume English UI is enabled; skipping the document "
                    "language check. Only use this when the visible AI Studio UI is English."
                )
            return "en"

        try:
            lang = await self.page.evaluate(
                "() => {"
                "  const el = document.documentElement;"
                "  if (!el) return '';"
                "  return (el.getAttribute('lang') || el.lang || '').toString();"
                "}"
            )
        except Exception as e:
            Logger.debug(f"Google AI Studio: failed to read document language: {e}")
            return ""

        return str(lang or "").strip()

    @property
    def required_ui_language_label(self) -> str:
        return "English (en / en-US)"

    @property
    def ui_language_change_target_label(self) -> str:
        return "your Google account language"

    def _get_configured_model_label(self) -> str:
        try:
            value = str(self.config_manager.get_setting("aistudio_behavior", "model") or "").strip()
        except Exception:
            value = ""
        return value if value in self.MODEL_CONFIGS else self.DEFAULT_MODEL_LABEL

    def api_real_model_labels(self) -> list[str]:
        return list(self.MODEL_CONFIGS.keys())

    def _get_model_label_for_request(self, model: Any = None) -> str:
        return self._resolve_model_label_for_request(model)

    def _resolve_model_label_for_request(
        self,
        model: Any = None,
        *,
        model_label_override: str | None = None,
    ) -> str:
        configured_label = self._get_configured_model_label()
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        if not override and model:
            raw = str(model).strip()
            if raw in self.MODEL_CONFIGS:
                override = raw
            else:
                canon = self._canonicalize_text(raw)
                for label, cfg in self.MODEL_CONFIGS.items():
                    if canon in (
                        self._canonicalize_text(label),
                        self._canonicalize_text(cfg.get("base_id") or ""),
                    ):
                        override = label
                        break
        override_label = str(model_label_override or override or "").strip()
        return override_label if override_label in self.MODEL_CONFIGS else configured_label

    @classmethod
    def _model_config_for_label(cls, label: str) -> Dict[str, Any]:
        return dict(cls.MODEL_CONFIGS.get(label) or cls.MODEL_CONFIGS[cls.DEFAULT_MODEL_LABEL])

    @staticmethod
    def _is_paid_gemini_25_model_config(model_config: Dict[str, Any]) -> bool:
        model_base_id = str(model_config.get("base_id") or "").strip().lower()
        return model_base_id.startswith("gemini-2.5-")

    @staticmethod
    def _normalize_account_email(email: Any) -> str:
        return str(email or "").strip().lower()

    @staticmethod
    def _normalize_paid_model_access_email_list(raw_value: Any) -> set[str]:
        if isinstance(raw_value, str):
            raw_items = re.split(r"[\s,;]+", raw_value)
        elif isinstance(raw_value, (list, tuple, set)):
            raw_items = raw_value
        else:
            raw_items = ()

        emails: set[str] = set()
        for raw_item in raw_items:
            email = AIStudioDriver._normalize_account_email(raw_item)
            if email:
                emails.add(email)
        return emails

    def _assume_paid_model_access_enabled(self) -> bool:
        try:
            return bool(self.config_manager.get_setting("aistudio_behavior", "assume_paid_model_access"))
        except Exception:
            return False

    def _paid_model_access_emails(self) -> set[str]:
        try:
            raw_value = self.config_manager.get_setting(
                "aistudio_behavior",
                "paid_model_access_emails",
            )
        except Exception:
            raw_value = None
        return self._normalize_paid_model_access_email_list(raw_value)

    def _active_account_email(self) -> str:
        try:
            pair = self.ece_active_pair()
        except Exception:
            pair = None
        return self._normalize_account_email(getattr(pair, "email", None) if pair else None)

    def _paid_gemini_25_access_allowed_for_current_account(self) -> bool:
        if not self._assume_paid_model_access_enabled():
            return False

        allowed_emails = self._paid_model_access_emails()
        if not allowed_emails:
            return True

        active_email = self._active_account_email()
        return bool(active_email and active_email in allowed_emails)

    def _paid_gemini_25_model_error(self) -> str:
        if not self._assume_paid_model_access_enabled():
            return (
                f"{self.GEMINI_25_PAID_MODEL_ERROR} Enable Assume Paid Model Access only "
                "for AI Studio accounts that you know can use Gemini 2.5."
            )

        allowed_emails = self._paid_model_access_emails()
        if allowed_emails:
            active_email = self._active_account_email()
            account_label = f"`{active_email}`" if active_email else "the current manual account"
            return (
                "Gemini 2.5 models require paid access in Google AI Studio. "
                "Assume Paid Model Access is enabled, but "
                f"{account_label} is not listed in Paid Model Access Emails."
            )

        return self.GEMINI_25_PAID_MODEL_ERROR

    def _raise_if_model_unavailable(self, model_config: Dict[str, Any]) -> None:
        if (
            self._is_paid_gemini_25_model_config(model_config)
            and not self._paid_gemini_25_access_allowed_for_current_account()
        ):
            raise RuntimeError(self._paid_gemini_25_model_error())

    def can_handle_request_model(self, model: Any = None) -> bool:
        """Return whether this account-backed driver can handle the requested model."""
        desired_label = self._resolve_model_label_for_request(model)
        model_config = self._model_config_for_label(desired_label)
        if not self._is_paid_gemini_25_model_config(model_config):
            return True
        return self._paid_gemini_25_access_allowed_for_current_account()

    def validate_request_model_available(self, model: Any = None) -> None:
        desired_label = self._resolve_model_label_for_request(model)
        self._raise_if_model_unavailable(self._model_config_for_label(desired_label))

    def validate_explicit_request_model_available(self, model: Any = None) -> None:
        desired_label = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        if desired_label:
            self._raise_if_model_unavailable(self._model_config_for_label(desired_label))

    @classmethod
    def _model_max_output_tokens(cls, model_config: Dict[str, Any]) -> int:
        return cls._clamp_int(
            model_config.get("max_output_tokens"),
            cls.DEFAULT_MAX_OUTPUT_TOKENS,
            1,
            cls.DEFAULT_MAX_OUTPUT_TOKENS,
        )

    @staticmethod
    def _model_supports_url_context(model_config: Dict[str, Any]) -> bool:
        return bool(model_config.get("supports_url_context", True))

    @staticmethod
    def _model_supports_temperature(model_config: Dict[str, Any]) -> bool:
        return bool(model_config.get("supports_temperature", True))

    @staticmethod
    def _model_supports_top_p(model_config: Dict[str, Any]) -> bool:
        return bool(model_config.get("supports_top_p", True))

    @staticmethod
    def _model_forces_search(model_config: Dict[str, Any]) -> bool:
        return bool(model_config.get("force_search", False))

    @classmethod
    def _current_model_matches(
        cls,
        current_label: str,
        desired_label: str,
        model_config: Dict[str, Any],
    ) -> bool:
        current = cls._canonicalize_text(current_label)
        if not current:
            return False

        base_id = str(model_config.get("base_id") or "").strip()
        selector_tail = str(model_config.get("selector_id") or "").rsplit("/", 1)[-1]
        candidates = [
            desired_label,
            base_id,
            selector_tail,
        ]
        if base_id:
            candidates.extend([f"{base_id}-preview", f"{base_id}preview"])
        if desired_label:
            candidates.extend([f"{desired_label} Preview", f"{desired_label}Preview"])

        for candidate in candidates:
            if not candidate:
                continue
            cand_canon = cls._canonicalize_text(candidate)
            if current == cand_canon or cand_canon in current or current in cand_canon:
                return True
        return False

    async def _read_current_model_id(self) -> str:
        """Read the currently selected model label from the model selector card."""
        if not self.page:
            return ""

        try:
            current = await self.page.evaluate(
                "() => {"
                "  const span = "
                "    document.querySelector(\"button.model-selector-card span[data-test-id='model-name']\") || "
                "    document.querySelector(\"button.model-selector-card span[class*='model-name']\");"
                "  if (!span) return '';"
                "  return (span.textContent || '').toString().trim();"
                "}"
            )
        except Exception as e:
            Logger.debug(f"Google AI Studio: failed to read current model label: {e}")
            return ""

        return str(current or "").strip()

    async def _open_model_selector(self, timeout_ms: int = 6000) -> bool:
        """Open the model picker and wait for its carousel to appear."""
        if not self.page:
            return False

        trigger = self.page.locator(self.MODEL_SELECTOR_CARD_SELECTOR)
        if await trigger.count() == 0:
            Logger.warning("Google AI Studio: model selector trigger not found.")
            return False

        try:
            await self._click_locator(trigger.first, timeout=3000)
        except Exception as e:
            Logger.warning(f"Google AI Studio: failed to open model selector: {e}")
            return False

        try:
            await self.page.wait_for_selector(
                "[data-test-id='model-carousel-in-selector']",
                timeout=int(timeout_ms),
                state="visible",
            )
            return True
        except Exception:
            Logger.warning("Google AI Studio: model selector did not appear.")
            return False

    async def _click_model_family_filter(self) -> bool:
        if not self.page:
            return False

        try:
            root = self.page.locator("[data-test-id='model-carousel-in-selector']")
            buttons = root.locator("button")
            count = await buttons.count()
        except Exception:
            count = 0

        target_label = self._normalize_text(self.MODEL_FAMILY_FILTER_LABEL)
        for idx in range(min(count, 24)):
            button = buttons.nth(idx)
            try:
                if not await button.is_visible():
                    continue
                text = self._normalize_text(str(await button.inner_text() or ""))
                if text != target_label:
                    continue
                await self._click_locator(button, timeout=2500)
                await self._ui_settle_pause(0.2)
                return True
            except Exception:
                continue

        try:
            clicked = await self.page.evaluate(
                "(familyLabel) => {"
                "  const root = document.querySelector(\"[data-test-id='model-carousel-in-selector']\");"
                "  if (!root) return false;"
                "  const target = (familyLabel || '').toString().trim().toLowerCase();"
                "  const buttons = Array.from(root.querySelectorAll('button'));"
                "  for (const btn of buttons) {"
                "    const text = (btn.textContent || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();"
                "    if (text === target) {"
                "      btn.click();"
                "      return true;"
                "    }"
                "  }"
                "  return false;"
                "}",
                self.MODEL_FAMILY_FILTER_LABEL,
            )
        except Exception:
            clicked = False

        if clicked:
            await asyncio.sleep(0.2)
        return bool(clicked)

    async def _click_model_option(
        self,
        selector_id: str,
        base_id: str = "",
        label: str = "",
    ) -> bool:
        """Click a visible model option by its picker button id or text."""
        if not self.page:
            return False

        targets = [
            f"div.model-options-container button[id='{selector_id}']",
            f"button[id='{selector_id}']",
        ]
        if base_id:
            targets.extend([
                f"div.model-options-container button[id*='{base_id}']",
                f"button[id*='{base_id}']",
            ])

        for sel in targets:
            target = self.page.locator(sel)
            count = 0
            try:
                count = await target.count()
            except Exception:
                count = 0

            for idx in range(min(count, 8)):
                candidate = target.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception:
                    pass

                try:
                    await self._click_locator(candidate, timeout=3000)
                    return True
                except Exception:
                    continue

        if label:
            canon_label = self._canonicalize_text(label)
            try:
                container = self.page.locator("div.model-options-container button")
                btn_count = await container.count()
                for idx in range(min(btn_count, 30)):
                    btn = container.nth(idx)
                    if await btn.is_visible():
                        txt = self._canonicalize_text(await btn.inner_text())
                        if canon_label in txt or txt in canon_label:
                            await self._click_locator(btn, timeout=3000)
                            return True
            except Exception:
                pass

        return False

    async def _ensure_model_selected(self, desired_label: str) -> None:
        """Best-effort apply the configured AI Studio model in the picker UI."""
        desired = self._model_config_for_label(desired_label)
        desired_base = str(desired.get("base_id") or "").strip()
        desired_selector = str(desired.get("selector_id") or "").strip()
        if not desired_base or not desired_selector:
            return

        current = await self._read_current_model_id()
        if self._current_model_matches(current, desired_label, desired):
            return

        if not await self._open_model_selector():
            return

        await self._click_model_family_filter()
        if not await self._click_model_option(desired_selector, desired_base, desired_label):
            Logger.warning(
                f"Google AI Studio: target model '{desired_label}' was not found in the picker."
            )
            return

        deadline = time.time() + 5.0
        while time.time() < deadline:
            current = await self._read_current_model_id()
            if self._current_model_matches(current, desired_label, desired):
                return
            await asyncio.sleep(0.12)

        Logger.warning(
            f"Google AI Studio: clicked model '{desired_label}' but could not confirm the selection."
        )

    async def apply_configured_model(self, model: Any = None) -> None:
        """Apply the currently configured model label to the AI Studio UI."""
        desired_label = self._get_model_label_for_request(model)
        self._raise_if_model_unavailable(self._model_config_for_label(desired_label))
        try:
            await self._ensure_model_selected(desired_label)
        except Exception as e:
            Logger.warning(
                f"Google AI Studio: Failed to apply model selection '{desired_label}': {e}"
            )

    @staticmethod
    def _normalize_thinking_level(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"minimal", "min"}:
            return "Minimal"
        if normalized == "low":
            return "Low"
        if normalized in {"med", "medium"}:
            return "Medium"
        if normalized == "high":
            return "High"
        return ""

    def _configured_thinking_level(self) -> str:
        """Return the configured thinking level normalized to a supported label."""
        try:
            value = str(self.config_manager.get_setting("aistudio_behavior", "thinking_level") or "").strip()
        except Exception:
            value = ""
        normalized = self._normalize_thinking_level(value)
        return normalized or "Medium"

    @classmethod
    def _closest_supported_thinking_level(cls, wanted: str, supported: tuple[str, ...]) -> str:
        """Return the nearest thinking level the selected model actually exposes."""
        if not supported:
            return ""
        if wanted in supported:
            return wanted

        try:
            wanted_rank = cls.THINKING_LEVEL_ORDER.index(wanted)
        except ValueError:
            return supported[0]

        def score(level: str) -> tuple[int, bool, int]:
            try:
                rank = cls.THINKING_LEVEL_ORDER.index(level)
            except ValueError:
                return (999, True, 999)
            return (abs(rank - wanted_rank), rank > wanted_rank, rank)

        return min(supported, key=score)

    @classmethod
    def _resolve_macro_thinking_level(cls, token: str, model_base_id: str) -> str:
        """Map macro tokens like ``r1``-``r4`` onto model-supported thinking levels."""
        normalized = str(token or "").strip().lower()
        supported = cls.THINKING_LEVELS_BY_MODEL.get(str(model_base_id or "").strip().lower()) or ()
        if not supported:
            return ""

        if normalized in {"minimal", "low", "medium", "high"}:
            wanted = cls._normalize_thinking_level(normalized)
            return cls._closest_supported_thinking_level(wanted, supported)

        if normalized == "r1":
            return supported[0]
        if normalized == "r2":
            return supported[1] if len(supported) > 1 else supported[-1]
        if normalized == "r3":
            return supported[2] if len(supported) > 2 else supported[-1]
        if normalized == "r4":
            return supported[-1]
        return ""

    @classmethod
    def _default_low_thinking_level(cls, model_base_id: str) -> str:
        return cls.LOWEST_LEVEL_BY_MODEL.get(str(model_base_id or "").strip().lower(), "")

    @classmethod
    def _thinking_budget_for_level(cls, model_base_id: str, level: str) -> Optional[int]:
        model_key = str(model_base_id or "").strip().lower()
        normalized = cls._normalize_thinking_level(level)
        budget_map = cls.THINKING_BUDGET_BY_MODEL.get(model_key) or {}
        if normalized not in budget_map:
            return None

        budget = budget_map.get(normalized)
        if budget is None:
            return None

        min_value, max_value = cls.THINKING_BUDGET_RANGE_BY_MODEL.get(model_key, (1, budget))
        return cls._clamp_int(budget, budget, min_value, max_value)

    def _extract_model_level_override(self, model: str) -> tuple[str, Dict[str, Any]]:
        """Split suffixes like ``-high`` or ``-r2`` into a base model and overrides."""
        normalized = str(model or "").strip()
        overrides: Dict[str, Any] = {}
        if not normalized:
            return normalized, overrides

        for suffix in ("-minimal", "-low", "-medium", "-high", "-r0", "-r1", "-r2", "-r3", "-r4"):
            if not normalized.lower().endswith(suffix):
                continue
            stripped = normalized[: -len(suffix)].rstrip("-")
            token = suffix[1:]
            if token == "r0":
                overrides["deepthink_enabled"] = False
            else:
                overrides["deepthink_enabled"] = True
                overrides["thinking_level_macro"] = token
            return stripped, overrides

        return normalized, overrides

    def _parse_config_float(
        self,
        category_key: str,
        field_key: str,
        *,
        default: float,
        min_value: float,
        max_value: float,
    ) -> float:
        """Read, validate, and clamp a floating-point config value."""
        raw = None
        try:
            raw = self.config_manager.get_setting(category_key, field_key)
        except Exception:
            raw = None

        if raw is None or str(raw).strip() == "":
            return default

        try:
            parsed = float(raw)
        except Exception:
            Logger.warning(
                f"Google AI Studio: invalid float setting '{category_key}.{field_key}'={raw!r}. "
                f"Falling back to {default}."
            )
            return default

        return max(min(parsed, max_value), min_value)

    @staticmethod
    def _clamp_int(value: Any, default: int, min_value: int, max_value: int) -> int:
        """Convert a value to ``int`` and clamp it into the requested range."""
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(min(parsed, max_value), min_value)

    def _resolve_ai_studio_request_settings(
        self,
        model: str,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        overrides: Optional[Dict[str, Any]] = None,
        model_label_override: str | None = None,
    ) -> Dict[str, Any]:
        """Resolve the effective per-request AI Studio settings.

        This merges behavior-mode defaults, config values, explicit request parameters,
        and prompt-level macro overrides into the normalized settings dict used by the
        UI automation layer.
        """
        requested_model, model_suffix_overrides = self._extract_model_level_override(model)
        merged_overrides: Dict[str, Any] = {}
        if overrides:
            merged_overrides.update(overrides)
        if model_suffix_overrides:
            merged_overrides.update(model_suffix_overrides)

        mode = resolve_behavior_mode(
            requested_model,
            self.provider,
            real_model_labels=self.api_real_model_labels(),
        )

        desired_label = self._resolve_model_label_for_request(
            requested_model,
            model_label_override=model_label_override,
        )
        model_config = self._model_config_for_label(desired_label)
        self._raise_if_model_unavailable(model_config)
        model_base_id = str(model_config.get("base_id") or "").strip().lower()
        force_search = self._model_forces_search(model_config)
        supports_url_context = self._model_supports_url_context(model_config)
        supports_temperature = self._model_supports_temperature(model_config)
        supports_top_p = self._model_supports_top_p(model_config)
        model_max_output_tokens = self._model_max_output_tokens(model_config)

        deepthink_enabled = bool(self.config_manager.get_setting("aistudio_behavior", "enable_deepthink"))
        send_deepthink = bool(self.config_manager.get_setting("aistudio_behavior", "send_deepthink"))
        search_enabled = bool(self.config_manager.get_setting("aistudio_behavior", "enable_search"))
        url_context_enabled = bool(self.config_manager.get_setting("aistudio_behavior", "enable_url_context"))
        use_system_prompt_field = self._use_system_prompt_field_enabled()
        send_as_text_file = bool(self.config_manager.get_setting("aistudio_behavior", "send_as_text_file"))
        text_file_message = str(
            self.config_manager.get_setting("aistudio_behavior", "text_file_message") or ""
        )
        anti_censorship = bool(self.config_manager.get_setting("aistudio_behavior", "anti_censorship"))
        anti_censorship_replacement_message = str(
            self.config_manager.get_setting(
                "aistudio_behavior",
                "anti_censorship_replacement_message",
            )
            or "."
        )
        anti_censorship_continue_nudge = str(
            self.config_manager.get_setting(
                "aistudio_behavior",
                "anti_censorship_continue_nudge",
            )
            or "Continue."
        )
        caars_enabled = bool(
            anti_censorship
            and self.config_manager.get_setting("aistudio_behavior", "caars_enabled")
        )
        caars_savior_model = str(
            self.config_manager.get_setting("aistudio_behavior", "caars_savior_model")
            or ""
        ).strip()
        if caars_savior_model not in self.MODEL_CONFIGS:
            caars_savior_model = "Gemini 3.1 Flash Lite"

        if mode == MODE_CHAT:
            deepthink_enabled = False
            send_deepthink = False
        elif mode == MODE_REASONER:
            deepthink_enabled = True

        if "deepthink_enabled" in merged_overrides:
            deepthink_enabled = bool(merged_overrides["deepthink_enabled"])
        if "send_deepthink" in merged_overrides:
            send_deepthink = bool(merged_overrides["send_deepthink"])
        if "search_enabled" in merged_overrides:
            search_enabled = bool(merged_overrides["search_enabled"])
        if "url_context_enabled" in merged_overrides:
            url_context_enabled = bool(merged_overrides["url_context_enabled"])
        if "send_as_text_file" in merged_overrides:
            send_as_text_file = bool(merged_overrides["send_as_text_file"])
        if "caars_enabled" in merged_overrides:
            caars_enabled = bool(caars_enabled and merged_overrides["caars_enabled"])
        if force_search:
            search_enabled = True
        if not supports_url_context:
            url_context_enabled = False
        configured_thinking_level = self._configured_thinking_level()
        macro_level_token = str(merged_overrides.get("thinking_level_macro") or "").strip()
        if macro_level_token:
            thinking_level = self._resolve_macro_thinking_level(macro_level_token, model_base_id)
        elif deepthink_enabled:
            thinking_level = self._resolve_macro_thinking_level(configured_thinking_level, model_base_id)
        else:
            thinking_level = self._default_low_thinking_level(model_base_id)

        if temperature is None:
            temperature_value = self._parse_config_float(
                "aistudio_behavior",
                "temperature",
                default=1.0,
                min_value=0.0,
                max_value=2.0,
            )
        else:
            temperature_value = max(min(float(temperature), 2.0), 0.0)

        if top_p is None:
            top_p_value = self._parse_config_float(
                "aistudio_behavior",
                "top_p",
                default=0.95,
                min_value=0.0,
                max_value=1.0,
            )
        else:
            top_p_value = max(min(float(top_p), 1.0), 0.0)

        if max_tokens is None:
            raw_max = self.config_manager.get_setting("aistudio_behavior", "max_output_tokens")
        else:
            raw_max = max_tokens
        max_output_tokens = self._clamp_int(raw_max, model_max_output_tokens, 1, model_max_output_tokens)
        file_upload_timeout = self._clamp_int(
            self.config_manager.get_setting("aistudio_behavior", "file_upload_timeout"),
            20,
            1,
            120,
        )

        return {
            "requested_model": requested_model,
            "model_label": desired_label,
            "model_base_id": model_base_id,
            "deepthink_enabled": bool(deepthink_enabled),
            "thinking_level": thinking_level,
            "send_deepthink": bool(send_deepthink),
            "search_enabled": bool(search_enabled),
            "force_search": bool(force_search),
            "url_context_enabled": bool(url_context_enabled),
            "supports_url_context": bool(supports_url_context),
            "supports_temperature": bool(supports_temperature),
            "supports_top_p": bool(supports_top_p),
            "use_system_prompt_field": bool(use_system_prompt_field),
            "send_as_text_file": bool(send_as_text_file),
            "text_file_message": text_file_message,
            "anti_censorship": bool(anti_censorship),
            "anti_censorship_replacement_message": anti_censorship_replacement_message,
            "anti_censorship_continue_nudge": anti_censorship_continue_nudge,
            "caars_enabled": bool(caars_enabled),
            "caars_savior_model": caars_savior_model,
            "file_upload_timeout": int(file_upload_timeout),
            "temperature": float(temperature_value),
            "top_p": float(top_p_value),
            "max_output_tokens": int(max_output_tokens),
            "model_max_output_tokens": int(model_max_output_tokens),
        }

    def _message_format_separator(self) -> str:
        """Return the separator used by the shared formatting pipeline."""
        apply_formatting = bool(self.config_manager.get_setting("formatting", "apply_formatting"))
        if not apply_formatting:
            return "\n"
        divider = self.config_manager.get_setting("formatting", "formatting_divider") or ""
        return str(divider).replace("\\n", "\n")

    @staticmethod
    def _message_content_as_text(message: Any) -> str:
        """Convert a message object's content into plain text."""
        content = None
        try:
            content = getattr(message, "content")
        except Exception:
            content = None
        if content is None and isinstance(message, dict):
            content = message.get("content", "")
        return "" if content is None else str(content)

    def _strip_leading_rendered_injection(self, text: str, injection_text: str) -> str:
        """Remove one rendered injection prefix from a formatted prompt."""
        if not text or not injection_text:
            return text
        if text == injection_text:
            return ""
        if text.startswith(injection_text):
            remainder = text[len(injection_text) :]
            return remainder[1:] if remainder.startswith("\n") else remainder
        return text

    def _strip_formatted_prefix(self, text: str, prefix: str) -> str:
        """Remove a formatted message prefix and its following separator."""
        if not text or not prefix:
            return text
        if text == prefix:
            return ""
        if not text.startswith(prefix):
            return text

        remainder = text[len(prefix) :]
        separator = self._message_format_separator()
        if separator and remainder.startswith(separator):
            return remainder[len(separator) :]
        if remainder.startswith("\n"):
            return remainder[1:]
        return remainder

    def _prepare_prompt_payload(self, message_for_formatting: Union[str, List[Any]]) -> tuple[str, str]:
        """Build the chat payload and optional AI Studio system-instructions text."""
        formatted_message = self._format_messages(message_for_formatting)
        if isinstance(message_for_formatting, str):
            return formatted_message, ""
        if not self._use_system_prompt_field_enabled():
            return formatted_message, ""

        system_prompt_parts: list[str] = []
        leading_system_messages: list[Any] = []
        if isinstance(message_for_formatting, list):
            leading_system_messages, _ = split_leading_system_messages(message_for_formatting)
            for item in leading_system_messages:
                content = self._message_content_as_text(item).strip()
                if content:
                    system_prompt_parts.append(content)

        injection_position, rendered_injection = resolve_rendered_injection(
            self.config_manager,
            message_for_formatting,
        )
        use_before_injection = (
            bool(rendered_injection)
            and str(injection_position or "").strip().lower() == "before"
        )
        if use_before_injection:
            rendered_injection = str(rendered_injection or "").strip()
            if rendered_injection:
                system_prompt_parts.append(rendered_injection)
                formatted_message = self._strip_leading_rendered_injection(
                    formatted_message,
                    rendered_injection,
                )

        if leading_system_messages:
            leading_prefix = self._format_messages(leading_system_messages)
            if use_before_injection and rendered_injection:
                leading_prefix = self._strip_leading_rendered_injection(
                    leading_prefix,
                    rendered_injection,
                )
            formatted_message = self._strip_formatted_prefix(formatted_message, leading_prefix)

        system_prompt_text = "\n\n".join(part for part in system_prompt_parts if part)
        return formatted_message, system_prompt_text

    async def _read_toggle_state_by_aria_label(self, aria_label: str) -> Optional[bool]:
        """Best-effort read a switch state using the control's aria-label."""
        if not self.page:
            return None

        try:
            state = await self.page.evaluate(
                """(label) => {
                    const normalize = (value) => (value || '').toString().trim();
                    const root =
                        document.querySelector(`[aria-label="${label}"]`) ||
                        document.querySelector(`button[aria-label="${label}"]`);
                    if (!root) return null;

                    const target =
                        root.closest('.mdc-switch') ||
                        root.querySelector('.mdc-switch') ||
                        root;

                    const className = normalize(target.getAttribute('class'));
                    if (className.includes('mdc-switch--selected') || className.includes('mdc-switch--checked')) {
                        return true;
                    }
                    if (className.includes('mdc-switch--unselected')) {
                        return false;
                    }

                    const ariaChecked =
                        normalize(target.getAttribute('aria-checked')) ||
                        normalize(root.getAttribute('aria-checked'));
                    if (ariaChecked === 'true') return true;
                    if (ariaChecked === 'false') return false;
                    return null;
                }""",
                aria_label,
            )
        except Exception:
            state = None

        if isinstance(state, bool):
            return state
        return None

    async def _set_toggle_state_by_aria_label(self, aria_label: str, state: bool) -> None:
        """Toggle a labeled switch only when it differs from the requested state."""
        current = await self._read_toggle_state_by_aria_label(aria_label)
        if current is not None and current == state:
            return

        await self._dismiss_transient_overlays()
        toggle = await self._find_first_visible(
            [f"[aria-label='{aria_label}']", f"button[aria-label='{aria_label}']"],
            timeout_ms=8000,
        )
        if toggle is None:
            Logger.warning(f"Google AI Studio: toggle '{aria_label}' was not found.")
            return

        try:
            await self._click_locator(toggle, timeout=3000)
        except Exception as e:
            try:
                await self._dismiss_transient_overlays()
                await toggle.evaluate("el => el.click()")
            except Exception:
                Logger.warning(f"Google AI Studio: failed to toggle '{aria_label}': {e}")
                return

        deadline = time.time() + 4.0
        while time.time() < deadline:
            current = await self._read_toggle_state_by_aria_label(aria_label)
            if current is not None and current == state:
                return
            await asyncio.sleep(0.1)

        after = await self._read_toggle_state_by_aria_label(aria_label)
        if after != state:
            Logger.warning(
                f"Google AI Studio: toggle '{aria_label}' did not settle to the requested state "
                f"(wanted={state}, actual={after})."
            )

    async def set_search_state(self, state: bool) -> None:
        await self._set_toggle_state_by_aria_label(self.SEARCH_TOGGLE_LABEL, state)

    async def _set_url_context_state(self, state: bool) -> None:
        await self._set_toggle_state_by_aria_label(self.URL_CONTEXT_TOGGLE_LABEL, state)

    async def _read_thinking_budget_value(self) -> Optional[int]:
        """Read AI Studio's visible thinking-budget slider value."""
        if not self.page:
            return None

        try:
            raw_value = await self.page.evaluate(
                """(wrapperSelector) => {
                    const wrapper = document.querySelector(wrapperSelector);
                    if (!wrapper) return null;

                    let input = null;
                    try {
                        input = wrapper.querySelector(':scope > div > ms-slider > div > input:nth-child(2)');
                    } catch (e) {}
                    input = input || wrapper.querySelector('ms-slider input') || wrapper.querySelector('input');
                    if (!input) return null;

                    const value = (input.value ?? '').toString().trim();
                    if (value) return value;
                    const ariaValue = (input.getAttribute('aria-valuenow') || '').toString().trim();
                    return ariaValue || null;
                }""",
                self.THINKING_BUDGET_WRAPPER_SELECTOR,
            )
        except Exception:
            return None

        try:
            return int(float(str(raw_value or "").strip()))
        except Exception:
            return None

    async def _set_thinking_budget_value(self, value: int) -> bool:
        """Set AI Studio's manual thinking-budget slider input."""
        if not self.page:
            return False

        try:
            applied = await self.page.evaluate(
                """(payload) => {
                    const wrapper = document.querySelector(payload.wrapperSelector);
                    if (!wrapper) return false;

                    let input = null;
                    try {
                        input = wrapper.querySelector(':scope > div > ms-slider > div > input:nth-child(2)');
                    } catch (e) {}
                    input = input || wrapper.querySelector('ms-slider input') || wrapper.querySelector('input');
                    if (!input) return false;

                    const desired = Math.max(
                        payload.minValue,
                        Math.min(payload.maxValue, Number.parseInt(payload.value, 10))
                    ).toString();
                    try { input.focus({ preventScroll: true }); } catch (e) {
                        try { input.focus(); } catch (e2) {}
                    }

                    let proto = input;
                    let setter = null;
                    while (proto && !setter) {
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && typeof desc.set === 'function') {
                            setter = desc.set;
                            break;
                        }
                        proto = Object.getPrototypeOf(proto);
                    }

                    if (setter) {
                        setter.call(input, desired);
                    } else {
                        input.value = desired;
                    }

                    input.setAttribute('aria-valuenow', desired);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return (input.value ?? '').toString() === desired;
                }""",
                {
                    "wrapperSelector": self.THINKING_BUDGET_WRAPPER_SELECTOR,
                    "value": int(value),
                    "minValue": 1,
                    "maxValue": max(1, int(value)),
                },
            )
        except Exception:
            return False

        return bool(applied)

    async def _ui_settle_pause(self, delay_s: float = 0.22) -> None:
        delay = max(0.0, float(delay_s))
        if self._humanize_mouse_movements_enabled() and delay > 0.0:
            delay += random.uniform(0.02, 0.12)
        await asyncio.sleep(delay)

    async def _dismiss_transient_overlays(self) -> None:
        """Dismiss transient menus and backdrops that can steal subsequent clicks."""
        if not self.page:
            return

        started = time.perf_counter()
        Logger.extra_debug("Google AI Studio timing: dismiss transient overlays -> start")
        try:
            await self.page.evaluate(
                """() => {
                    const backdrop = document.querySelector('.cdk-overlay-backdrop-showing');
                    if (backdrop) {
                        try { backdrop.click(); } catch (e) {}
                    }
                }"""
            )
        except Exception:
            pass

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await self._ui_settle_pause(0.12)
        Logger.extra_debug(
            "Google AI Studio timing: dismiss transient overlays -> "
            f"done in {time.perf_counter() - started:.3f}s"
        )

    async def _refocus_composer_before_send(self) -> None:
        if not self.page:
            return

        started = time.perf_counter()
        Logger.extra_debug("Google AI Studio timing: refocus composer -> start")
        await self._dismiss_transient_overlays()
        find_started = time.perf_counter()
        editor = await self._find_first_visible(self.CHAT_READY_SELECTORS, timeout_ms=3000)
        Logger.extra_debug(
            "Google AI Studio timing: refocus composer -> find editor "
            f"{'found' if editor is not None else 'missing'} in "
            f"{time.perf_counter() - find_started:.3f}s"
        )
        if editor is None:
            Logger.extra_debug(
                "Google AI Studio timing: refocus composer -> abandoned "
                f"after {time.perf_counter() - started:.3f}s"
            )
            return

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        box_started = time.perf_counter()
        try:
            box = await editor.bounding_box()
            Logger.extra_debug(
                "Google AI Studio timing: refocus composer -> bounding_box "
                f"{'resolved' if box else 'empty'} in {time.perf_counter() - box_started:.3f}s"
            )
        except Exception as e:
            box = None
            Logger.extra_debug(
                "Google AI Studio timing: refocus composer -> bounding_box failed "
                f"after {time.perf_counter() - box_started:.3f}s: {e}"
            )

        if box:
            try:
                x = float(box["x"]) + min(max(float(box["width"]) * 0.25, 12.0), 48.0)
                y = float(box["y"]) + min(max(float(box["height"]) * 0.5, 8.0), 20.0)
                await self._click_mouse_point(x, y)
                Logger.extra_debug(
                    "Google AI Studio timing: refocus composer -> mouse click completed "
                    f"in {time.perf_counter() - started:.3f}s"
                )
                return
            except Exception as e:
                Logger.extra_debug(
                    "Google AI Studio timing: refocus composer -> mouse click failed "
                    f"after {time.perf_counter() - started:.3f}s: {e}"
                )
                pass

        click_started = time.perf_counter()
        try:
            await self._click_locator(editor, timeout=2000)
            Logger.extra_debug(
                "Google AI Studio timing: refocus composer -> locator click completed "
                f"in {time.perf_counter() - click_started:.3f}s "
                f"(total {time.perf_counter() - started:.3f}s)"
            )
        except Exception as e:
            Logger.extra_debug(
                "Google AI Studio timing: refocus composer -> locator click failed "
                f"after {time.perf_counter() - click_started:.3f}s "
                f"(total {time.perf_counter() - started:.3f}s): {e}"
            )
            pass

    async def _set_input_value(
        self,
        selector: str,
        value_text: str,
        *,
        timeout_ms: int = 8000,
        debug_label: str = "",
    ) -> bool:
        """Set an input value via DOM setters first, with a Playwright fallback."""
        field = await self._find_first_visible([selector], timeout_ms=timeout_ms)
        if field is None:
            return False

        return await self._set_text_control_value(
            field,
            value_text,
            nested_selector="input",
            debug_label=debug_label,
        )

    async def _set_text_control_value(
        self,
        field,
        value_text: str,
        *,
        nested_selector: str | None = None,
        debug_label: str = "",
    ) -> bool:
        """Force-set an input or textarea value while re-focusing between retries."""
        if field is None:
            return False

        value_text = str(value_text or "")
        nested_selector = str(nested_selector or "").strip()
        debug_label = str(debug_label or "").strip()
        debug_prefix = (
            f"Google AI Studio timing: set text control [{debug_label}]"
            if debug_label
            else "Google AI Studio timing: set text control"
        )
        started = time.perf_counter()
        Logger.extra_debug(
            f"{debug_prefix} -> start chars={len(value_text)} "
            f"nested_selector={nested_selector or '<none>'}"
        )

        for attempt in range(1, 5):
            attempt_started = time.perf_counter()
            Logger.extra_debug(f"{debug_prefix} -> attempt {attempt} start")
            try:
                await self._click_locator(field, timeout=1500, force=True)
                Logger.extra_debug(
                    f"{debug_prefix} -> attempt {attempt} click completed in "
                    f"{time.perf_counter() - attempt_started:.3f}s"
                )
            except Exception as e:
                Logger.extra_debug(
                    f"{debug_prefix} -> attempt {attempt} click failed after "
                    f"{time.perf_counter() - attempt_started:.3f}s: {e}"
                )
                pass

            evaluate_started = time.perf_counter()
            try:
                applied = await field.evaluate(
                    """(el, payload) => {
                        const value = (payload && payload.value !== undefined)
                            ? (payload.value ?? '').toString()
                            : '';
                        const nestedSelector = (payload && payload.nestedSelector)
                            ? payload.nestedSelector.toString()
                            : '';

                        const resolveTarget = () => {
                            if (el && typeof el.value !== 'undefined') return el;
                            if (nestedSelector && el && el.querySelector) {
                                return el.querySelector(nestedSelector);
                            }
                            return null;
                        };

                        const target = resolveTarget();
                        if (!target) return false;

                        try { target.focus({ preventScroll: true }); } catch (e) {
                            try { target.focus(); } catch (e2) {}
                        }

                        let proto = target;
                        let setter = null;
                        while (proto && !setter) {
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (desc && typeof desc.set === 'function') {
                                setter = desc.set;
                                break;
                            }
                            proto = Object.getPrototypeOf(proto);
                        }

                        if (setter) {
                            setter.call(target, value);
                        } else {
                            target.value = value;
                        }

                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        try {
                            target.setSelectionRange(value.length, value.length);
                        } catch (e) {}

                        return (target.value ?? '').toString() === value;
                    }""",
                    {
                        "value": value_text,
                        "nestedSelector": nested_selector,
                    },
                )
                Logger.extra_debug(
                    f"{debug_prefix} -> attempt {attempt} evaluate returned "
                    f"{bool(applied)} in {time.perf_counter() - evaluate_started:.3f}s"
                )
            except Exception as e:
                applied = False
                Logger.extra_debug(
                    f"{debug_prefix} -> attempt {attempt} evaluate failed after "
                    f"{time.perf_counter() - evaluate_started:.3f}s: {e}"
                )

            if applied:
                Logger.extra_debug(
                    f"{debug_prefix} -> applied by evaluate in "
                    f"{time.perf_counter() - started:.3f}s"
                )
                await self._humanized_action_pause(0.05, 0.16)
                return True

            fill_started = time.perf_counter()
            try:
                await field.fill(value_text)
                current_value = await field.evaluate(
                    """(el) => {
                        const target = (el && typeof el.value !== 'undefined')
                            ? el
                            : (el && el.querySelector ? el.querySelector('input, textarea') : null);
                        return target ? (target.value ?? '').toString() : '';
                    }"""
                )
                if str(current_value or "") == value_text:
                    Logger.extra_debug(
                        f"{debug_prefix} -> applied by fill in "
                        f"{time.perf_counter() - started:.3f}s "
                        f"(fill phase {time.perf_counter() - fill_started:.3f}s)"
                    )
                    await self._humanized_action_pause(0.05, 0.16)
                    return True
                Logger.extra_debug(
                    f"{debug_prefix} -> attempt {attempt} fill value mismatch "
                    f"after {time.perf_counter() - fill_started:.3f}s"
                )
            except Exception as e:
                Logger.extra_debug(
                    f"{debug_prefix} -> attempt {attempt} fill failed after "
                    f"{time.perf_counter() - fill_started:.3f}s: {e}"
                )
                pass

            await asyncio.sleep(0.08)

        Logger.extra_debug(
            f"{debug_prefix} -> failed after {time.perf_counter() - started:.3f}s"
        )
        return False

    async def _set_textarea_value(self, selector: str, value_text: str, *, timeout_ms: int = 8000) -> bool:
        """Set a textarea value with the same resilient path used by the composer."""
        field = await self._find_first_visible([selector], timeout_ms=timeout_ms)
        if field is None:
            return False

        return await self._set_text_control_value(field, value_text, nested_selector="textarea")

    @staticmethod
    def _format_decimal_setting_value(value: float) -> str:
        """Format decimal request controls the same way the UI setter does."""
        return f"{float(value):.4f}".rstrip("0").rstrip(".")

    @classmethod
    def _decimal_setting_matches(cls, raw_value: Any, desired: float) -> bool:
        """Compare a raw UI decimal value against the normalized desired value."""
        text = str(raw_value or "").strip()
        if not text:
            return False
        try:
            current = cls._format_decimal_setting_value(float(text))
        except Exception:
            return False
        return current == cls._format_decimal_setting_value(desired)

    @staticmethod
    def _integer_setting_matches(raw_value: Any, desired: int) -> bool:
        """Compare a raw UI integer value against the desired integer."""
        text = str(raw_value or "").strip()
        if not text:
            return False
        try:
            current = int(text)
        except Exception:
            try:
                parsed = float(text)
            except Exception:
                return False
            if not parsed.is_integer():
                return False
            current = int(parsed)
        return current == int(desired)

    async def _read_input_value(self, selector: str) -> Optional[str]:
        """Read an input's current DOM value without requiring it to be visible."""
        if not self.page:
            return None

        try:
            value = await self.page.evaluate(
                """(selector) => {
                    const input = document.querySelector(selector);
                    if (!input) return null;
                    return (input.value ?? '').toString();
                }""",
                selector,
            )
        except Exception:
            return None

        text = str(value or "").strip()
        return text or None

    async def _read_top_p_value(self) -> Optional[str]:
        """Best-effort read of the current top-p input value from the run settings panel."""
        if not self.page:
            return None

        try:
            value = await self.page.evaluate(
                """(tooltip) => {
                    const root = Array.from(document.querySelectorAll('div[mattooltip]'))
                        .find((el) => (el.getAttribute('mattooltip') || '').toString() === tooltip);
                    if (!root) return null;

                    const secondChild = root.children && root.children.length >= 2
                        ? root.children[1]
                        : null;
                    let input = null;
                    try {
                        input = secondChild
                            ? secondChild.querySelector(':scope > ms-slider > div > input:nth-child(2)')
                            : null;
                    } catch (e) {}
                    input = input
                        || (secondChild ? secondChild.querySelector('ms-slider input') : null)
                        || root.querySelector('ms-slider input')
                        || root.querySelector('input');
                    if (!input) return null;

                    const value = (input.value ?? '').toString().trim();
                    if (value) return value;
                    const ariaValue = (input.getAttribute('aria-valuenow') || '').toString().trim();
                    return ariaValue || null;
                }""",
                self.TOP_P_TOOLTIP,
            )
        except Exception:
            return None

        text = str(value or "").strip()
        return text or None

    async def _set_temperature_value(self, value: float) -> None:
        formatted = self._format_decimal_setting_value(value)
        current = await self._read_input_value(
            "div[data-test-id='temperatureSliderContainer'] input.slider-number-input.small, "
            "div[data-test-id='temperatureSliderContainer'] input"
        )
        if self._decimal_setting_matches(current, value):
            Logger.debug(
                f"Google AI Studio: temperature already matches {formatted}; skipping update."
            )
            return
        ok = await self._set_input_value(
            "div[data-test-id='temperatureSliderContainer'] input.slider-number-input.small, "
            "div[data-test-id='temperatureSliderContainer'] input",
            formatted,
        )
        if not ok:
            Logger.warning("Google AI Studio: temperature input was not found.")

    async def _advanced_settings_visible(self) -> bool:
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
                        return style.visibility !== 'hidden' && style.display !== 'none';
                    };

                    const maxTokens =
                        document.querySelector("input[aria-label='Maximum output tokens']") ||
                        document.querySelector("input[name='maxOutputTokens']");
                    if (isVisible(maxTokens)) return true;

                    const safetyTrigger = document.querySelector("div.safety-settings button");
                    if (isVisible(safetyTrigger)) return true;

                    const topP = Array.from(document.querySelectorAll('div[mattooltip]'))
                        .find((el) => (el.getAttribute('mattooltip') || '').toString() === 'Probability threshold for top-p sampling');
                    if (isVisible(topP)) return true;

                    return false;
                }"""
            )
        except Exception:
            visible = False

        return bool(visible)

    async def _ensure_advanced_settings_expanded(self) -> bool:
        """Expand the advanced settings panel if its controls are still hidden."""
        if not self.page:
            return False

        if await self._advanced_settings_visible():
            return True

        button = await self._find_first_visible(
            [
                f"button[aria-label='{self.ADVANCED_SETTINGS_LABEL}']",
                f"[aria-label='{self.ADVANCED_SETTINGS_LABEL}']",
            ],
            timeout_ms=6000,
        )
        if button is None:
            Logger.warning("Google AI Studio: advanced settings toggle was not found.")
            return False

        try:
            aria_expanded = str(await button.get_attribute("aria-expanded") or "").strip().lower()
        except Exception:
            aria_expanded = ""
        if aria_expanded == "true":
            return True

        try:
            await self._click_locator(button, timeout=3000)
        except Exception:
            return False

        await asyncio.sleep(0.25)
        return await self._advanced_settings_visible()

    async def _set_top_p_value(self, value: float) -> None:
        if not self.page:
            return

        formatted = self._format_decimal_setting_value(value)
        current = await self._read_top_p_value()
        if self._decimal_setting_matches(current, value):
            Logger.debug(f"Google AI Studio: top-p already matches {formatted}; skipping update.")
            return

        expanded = await self._ensure_advanced_settings_expanded()
        if not expanded:
            Logger.warning("Google AI Studio: advanced settings could not be expanded for top-p.")
            return

        current = await self._read_top_p_value()
        if self._decimal_setting_matches(current, value):
            Logger.debug(
                f"Google AI Studio: top-p already matches {formatted} after expanding settings; skipping update."
            )
            return

        try:
            ok = await self.page.evaluate(
                """(payload) => {
                    const apply = (input) => {
                        if (!input) return false;
                        const targetValue = (payload && payload.value !== undefined)
                            ? (payload.value ?? '').toString()
                            : '';
                        const proto = Object.getPrototypeOf(input);
                        const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
                        if (desc && typeof desc.set === 'function') {
                            desc.set.call(input, targetValue);
                        } else {
                            input.value = targetValue;
                        }
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    };

                    const tooltip = (payload && payload.tooltip) ? payload.tooltip.toString() : '';
                    const root = Array.from(document.querySelectorAll('div[mattooltip]'))
                        .find((el) => (el.getAttribute('mattooltip') || '').toString() === tooltip);
                    if (!root) return false;

                    const secondChild = root.children && root.children.length >= 2
                        ? root.children[1]
                        : null;
                    let input = null;
                    try {
                        input = secondChild
                            ? secondChild.querySelector(':scope > ms-slider > div > input:nth-child(2)')
                            : null;
                    } catch (e) {}
                    input = input
                        || (secondChild ? secondChild.querySelector('ms-slider input') : null)
                        || root.querySelector('ms-slider input')
                        || root.querySelector('input');
                    return apply(input);
                }""",
                {
                    "tooltip": self.TOP_P_TOOLTIP,
                    "value": formatted,
                },
            )
        except Exception:
            ok = False

        if not ok:
            Logger.warning("Google AI Studio: top-p input was not found.")

    async def _set_max_output_tokens(self, value: int) -> None:
        max_tokens_selector = (
            f"input[aria-label='{self.MAX_OUTPUT_TOKENS_INPUT_LABEL}'], "
            "input[name='maxOutputTokens']"
        )
        current = await self._read_input_value(max_tokens_selector)
        if self._integer_setting_matches(current, value):
            Logger.debug(
                f"Google AI Studio: maxOutputTokens already matches {int(value)}; skipping update."
            )
            return

        expanded = await self._ensure_advanced_settings_expanded()
        if not expanded:
            Logger.warning("Google AI Studio: advanced settings could not be expanded for maxOutputTokens.")
            return
        ok = await self._set_input_value(max_tokens_selector, str(int(value)))
        if not ok:
            Logger.warning("Google AI Studio: maxOutputTokens input was not found.")

    async def _close_safety_settings_panel(self, panel=None) -> bool:
        """Close the run safety settings panel, preferring the explicit close button."""
        if not self.page:
            return False

        current_panel = panel
        if current_panel is None:
            current_panel = await self._find_first_visible(
                [self.RUN_SAFETY_SETTINGS_PANEL_SELECTOR],
                timeout_ms=0,
            )
        if current_panel is None:
            return True

        close_button = await self._find_first_visible_within(
            current_panel,
            self.RUN_SAFETY_SETTINGS_CLOSE_SELECTORS,
            timeout_ms=1000,
        )
        if close_button is not None:
            for _ in range(3):
                await self._ui_settle_pause(0.16)
                try:
                    await self._click_locator(close_button, timeout=2000, force=True)
                except Exception:
                    try:
                        await close_button.evaluate("el => el.click()")
                    except Exception:
                        pass

                await self._ui_settle_pause(0.16)
                if (
                    await self._find_first_visible(
                        [self.RUN_SAFETY_SETTINGS_PANEL_SELECTOR],
                        timeout_ms=0,
                    )
                    is None
                ):
                    return True

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        await self._ui_settle_pause(0.12)
        return (
            await self._find_first_visible(
                [self.RUN_SAFETY_SETTINGS_PANEL_SELECTOR],
                timeout_ms=0,
            )
            is None
        )

    async def _set_safety_filters_low(self) -> bool:
        """Open the safety settings panel and move all sliders to their lowest values."""
        if not self.page:
            return False

        expanded = await self._ensure_advanced_settings_expanded()
        if not expanded:
            Logger.warning("Google AI Studio: advanced settings could not be expanded for safety settings.")
            return False

        trigger = await self._find_first_visible(
            [
                "div.safety-settings > :nth-child(2) button",
                "div.safety-settings button",
            ],
            timeout_ms=6000,
        )
        if trigger is None:
            Logger.warning("Google AI Studio: safety settings trigger was not found.")
            return False

        try:
            await self._click_locator(trigger, timeout=3000)
        except Exception as e:
            Logger.warning(f"Google AI Studio: failed to open safety settings: {e}")
            return False

        try:
            await self.page.wait_for_selector(
                self.RUN_SAFETY_SETTINGS_PANEL_SELECTOR,
                timeout=5000,
                state="visible",
            )
        except Exception:
            Logger.warning("Google AI Studio: safety settings panel did not appear.")
            return False

        panel = await self._find_first_visible([self.RUN_SAFETY_SETTINGS_PANEL_SELECTOR], timeout_ms=0)
        if panel is None:
            Logger.warning("Google AI Studio: safety settings panel did not appear.")
            return False

        try:
            await self.page.evaluate(
                """(panelSelector) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        return style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const sliders = Array.from(
                        document.querySelectorAll(`${panelSelector} input[type="range"]`)
                    ).filter(isVisible);
                    for (const slider of sliders) {
                        const min = slider.getAttribute('min');
                        const target = (min !== null && min !== '') ? min : '-1';
                        const proto = Object.getPrototypeOf(slider);
                        const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
                        if (desc && typeof desc.set === 'function') {
                            desc.set.call(slider, target);
                        } else {
                            slider.value = target;
                        }
                        const rect = slider.getBoundingClientRect();
                        const clientX = rect.left + 1;
                        const clientY = rect.top + (rect.height / 2);
                        try {
                            slider.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX, clientY }));
                        } catch (e) {}
                        try {
                            slider.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX, clientY }));
                        } catch (e) {}
                        try {
                            slider.dispatchEvent(new TouchEvent('touchstart', {
                                bubbles: true,
                                touches: [new Touch({ identifier: Date.now(), target: slider, clientX, clientY })],
                            }));
                        } catch (e) {}
                        slider.dispatchEvent(new Event('input', { bubbles: true }));
                        slider.dispatchEvent(new Event('change', { bubbles: true }));
                        try {
                            slider.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX, clientY }));
                        } catch (e) {}
                        try {
                            slider.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, clientX, clientY }));
                        } catch (e) {}
                    }
                }""",
                self.RUN_SAFETY_SETTINGS_PANEL_SELECTOR,
            )
        except Exception as e:
            Logger.warning(f"Google AI Studio: failed to lower safety filters: {e}")
            return False

        if not await self._close_safety_settings_panel(panel):
            Logger.warning("Google AI Studio: safety settings panel did not close cleanly.")
            return False
        return True

    async def _ensure_safety_filters_initialized(self) -> None:
        """Apply the low-safety preset once per session after the chat UI is ready."""
        if self._safety_filters_initialized:
            return
        if not self.page:
            return
        if not await self._wait_for_chat_ready(timeout_ms=15000):
            return
        ok = await self._set_safety_filters_low()
        if ok:
            self._safety_filters_initialized = True

    async def _read_current_thinking_level(self) -> str:
        """Read the currently displayed thinking level text from the trigger, if visible."""
        if not self.page:
            return ""
        selectors = [
            "mat-select[aria-label*='hinking'] .mat-mdc-select-value-text",
            "mat-select[aria-label*='hinking'] .mat-mdc-select-min-line",
            "mat-select[aria-label*='hinking'] span",
            ".mat-mdc-form-field:has-text('Thinking') .mat-mdc-select-value-text",
            ".mat-mdc-form-field:has-text('Thinking') .mat-mdc-select-min-line",
            "mat-form-field:has-text('Thinking') .mat-mdc-select-value-text",
            "mat-form-field:has-text('Thinking') [role='combobox']",
        ]
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    txt = str(await loc.first.inner_text() or "").strip()
                    norm = self._normalize_thinking_level(txt)
                    if norm:
                        return norm
            except Exception:
                continue
        return ""

    async def _open_thinking_level_dropdown(self) -> bool:
        """Find and open the thinking-level dropdown despite AI Studio layout variance."""
        if not self.page:
            return False

        await self._dismiss_transient_overlays()

        # Try direct selectors first
        direct_selectors = [
            "mat-select[aria-label='Thinking Level']",
            "mat-select[aria-label='Thinking Effort']",
            "mat-select[aria-label*='hinking']",
            "[data-test-id*='thinking'] mat-select",
            "[data-test-id*='thinking'] [role='combobox']",
        ]
        for sel in direct_selectors:
            try:
                loc = self.page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await self._click_locator(loc.first, timeout=3000)
                    await self._ui_settle_pause(0.18)
                    try:
                        await self.page.wait_for_selector(self.THINKING_LISTBOX_SELECTOR, timeout=5000, state="visible")
                        return True
                    except Exception:
                        pass
            except Exception:
                continue

        fields = self.page.locator(".mat-mdc-form-field, mat-form-field")
        count = 0
        try:
            count = await fields.count()
        except Exception:
            count = 0

        visible_items = []
        for idx in range(min(count, 12)):
            item = fields.nth(idx)
            try:
                if await item.is_visible():
                    visible_items.append(item)
            except Exception:
                continue

        if not visible_items:
            return False

        target = None
        for item in visible_items:
            try:
                for label in self.THINKING_SELECT_LABELS:
                    thinking_select = item.locator(f"mat-select[aria-label='{label}'], mat-select[aria-label*='{label}']")
                    if await thinking_select.count() > 0:
                        target = thinking_select.first
                        break
                if target is not None:
                    break
            except Exception:
                pass

        if target is None:
            for item in visible_items:
                try:
                    label_text = str(await item.inner_text() or "")
                except Exception:
                    label_text = ""
                normalized_label_text = label_text.strip().lower()
                if any(label.lower() in normalized_label_text for label in self.THINKING_SELECT_LABELS):
                    target = item
                    break

        if target is None:
            for item in visible_items:
                try:
                    ancestor = item.locator("xpath=../..")
                    if await ancestor.count() == 0:
                        continue
                    describedby = str(await ancestor.first.get_attribute("aria-describedby") or "").strip()
                    if describedby == self.THINKING_FORM_FIELD_GRANDPARENT_ARIA_DESCRIBEDBY:
                        target = item
                        break
                except Exception:
                    continue

        if target is None:
            return False

        clickable = target.locator(".mat-mdc-select-trigger, [role='combobox'], .mat-mdc-text-field-wrapper")
        try:
            if await clickable.count() > 0 and await clickable.first.is_visible():
                target = clickable.first
        except Exception:
            pass

        try:
            await self._click_locator(target, timeout=3000)
        except Exception:
            return False
        await self._ui_settle_pause(0.18)

        try:
            await self.page.wait_for_selector(self.THINKING_LISTBOX_SELECTOR, timeout=5000, state="visible")
            return True
        except Exception:
            return False

    async def _select_thinking_level(self, level: str) -> bool:
        """Select a normalized thinking level from the thinking dropdown."""
        wanted = self._normalize_thinking_level(level)
        if not wanted:
            return False
        if not self.page:
            return False

        # If already set to the desired level, skip opening the dropdown
        current = await self._read_current_thinking_level()
        if current and current == wanted:
            return True

        if not await self._open_thinking_level_dropdown():
            return False

        options = self.page.locator(self.THINKING_LISTBOX_SELECTOR)
        count = 0
        try:
            count = await options.count()
        except Exception:
            count = 0

        available: list[tuple[str, Any]] = []
        for idx in range(min(count, 12)):
            candidate = options.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
                text = str(await candidate.inner_text() or "").strip()
                norm = self._normalize_thinking_level(text)
                if norm:
                    available.append((norm, candidate))
            except Exception:
                continue

        if not available:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        # Try exact match first
        target_cand = next((cand for norm, cand in available if norm == wanted), None)

        # Fallback to closest available option if wanted is not present
        if target_cand is None:
            supported_available = tuple(norm for norm, _ in available)
            closest_norm = self._closest_supported_thinking_level(wanted, supported_available)
            target_cand = next((cand for norm, cand in available if norm == closest_norm), None)
            if target_cand:
                Logger.info(
                    f"Google AI Studio: thinking level '{wanted}' not in dropdown {supported_available}; selecting closest '{closest_norm}'."
                )

        if target_cand is not None:
            try:
                await self._click_locator(target_cand, timeout=3000)
                await self._ui_settle_pause(0.18)
                return True
            except Exception:
                pass

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    async def _apply_thinking_budget_level(self, model_base: str, level: str) -> bool:
        """Apply 2.5 Flash/Pro thinking via mode and manual budget controls."""
        model_key = str(model_base or "").strip().lower()
        if model_key not in self.THINKING_BUDGET_BY_MODEL:
            return False

        budget = self._thinking_budget_for_level(model_key, level)
        if model_key in self.THINKING_MODE_TOGGLE_MODELS and budget is None:
            await self._set_toggle_state_by_aria_label(self.THINKING_MODE_TOGGLE_LABEL, False)
            await self._ui_settle_pause(0.2)
            return True

        if budget is None:
            return False

        if model_key in self.THINKING_MODE_TOGGLE_MODELS:
            await self._set_toggle_state_by_aria_label(self.THINKING_MODE_TOGGLE_LABEL, True)
            await self._ui_settle_pause(0.25)

        await self._set_toggle_state_by_aria_label(self.THINKING_BUDGET_TOGGLE_LABEL, True)
        await self._ui_settle_pause(0.25)

        current = await self._read_thinking_budget_value()
        if current is not None and self._integer_setting_matches(current, budget):
            return True

        min_value, max_value = self.THINKING_BUDGET_RANGE_BY_MODEL.get(model_key, (1, budget))
        budget = self._clamp_int(budget, budget, min_value, max_value)
        ok = await self._set_thinking_budget_value(budget)
        if ok:
            await self._ui_settle_pause(0.12)
        return ok

    async def set_deepthink_state(self, state: bool) -> None:
        current_model = await self._read_current_model_id()
        model_base = str(current_model or "").strip().lower()
        if model_base not in self.THINKING_LEVELS_BY_MODEL:
            configured_model = self._model_config_for_label(self._get_configured_model_label())
            model_base = str(configured_model.get("base_id") or "").strip().lower()
        if model_base not in self.THINKING_LEVELS_BY_MODEL:
            return

        target_level = (
            self._configured_thinking_level() if state else self._default_low_thinking_level(model_base)
        )
        if not target_level:
            return

        if model_base in self.THINKING_BUDGET_BY_MODEL:
            ok = await self._apply_thinking_budget_level(model_base, target_level)
        else:
            ok = await self._select_thinking_level(target_level)
        if not ok:
            Logger.warning(
                f"Google AI Studio: failed to set thinking level to '{target_level}'."
            )

    async def _apply_request_controls(self, settings: Dict[str, Any]) -> None:
        """Apply the resolved model and control settings to the current chat UI."""
        await self._ensure_model_selected(str(settings.get("model_label") or self._get_configured_model_label()))
        await self._ui_settle_pause(0.28)

        current_model = await self._read_current_model_id()
        model_base = str(settings.get("model_base_id") or current_model or "").strip().lower()
        thinking_level = self._normalize_thinking_level(str(settings.get("thinking_level") or ""))
        if thinking_level and model_base in self.THINKING_BUDGET_BY_MODEL:
            ok = await self._apply_thinking_budget_level(model_base, thinking_level)
            if not ok:
                Logger.warning(
                    f"Google AI Studio: failed to apply thinking budget for '{thinking_level}'."
                )
            await self._ui_settle_pause(0.24)
        elif thinking_level and model_base in self.THINKING_LEVELS_BY_MODEL:
            ok = await self._select_thinking_level(thinking_level)
            if not ok:
                Logger.warning(
                    f"Google AI Studio: failed to apply thinking level '{thinking_level}'."
                )
            await self._ui_settle_pause(0.24)

        if not bool(settings.get("force_search", False)):
            await self.set_search_state(bool(settings.get("search_enabled")))
            await self._ui_settle_pause(0.18)
        if bool(settings.get("supports_url_context", True)):
            await self._set_url_context_state(bool(settings.get("url_context_enabled")))
            await self._ui_settle_pause(0.18)
        if bool(settings.get("supports_temperature", True)):
            await self._set_temperature_value(float(settings.get("temperature", 1.0)))
            await self._ui_settle_pause(0.14)
        else:
            Logger.debug(
                "Google AI Studio: selected model does not expose temperature; skipping update."
            )
        if bool(settings.get("supports_top_p", True)):
            await self._set_top_p_value(float(settings.get("top_p", 0.95)))
            await self._ui_settle_pause(0.18)
        else:
            Logger.debug("Google AI Studio: selected model does not expose top-p; skipping update.")
        await self._set_max_output_tokens(int(settings.get("max_output_tokens", 65536)))
        await self._ui_settle_pause(0.22)

    async def set_sidebar_status(self, open: bool) -> None:
        _ = open
        return

    async def click_new_chat(self, source: str = "auto") -> None:
        _ = source
        if not self.page:
            return
        await self._navigate_to_start_url(self.START_URL)
        await self._wait_for_chat_ready(timeout_ms=60000)
        await self._ensure_temporary_chat_url(timeout_ms=60000)

    def _preflight_next_chat_enabled(self) -> bool:
        """Return whether post-response blank-chat preparation is enabled."""
        try:
            return bool(self.config_manager.get_setting("aistudio_behavior", "preflight_next_chat"))
        except Exception:
            return False

    def _clear_preflight_chat_state(self) -> None:
        """Forget any prepared blank chat snapshot."""
        self._preflight_ready = False
        self._preflight_state = None
        self._preflight_system_prompt_text = ""

    async def cleanup_background_tasks(self) -> None:
        await self._cancel_task(
            self._preflight_task,
            label="stopping Google AI Studio preflight task",
        )
        self._preflight_task = None
        self._clear_preflight_chat_state()

    async def _await_preflight_task(self) -> None:
        """Let an in-progress preflight finish before the next UI action."""
        task = self._preflight_task
        if task is None:
            return

        try:
            if not task.done():
                Logger.debug("Google AI Studio Preflight: waiting for prepared chat setup to finish...")
            await task
        except asyncio.CancelledError:
            raise
        except Exception as e:
            Logger.debug(f"Google AI Studio Preflight: background task ended with an error: {e}")
        finally:
            if self._preflight_task is task:
                self._preflight_task = None

    async def _prepare_preflight_next_chat(
        self,
        settings: Dict[str, Any],
        system_prompt_text: str,
    ) -> None:
        """Prepare a blank AI Studio chat using the previous request's settings."""
        self._clear_preflight_chat_state()
        if not self.page:
            return

        try:
            Logger.info("Google AI Studio Preflight: preparing the next chat session...")
            await self._wait_for_generation_idle(timeout_ms=10000)
            await self.click_new_chat(source="preflight")
            await self._ui_settle_pause(0.2)
            await self._apply_request_controls(settings)
            await self._ui_settle_pause(0.2)
            if bool(settings.get("use_system_prompt_field")):
                await self._sync_system_prompt_field(system_prompt_text)
                await self._ui_settle_pause(0.18)

            self._preflight_state = self._build_clean_regeneration_state(
                settings,
                system_prompt_text=system_prompt_text,
            )
            self._preflight_system_prompt_text = str(system_prompt_text or "")
            self._preflight_ready = True
            Logger.info("Google AI Studio Preflight: next chat session is ready.")
        except asyncio.CancelledError:
            self._clear_preflight_chat_state()
            raise
        except Exception as e:
            self._clear_preflight_chat_state()
            Logger.warning(f"Google AI Studio Preflight: failed to prepare next chat session: {e}")

    def _schedule_preflight_next_chat(
        self,
        settings: Dict[str, Any],
        system_prompt_text: str,
    ) -> None:
        """Start best-effort blank-chat preparation after a successful request."""
        if not self._preflight_next_chat_enabled():
            self._clear_preflight_chat_state()
            return
        try:
            if bool(self.config_manager.get_setting("aistudio_behavior", "clean_regeneration")):
                self._clear_preflight_chat_state()
                return
        except Exception:
            pass

        task = self._preflight_task
        if task is not None and not task.done():
            return
        if task is not None and task.done():
            self._preflight_task = None

        settings_snapshot = dict(settings)
        system_prompt_snapshot = str(system_prompt_text or "")
        self._clear_preflight_chat_state()
        self._preflight_task = asyncio.create_task(
            self._prepare_preflight_next_chat(settings_snapshot, system_prompt_snapshot)
        )

    async def _preflight_live_model_matches(self, settings: Dict[str, Any]) -> bool:
        """Return whether the prepared chat still has the expected live model."""
        desired_label = str(settings.get("model_label") or "").strip()
        if not desired_label:
            return True

        try:
            desired = self._model_config_for_label(desired_label)
            current = await self._read_current_model_id()
            return self._current_model_matches(current, desired_label, desired)
        except Exception as e:
            Logger.debug(f"Google AI Studio Preflight: failed to verify live model: {e}")
            return False

    async def _consume_preflighted_chat(
        self,
        settings: Dict[str, Any],
        system_prompt_text: str,
    ) -> bool:
        """Use the prepared blank chat, adjusting it if this request changed settings."""
        await self._await_preflight_task()
        if not self._preflight_ready:
            return False

        previous_state = self._preflight_state
        previous_system_prompt_text = self._preflight_system_prompt_text
        self._clear_preflight_chat_state()

        requested_state = self._build_clean_regeneration_state(
            settings,
            system_prompt_text=system_prompt_text,
        )
        if previous_state == requested_state:
            if await self._preflight_live_model_matches(settings):
                Logger.info("Google AI Studio Preflight: using the prepared chat session.")
                return True

            Logger.info(
                "Google AI Studio Preflight: prepared chat settings matched, "
                "but the live model changed. Reapplying controls..."
            )
        else:
            Logger.info("Google AI Studio Preflight: adjusting the prepared chat for this request...")

        await self._apply_request_controls(settings)
        await self._ui_settle_pause(0.2)

        should_sync_system_prompt = (
            bool(settings.get("use_system_prompt_field"))
            or bool((previous_state or {}).get("use_system_prompt_field"))
            or bool(str(previous_system_prompt_text or "").strip())
        )
        if should_sync_system_prompt:
            next_system_prompt_text = (
                str(system_prompt_text or "")
                if bool(settings.get("use_system_prompt_field"))
                else ""
            )
            await self._sync_system_prompt_field(next_system_prompt_text)
            await self._ui_settle_pause(0.18)
        return True

    async def upload_file(self, file_spec: Any) -> None:
        """Upload media through AI Studio's picker, including acknowledgement handling."""
        if not self.page:
            return
        return await self._upload_file_with_ack(file_spec)

    async def _upload_file_with_ack(self, file_spec: Any) -> None:
        """Upload a file and handle the copyright acknowledgement dialog if it appears."""
        if not self.page:
            return

        temp_dir: str | None = None
        temp_path: str | None = None

        async def _wait_for_uploaded_media(timeout_ms: int) -> bool:
            deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
            while True:
                media = await self._find_first_visible(
                    [self.PROMPT_MEDIA_CONTAINER_SELECTOR],
                    timeout_ms=0,
                )
                if media is not None:
                    return True
                if time.time() >= deadline:
                    return False
                await asyncio.sleep(0.1)

        async def _wait_for_upload_ack_dialog(timeout_ms: int) -> bool:
            dialog = await self._find_first_visible(
                [self.UPLOAD_ACK_DIALOG_TITLE_SELECTOR],
                timeout_ms=timeout_ms,
            )
            return dialog is not None

        async def _find_upload_file_input(timeout_ms: int = 0):
            deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
            while True:
                for selector in self.UPLOAD_FILE_INPUT_SELECTORS:
                    locator = self.page.locator(selector)
                    try:
                        count = await locator.count()
                    except Exception:
                        count = 0
                    if count > 0:
                        return locator.nth(0)

                if timeout_ms <= 0 or time.time() >= deadline:
                    return None
                await asyncio.sleep(0.1)

        async def _trigger_picker_upload(chooser_files_value: Any) -> bool:
            add_media_button = await self._find_first_visible(
                self.ADD_MEDIA_BUTTON_SELECTORS,
                timeout_ms=8000,
            )
            if add_media_button is None:
                Logger.warning("Google AI Studio: add-media button was not found.")
                return False

            try:
                await self._click_locator(add_media_button, timeout=3000)
            except Exception as e:
                try:
                    await self._click_locator(add_media_button, timeout=3000, force=True)
                except Exception:
                    Logger.warning(f"Google AI Studio: failed to click add-media button: {e}")
                    return False

            try:
                await self.page.wait_for_selector(
                    ".mat-mdc-menu-content, .mat-mdc-menu-panel, .cdk-overlay-pane",
                    timeout=5000,
                    state="visible",
                )
            except Exception:
                pass

            file_input = await _find_upload_file_input(timeout_ms=800)
            if file_input is None:
                try:
                    file_input = await _find_upload_file_input(timeout_ms=0)
                except Exception:
                    file_input = None

            if file_input is not None:
                try:
                    await file_input.set_input_files(chooser_files_value)
                    await self._ui_settle_pause(0.4)
                    return True
                except Exception as e:
                    Logger.debug(
                        f"Google AI Studio: direct upload input failed, trying picker menu: {e}"
                    )

            try:
                async with self.page.expect_file_chooser(timeout=6000) as fc_info:
                    upload_item = await self._find_first_visible(
                        self.UPLOAD_FILE_MENU_ITEM_SELECTORS,
                        timeout_ms=2500,
                    )
                    clicked = False
                    if upload_item is not None:
                        try:
                            await self._click_locator(upload_item, timeout=2500)
                            clicked = True
                        except Exception:
                            try:
                                await self._click_locator(upload_item, timeout=2500, force=True)
                                clicked = True
                            except Exception:
                                clicked = False

                    if not clicked:
                        clicked = await self.page.evaluate(
                            """() => {
                                const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();
                                const isVisible = (el) => {
                                    if (!el) return false;
                                    const rect = el.getBoundingClientRect();
                                    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                                    const style = window.getComputedStyle(el);
                                    if (!style) return false;
                                    return style.visibility !== 'hidden' && style.display !== 'none';
                                };

                                const candidates = Array.from(document.querySelectorAll(
                                    'button.upload-file-menu-item, .upload-file-menu-item, button[role="menuitem"], [role="menuitem"], button'
                                )).filter(isVisible);
                                const target = candidates.find((el) => {
                                    const text = normalize(el.innerText || el.textContent || '');
                                    const aria = normalize(el.getAttribute('aria-label'));
                                    const classes = normalize(el.className);
                                    return classes.includes('upload-file-menu-item')
                                        || text.includes('upload files')
                                        || aria.includes('upload files');
                                });
                                if (!target) return false;
                                try {
                                    target.click();
                                    return true;
                                } catch (e) {
                                    return false;
                                }
                            }"""
                        )
                    if not clicked:
                        raise RuntimeError("AI Studio file picker menu item was not clickable.")

                chooser = await fc_info.value
                await chooser.set_files(chooser_files_value)
                await self._ui_settle_pause(0.4)
                return True
            except Exception as e:
                file_input = await _find_upload_file_input(timeout_ms=1000)
                if file_input is not None:
                    try:
                        await file_input.set_input_files(chooser_files_value)
                        await self._ui_settle_pause(0.4)
                        return True
                    except Exception:
                        pass
                Logger.warning(f"Google AI Studio: upload file picker did not open: {e}")
                return False

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

                temp_dir = tempfile.mkdtemp(prefix="irp-aistudio-upload-")
                temp_path = os.path.join(temp_dir, safe_name)
                with open(temp_path, "wb") as f:
                    f.write(buffer)
                return temp_path
            except Exception:
                return None

        try:
            chooser_files = file_spec
            if isinstance(file_spec, dict):
                temp_candidate = _materialize_payload(file_spec)
                if temp_candidate:
                    chooser_files = temp_candidate

            upload_timeout_ms = int(
                self._clamp_int(
                    self.config_manager.get_setting("aistudio_behavior", "file_upload_timeout"),
                    20,
                    1,
                    120,
                )
                * 1000
            )

            if not await _trigger_picker_upload(chooser_files):
                raise RuntimeError("Google AI Studio file picker did not open.")

            ack_waiting_notified = False
            ack_auto_attempted = False
            upload_deadline = time.time() + max(0.0, float(upload_timeout_ms) / 1000.0)

            while True:
                media = await self._find_first_visible(
                    [self.PROMPT_MEDIA_CONTAINER_SELECTOR],
                    timeout_ms=0,
                )
                if media is not None:
                    return

                dialog = await self._find_first_visible(
                    [self.UPLOAD_ACK_DIALOG_CONTAINER_SELECTOR, self.UPLOAD_ACK_DIALOG_TITLE_SELECTOR],
                    timeout_ms=0,
                )
                if dialog is not None:
                    if not ack_auto_attempted:
                        ack_auto_attempted = True
                        Logger.info(
                            "Google AI Studio: upload acknowledgement dialog detected. "
                            "Attempting to acknowledge it automatically..."
                        )
                        accept_button = await self._find_first_visible(
                            [self.UPLOAD_ACK_ACCEPT_BUTTON_SELECTOR],
                            timeout_ms=1200,
                        )
                        if accept_button is not None:
                            try:
                                await self._click_locator(accept_button, timeout=3000)
                            except Exception:
                                try:
                                    await accept_button.evaluate("el => el.click()")
                                except Exception:
                                    pass

                    if not ack_waiting_notified:
                        ack_waiting_notified = True
                        Logger.warning(
                            "Google AI Studio: waiting for the upload acknowledgement dialog to be accepted..."
                        )
                        self.notify_user(
                            "Google AI Studio Upload",
                            "Please review and accept the media upload acknowledgement in the browser window. "
                            "IntenseRP will continue automatically once the dialog disappears.",
                            level="warning",
                        )

                if time.time() >= upload_deadline:
                    break
                await asyncio.sleep(0.1)

            if ack_waiting_notified:
                await self._ui_settle_pause(0.35)
                if await _wait_for_uploaded_media(timeout_ms=2000):
                    return

                Logger.warning(
                    "Google AI Studio: upload acknowledgement flow finished, but the file still did not appear. "
                    "Retrying the picker once..."
                )
                if not await _trigger_picker_upload(chooser_files):
                    raise RuntimeError("Google AI Studio retry upload could not reopen the file picker.")

            if not await _wait_for_uploaded_media(timeout_ms=upload_timeout_ms):
                raise RuntimeError(
                    "Google AI Studio file upload did not complete (prompt-media-container not found)."
                )
        except Exception as e:
            Logger.warning(f"Google AI Studio: file upload failed: {e}")
            raise
        finally:
            if temp_dir:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

    async def _is_system_prompt_panel_open(self) -> bool:
        """Return whether the AI Studio system-instructions editor is currently visible."""
        title_input = await self._find_first_visible(
            self.SYSTEM_INSTRUCTIONS_TITLE_INPUT_SELECTORS,
            timeout_ms=0,
        )
        if title_input is not None:
            return True

        textarea = await self._find_first_visible(
            self.SYSTEM_INSTRUCTIONS_TEXTAREA_SELECTORS,
            timeout_ms=0,
        )
        return textarea is not None

    async def _open_system_prompt_panel(self) -> bool:
        """Open AI Studio's System Instructions panel."""
        if not self.page:
            return False
        started = time.perf_counter()
        Logger.extra_debug("Google AI Studio timing: system-instructions open panel -> start")
        if await self._is_system_prompt_panel_open():
            Logger.extra_debug(
                "Google AI Studio timing: system-instructions open panel -> "
                f"already open in {time.perf_counter() - started:.3f}s"
            )
            return True

        find_started = time.perf_counter()
        trigger = await self._find_first_visible(self.SYSTEM_INSTRUCTIONS_CARD_SELECTORS, timeout_ms=8000)
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions open panel -> trigger "
            f"{'found' if trigger is not None else 'missing'} in "
            f"{time.perf_counter() - find_started:.3f}s"
        )
        if trigger is None:
            Logger.warning("Google AI Studio: system-instructions trigger was not found.")
            return False

        for attempt in range(1, 5):
            await self._dismiss_transient_overlays()
            click_started = time.perf_counter()
            try:
                await self._click_locator(trigger, timeout=2000, force=True)
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions open panel -> "
                    f"attempt {attempt} click completed in "
                    f"{time.perf_counter() - click_started:.3f}s"
                )
            except Exception as e:
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions open panel -> "
                    f"attempt {attempt} click failed after "
                    f"{time.perf_counter() - click_started:.3f}s: {e}"
                )
                try:
                    await trigger.evaluate("el => el.click()", timeout=1000)
                    Logger.extra_debug(
                        "Google AI Studio timing: system-instructions open panel -> "
                        f"attempt {attempt} JS click completed"
                    )
                except Exception as js_error:
                    Logger.extra_debug(
                        "Google AI Studio timing: system-instructions open panel -> "
                        f"attempt {attempt} JS click failed: {js_error}"
                    )
                    pass

            await self._ui_settle_pause(0.16)
            if await self._is_system_prompt_panel_open():
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions open panel -> "
                    f"opened on attempt {attempt} in {time.perf_counter() - started:.3f}s"
                )
                return True

        Logger.extra_debug(
            "Google AI Studio timing: system-instructions open panel -> "
            f"failed after {time.perf_counter() - started:.3f}s"
        )
        Logger.warning("Google AI Studio: system-instructions panel did not open.")
        return False

    async def _wait_for_system_prompt_panel_closed(self, timeout_ms: int = 0) -> bool:
        """Return once the system-instructions panel is no longer visible."""
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            if not await self._is_system_prompt_panel_open():
                return True
            if timeout_ms <= 0 or time.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _click_system_prompt_close_button_with_js(self) -> bool:
        """Click the current close button through the page DOM without locator auto-waiting."""
        if not self.page:
            return False
        try:
            return bool(
                await self.page.evaluate(
                    """(selectors) => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                            const style = window.getComputedStyle(el);
                            if (!style) return false;
                            return style.display !== 'none' && style.visibility !== 'hidden';
                        };

                        for (const selector of selectors || []) {
                            for (const el of Array.from(document.querySelectorAll(selector))) {
                                if (!isVisible(el)) continue;
                                try {
                                    el.click();
                                    return true;
                                } catch (e) {}
                            }
                        }
                        return false;
                    }""",
                    self.SYSTEM_INSTRUCTIONS_CLOSE_SELECTORS,
                )
            )
        except Exception:
            return False

    async def _close_system_prompt_panel(self) -> bool:
        """Close AI Studio's System Instructions panel."""
        if not self.page:
            return False
        started = time.perf_counter()
        Logger.extra_debug("Google AI Studio timing: system-instructions close panel -> start")
        if not await self._is_system_prompt_panel_open():
            Logger.extra_debug(
                "Google AI Studio timing: system-instructions close panel -> "
                f"already closed in {time.perf_counter() - started:.3f}s"
            )
            return True

        for attempt in range(1, 5):
            find_started = time.perf_counter()
            close_button = await self._find_first_visible(
                self.SYSTEM_INSTRUCTIONS_CLOSE_SELECTORS,
                timeout_ms=5000 if attempt == 1 else 1000,
            )
            Logger.extra_debug(
                "Google AI Studio timing: system-instructions close panel -> close button "
                f"{'found' if close_button is not None else 'missing'} on attempt {attempt} in "
                f"{time.perf_counter() - find_started:.3f}s"
            )
            if close_button is None:
                wait_started = time.perf_counter()
                if await self._wait_for_system_prompt_panel_closed(timeout_ms=1500):
                    Logger.extra_debug(
                        "Google AI Studio timing: system-instructions close panel -> "
                        f"closed after close button disappeared in "
                        f"{time.perf_counter() - wait_started:.3f}s "
                        f"(total {time.perf_counter() - started:.3f}s)"
                    )
                    return True
                continue

            click_started = time.perf_counter()
            try:
                await self._click_locator(close_button, timeout=2000, force=True)
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions close panel -> "
                    f"attempt {attempt} click completed in "
                    f"{time.perf_counter() - click_started:.3f}s"
                )
            except Exception as e:
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions close panel -> "
                    f"attempt {attempt} click failed after "
                    f"{time.perf_counter() - click_started:.3f}s: {e}"
                )

            wait_started = time.perf_counter()
            if await self._wait_for_system_prompt_panel_closed(timeout_ms=3500):
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions close panel -> "
                    f"closed after attempt {attempt} click in "
                    f"{time.perf_counter() - wait_started:.3f}s "
                    f"(total {time.perf_counter() - started:.3f}s)"
                )
                return True

            js_started = time.perf_counter()
            js_clicked = await self._click_system_prompt_close_button_with_js()
            Logger.extra_debug(
                "Google AI Studio timing: system-instructions close panel -> "
                f"attempt {attempt} page JS click "
                f"{'completed' if js_clicked else 'found no button'} in "
                f"{time.perf_counter() - js_started:.3f}s"
            )
            if js_clicked and await self._wait_for_system_prompt_panel_closed(timeout_ms=2500):
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions close panel -> "
                    f"closed after attempt {attempt} page JS click in "
                    f"{time.perf_counter() - started:.3f}s"
                )
                return True

        Logger.extra_debug(
            "Google AI Studio timing: system-instructions close panel -> "
            f"failed after {time.perf_counter() - started:.3f}s"
        )
        Logger.warning("Google AI Studio: system-instructions panel did not close cleanly.")
        return False

    async def _wait_for_system_prompt_save_status(self, timeout_ms: int = 12000) -> bool:
        """Wait until AI Studio reports that the system instructions were saved."""
        if not self.page:
            return False

        started = time.perf_counter()
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions save status wait -> "
            f"start timeout_ms={int(timeout_ms)}"
        )
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            try:
                save_visible = await self.page.evaluate(
                    """(selector) => {
                        return Array.from(document.querySelectorAll(selector)).some(
                            (status) => status.classList.contains('visible')
                        );
                    }""",
                    self.SYSTEM_INSTRUCTIONS_SAVE_STATUS_SELECTOR,
                )
            except Exception:
                save_visible = False

            if bool(save_visible):
                await self._ui_settle_pause(0.12)
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions save status wait -> "
                    f"visible in {time.perf_counter() - started:.3f}s"
                )
                return True

            if time.time() >= deadline:
                Logger.extra_debug(
                    "Google AI Studio timing: system-instructions save status wait -> "
                    f"timed out after {time.perf_counter() - started:.3f}s"
                )
                Logger.warning(
                    "Google AI Studio: system-instructions save status did not become visible in time."
                )
                return False

            await asyncio.sleep(0.1)

    def _build_system_prompt_title(self) -> str:
        """Generate a short unique label for AI Studio's system-instructions entry."""
        return f"intenserp-{secrets.token_hex(8)}"

    async def _sync_system_prompt_field(self, system_prompt_text: str) -> None:
        """Write or clear AI Studio's System Instructions field for the current chat."""
        if not self.page:
            return

        started = time.perf_counter()
        system_prompt_text = str(system_prompt_text or "")
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> "
            f"start chars={len(system_prompt_text)}"
        )
        open_started = time.perf_counter()
        opened = await self._open_system_prompt_panel()
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> open panel "
            f"{'ok' if opened else 'failed'} in {time.perf_counter() - open_started:.3f}s"
        )
        if not opened:
            return

        title_value = self._build_system_prompt_title()
        if system_prompt_text.strip():
            title_started = time.perf_counter()
            title_ok = await self._set_input_value(
                self.SYSTEM_INSTRUCTIONS_TITLE_INPUT_SELECTORS[0],
                title_value,
                timeout_ms=5000,
                debug_label="system-instructions-title",
            )
            if not title_ok:
                title_field = await self._find_first_visible(
                    self.SYSTEM_INSTRUCTIONS_TITLE_INPUT_SELECTORS,
                    timeout_ms=2000,
                )
                title_ok = await self._set_text_control_value(
                    title_field,
                    title_value,
                    debug_label="system-instructions-title-fallback",
                )
            Logger.extra_debug(
                "Google AI Studio timing: system-instructions sync -> title "
                f"{'ok' if title_ok else 'failed'} in {time.perf_counter() - title_started:.3f}s"
            )
            if not title_ok:
                Logger.warning("Google AI Studio: failed to set the system-instructions title.")

        textarea_started = time.perf_counter()
        textarea = await self._find_first_visible(
            self.SYSTEM_INSTRUCTIONS_TEXTAREA_SELECTORS,
            timeout_ms=8000,
        )
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> textarea "
            f"{'found' if textarea is not None else 'missing'} in "
            f"{time.perf_counter() - textarea_started:.3f}s"
        )
        if textarea is None:
            Logger.warning("Google AI Studio: system-instructions textarea was not found.")
            await self._close_system_prompt_panel()
            return

        body_started = time.perf_counter()
        body_ok = await self._set_text_control_value(
            textarea,
            system_prompt_text,
            debug_label="system-instructions-body",
        )
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> body "
            f"{'ok' if body_ok else 'failed'} in {time.perf_counter() - body_started:.3f}s"
        )
        if not body_ok:
            Logger.warning("Google AI Studio: failed to set the system-instructions body.")

        save_started = time.perf_counter()
        await self._wait_for_system_prompt_save_status()
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> save wait returned in "
            f"{time.perf_counter() - save_started:.3f}s"
        )
        close_started = time.perf_counter()
        await self._close_system_prompt_panel()
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> close panel returned in "
            f"{time.perf_counter() - close_started:.3f}s"
        )
        refocus_started = time.perf_counter()
        await self._refocus_composer_before_send()
        Logger.extra_debug(
            "Google AI Studio timing: system-instructions sync -> refocus returned in "
            f"{time.perf_counter() - refocus_started:.3f}s "
            f"(total {time.perf_counter() - started:.3f}s)"
        )

    async def enter_message(self, message: str) -> None:
        """Populate the prompt composer without relying on paste shortcuts."""
        if not self.page:
            return

        started = time.perf_counter()
        message = str(message or "")
        Logger.extra_debug(
            "Google AI Studio timing: enter prompt -> "
            f"start chars={len(message)}"
        )
        find_started = time.perf_counter()
        editor = await self._find_first_visible(self.CHAT_READY_SELECTORS, timeout_ms=15000)
        Logger.extra_debug(
            "Google AI Studio timing: enter prompt -> editor "
            f"{'found' if editor is not None else 'missing'} in "
            f"{time.perf_counter() - find_started:.3f}s"
        )
        if editor is None:
            Logger.warning("Google AI Studio: prompt textarea was not found.")
            return

        set_started = time.perf_counter()
        ok = await self._set_text_control_value(
            editor,
            message,
            nested_selector="textarea",
            debug_label="prompt-composer",
        )
        Logger.extra_debug(
            "Google AI Studio timing: enter prompt -> set text "
            f"{'ok' if ok else 'failed'} in {time.perf_counter() - set_started:.3f}s "
            f"(total {time.perf_counter() - started:.3f}s)"
        )
        if not ok:
            Logger.warning("Google AI Studio: failed to enter the prompt.")

    async def _find_send_button(self, timeout_ms: int = 8000):
        """Locate the first visible AI Studio send/run button."""
        return await self._find_first_visible(self.SEND_BUTTON_SELECTORS, timeout_ms=timeout_ms)

    async def _read_prompt_composer_text(self, timeout_ms: int = 0) -> str | None:
        """Return the current visible prompt composer text, if it can be read."""
        editor = await self._find_first_visible(self.CHAT_READY_SELECTORS, timeout_ms=timeout_ms)
        if editor is None:
            return None

        try:
            return str(
                await editor.evaluate(
                    """(el) => {
                        const target = (el && typeof el.value !== 'undefined')
                            ? el
                            : (el && el.querySelector ? el.querySelector('textarea') : null);
                        if (!target) return '';
                        return (target.value ?? target.textContent ?? '').toString();
                    }"""
                )
                or ""
            )
        except Exception:
            return None

    async def _prompt_has_attached_media(self, timeout_ms: int = 0) -> bool:
        """Return whether the prompt composer currently shows an attached media chip."""
        media = await self._find_first_visible(
            [self.PROMPT_MEDIA_CONTAINER_SELECTOR],
            timeout_ms=timeout_ms,
        )
        return media is not None

    async def _send_button_is_clickable(self, button) -> bool:
        """Return whether AI Studio's run control currently looks ready to submit."""
        if button is None:
            return False

        try:
            if not await button.is_visible():
                return False
        except Exception:
            return False

        if await self._run_control_looks_like_stop(button):
            return False

        try:
            if not await button.is_enabled():
                return False
        except Exception:
            pass

        disabled_attr = None
        try:
            disabled_attr = await button.get_attribute("disabled")
        except Exception:
            disabled_attr = None

        aria_disabled = ""
        try:
            aria_disabled = str(await button.get_attribute("aria-disabled") or "").strip().lower()
        except Exception:
            aria_disabled = ""

        return disabled_attr is None and aria_disabled != "true"

    async def _wait_for_send_click_observed(
        self,
        *,
        previous_composer_text: str | None,
        previous_prompt_had_media: bool = False,
        completion_started: asyncio.Event | None = None,
        timeout_ms: int | None = None,
    ) -> bool:
        """Wait until the UI proves a send click was accepted."""
        timeout = (
            self.SEND_CLICK_CONFIRM_TIMEOUT_MS
            if timeout_ms is None
            else max(0, int(timeout_ms))
        )
        deadline = time.time() + (float(timeout) / 1000.0)
        had_text = bool(str(previous_composer_text or "").strip())

        while True:
            if completion_started is not None and completion_started.is_set():
                return True

            if await self._find_stop_generation_button(timeout_ms=0) is not None:
                return True

            if had_text:
                current_text = await self._read_prompt_composer_text(timeout_ms=0)
                if current_text is not None and not current_text.strip():
                    return True

            if previous_prompt_had_media and not await self._prompt_has_attached_media(timeout_ms=0):
                return True

            if timeout <= 0 or time.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _run_control_looks_like_stop(self, button) -> bool:
        """Return whether AI Studio's run control is currently in Stop mode."""
        if button is None:
            return False

        try:
            signature = str(
                await button.evaluate(
                    r"""(el) => {
                        const normalize = (value) => (value || '').toString().replace(/\s+/g, ' ').trim().toLowerCase();
                        const values = [];
                        const attrNames = [
                            'aria-label',
                            'mattooltip',
                            'data-test-id',
                            'name',
                            'title',
                        ];
                        for (const name of attrNames) {
                            values.push(normalize(el.getAttribute(name)));
                        }
                        values.push(normalize(el.innerText || el.textContent || ''));
                        for (const icon of Array.from(el.querySelectorAll('mat-icon, .material-symbols-outlined, .material-icons'))) {
                            values.push(normalize(icon.innerText || icon.textContent || ''));
                        }
                        return values.filter(Boolean).join(' ');
                    }"""
                )
                or ""
            ).strip().lower()
        except Exception:
            return False

        if not signature:
            return False

        if "system instruction" in signature or "system-instruction" in signature:
            return False
        if "stop" not in signature:
            return False
        if "run-button" in signature:
            return True
        if "run button" in signature:
            return True
        if re.search(r"(^|\s)stop($|\s)", signature):
            return True
        return False

    async def _find_stop_generation_button(self, timeout_ms: int = 0):
        """Locate AI Studio's visible Stop button while a response is running."""
        if not self.page:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            for selector in self.STOP_BUTTON_SELECTORS:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0

                for idx in range(min(count, 8)):
                    candidate = locator.nth(idx)
                    try:
                        if not await candidate.is_visible():
                            continue
                    except Exception:
                        continue

                    if await self._run_control_looks_like_stop(candidate):
                        return candidate

            if timeout_ms <= 0 or time.time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def _click_stop_generation(self) -> bool:
        """Best-effort click AI Studio's Stop button for the active response."""
        button = await self._find_stop_generation_button(timeout_ms=1200)
        if button is None:
            return False

        try:
            await self._click_locator(button, timeout=2000)
            await self._ui_settle_pause(0.2)
            return True
        except Exception as e:
            try:
                await self._click_locator(button, timeout=2000, force=True)
                await self._ui_settle_pause(0.2)
                return True
            except Exception:
                Logger.warning(f"Google AI Studio: failed to click the stop button: {e}")
                return False

    async def _wait_for_generation_idle(self, timeout_ms: int = 10000) -> bool:
        """Wait until AI Studio no longer shows a visible Stop button."""
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            if await self._find_stop_generation_button(timeout_ms=0) is None:
                return True
            if timeout_ms <= 0 or time.time() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def send_message(
        self,
        timeout: int | None = None,
        *,
        completion_started: asyncio.Event | None = None,
    ) -> None:
        """Wait for the send button to become enabled and click it."""
        wait_timeout_s = 15.0 if timeout is None else max(float(timeout), 0.0)
        deadline = time.time() + wait_timeout_s
        last_state = "not found"

        while True:
            button = await self._find_send_button(timeout_ms=1000)
            if button is None:
                last_state = "not found"
                if time.time() >= deadline:
                    Logger.warning("Google AI Studio: send button was not found.")
                    return
                await asyncio.sleep(0.15)
                continue

            if await self._run_control_looks_like_stop(button):
                return

            if await self._send_button_is_clickable(button):
                break

            last_state = "disabled"
            if time.time() >= deadline:
                Logger.warning(
                    "Google AI Studio: send button did not become enabled in time "
                    f"(last state: {last_state})."
                )
                return
            await asyncio.sleep(0.15)

        last_error: Exception | None = None
        previous_composer_text = await self._read_prompt_composer_text(timeout_ms=0)
        previous_prompt_had_media = await self._prompt_has_attached_media(timeout_ms=0)
        for attempt in range(1, self.SEND_CLICK_MAX_ATTEMPTS + 1):
            if completion_started is not None and completion_started.is_set():
                return
            if await self._find_stop_generation_button(timeout_ms=0) is not None:
                return

            if attempt > 1:
                button = await self._find_send_button(timeout_ms=1200)
                if button is None:
                    last_state = "not found"
                    if await self._wait_for_send_click_observed(
                        previous_composer_text=previous_composer_text,
                        previous_prompt_had_media=previous_prompt_had_media,
                        completion_started=completion_started,
                        timeout_ms=900,
                    ):
                        return
                    continue
                if await self._run_control_looks_like_stop(button):
                    return
                if not await self._send_button_is_clickable(button):
                    last_state = "disabled"
                    if await self._wait_for_send_click_observed(
                        previous_composer_text=previous_composer_text,
                        previous_prompt_had_media=previous_prompt_had_media,
                        completion_started=completion_started,
                        timeout_ms=900,
                    ):
                        return
                    await self._ui_settle_pause(0.2)
                    continue

            try:
                await self._refocus_composer_before_send()
                await self._click_locator(button, timeout=3000)
                await self._ui_settle_pause(0.25)
            except Exception as e:
                last_error = e
                try:
                    await self._refocus_composer_before_send()
                    await self._click_locator(button, timeout=3000, force=True)
                    await self._ui_settle_pause(0.25)
                except Exception as force_error:
                    last_error = force_error
                    Logger.debug(
                        "Google AI Studio: send button click failed "
                        f"on attempt {attempt}/{self.SEND_CLICK_MAX_ATTEMPTS}: "
                        f"{force_error} (initial click error: {e})"
                    )
                    await self._ui_settle_pause(0.25)
                    continue

            if await self._wait_for_send_click_observed(
                previous_composer_text=previous_composer_text,
                previous_prompt_had_media=previous_prompt_had_media,
                completion_started=completion_started,
            ):
                return

            Logger.warning(
                "Google AI Studio: send click was not confirmed "
                f"on attempt {attempt}/{self.SEND_CLICK_MAX_ATTEMPTS}; retrying."
            )
            await self._ui_settle_pause(0.35)

        if last_error:
            Logger.warning(f"Google AI Studio: failed to click the send button: {last_error}")
        else:
            Logger.warning(
                "Google AI Studio: send button click did not register "
                f"(last state: {last_state})."
            )

    @staticmethod
    def _is_generate_content_url(url: str) -> bool:
        return AIStudioDriver.GENERATE_URL_SUBSTRING in str(url or "")

    def _get_request_capture_mode(self) -> str:
        try:
            mode = str(
                self.config_manager.get_setting("aistudio_behavior", "request_capture_mode")
                or self.REQUEST_CAPTURE_MODE_REPLAY
            ).strip().lower()
        except Exception:
            mode = self.REQUEST_CAPTURE_MODE_REPLAY

        if mode == self.REQUEST_CAPTURE_MODE_CDP_TEEING:
            return self.REQUEST_CAPTURE_MODE_CDP_TEEING
        return self.REQUEST_CAPTURE_MODE_REPLAY

    def _is_generate_content_response(self, response) -> bool:
        """Return whether a Playwright response matches AI Studio's GenerateContent call."""
        try:
            url = str(getattr(response, "url", "") or "")
        except Exception:
            url = ""
        if not self._is_generate_content_url(url):
            return False

        request = None
        try:
            request = response.request
        except Exception:
            request = None

        try:
            method = str(getattr(request, "method", "") or "").upper()
        except Exception:
            method = ""
        if method != "POST":
            return False

        try:
            status = int(getattr(response, "status", 0) or 0)
        except Exception:
            status = 0
        if status and status < 200:
            return False

        return True

    async def _capture_generate_response_body(
        self,
        response,
        queue: asyncio.Queue,
        *,
        send_deepthink: bool,
    ) -> None:
        """Parse a completed GenerateContent response body into OpenAI-style deltas."""
        try:
            await response.finished()
        except Exception:
            pass

        try:
            body = await response.body()
        except Exception as e:
            Logger.error(f"Google AI Studio: failed to read response body: {e}")
            await queue.put({"error": str(e)})
            await queue.put(None)
            return

        parser = _AiStudioJsonEventStreamParser()
        emitted_text = False
        try:
            for parsed_event in parser.feed(body):
                emitted = await self._process_stream_event(
                    parsed_event,
                    queue,
                    send_deepthink=send_deepthink,
                )
                emitted_text = emitted_text or emitted
            for parsed_event in parser.finish():
                emitted = await self._process_stream_event(
                    parsed_event,
                    queue,
                    send_deepthink=send_deepthink,
                )
                emitted_text = emitted_text or emitted
        except Exception as e:
            Logger.error(f"Google AI Studio: failed to parse response stream: {e}")
            await queue.put({"error": str(e)})
            await queue.put(None)
            return

        if self.thinking_active:
            if send_deepthink:
                await self._enqueue_openai_delta(queue, "</think>")
            self.thinking_active = False

        if not emitted_text:
            message = (
                "Google AI Studio returned no assistant text. "
                "The request may have been submitted before the UI fully settled."
            )
            Logger.warning(message)
            await queue.put({"error": message})
            await queue.put(None)
            return

        await self._enqueue_openai_delta(queue, "", finish_reason="stop")
        await queue.put(None)
        Logger.success("Google AI Studio response capture completed.")

    @staticmethod
    def _iter_nested_strings(value: Any):
        """Yield every nested string inside a parsed AI Studio event payload."""
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from AIStudioDriver._iter_nested_strings(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from AIStudioDriver._iter_nested_strings(item)

    @classmethod
    def _event_looks_hard_censored(cls, event: Any) -> bool:
        """Return whether a parsed event contains a hard-censorship style marker."""
        for text in cls._iter_nested_strings(event):
            normalized = cls._normalize_text(text)
            if not normalized:
                continue
            if "content blocked" in normalized:
                return True
            if "could not generate output" in normalized:
                return True
            if "prohibited use policy" in normalized:
                return True
        return False

    @classmethod
    def _event_looks_rate_limited(cls, event: Any) -> bool:
        """Return whether a parsed event contains rate-limit or quota wording."""
        for text in cls._iter_nested_strings(event):
            if cls.RATE_LIMIT_HINT_RE.search(str(text or "")):
                return True
        return False

    @classmethod
    def _error_looks_rate_limited(cls, message: str) -> bool:
        """Return whether an error string should trigger reload-on-failure rotation."""
        return bool(cls.RATE_LIMIT_HINT_RE.search(str(message or "")))

    @staticmethod
    def _build_rate_limit_error_message(status_code: int = 0) -> str:
        """Build a rate-limit-like error string that the API worker can recognize."""
        if int(status_code or 0) == 429:
            return "Google AI Studio rate limit (429): too many requests."
        return "Google AI Studio rate limit: quota or request limit reached."

    async def _find_latest_visible_assistant_turn(self):
        """Return the latest visible assistant turn and its hover container."""
        if not self.page:
            return None, None

        turns = self.page.locator(self.ASSISTANT_TURN_SELECTOR)
        try:
            count = await turns.count()
        except Exception:
            count = 0

        for idx in range(count - 1, -1, -1):
            turn = turns.nth(idx)
            try:
                if not await turn.is_visible():
                    continue
            except Exception:
                continue

            container = turn.locator("xpath=..")
            try:
                return turn, container.first
            except Exception:
                return turn, turn

        return None, None

    async def _hover_assistant_turn_controls(self, turn, container) -> bool:
        """Hover the latest assistant turn so edit/regenerate controls become visible."""
        for target in (container, turn):
            if target is None:
                continue
            try:
                await self._restore_minimized_window_for_hmm()
                await target.hover(timeout=3000)
                await asyncio.sleep(0.15)
                return True
            except Exception:
                continue
        return False

    async def _is_latest_assistant_turn_content_blocked(self, timeout_ms: int = 0) -> bool:
        """Return whether the latest assistant turn shows AI Studio's ``Content blocked`` marker."""
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            turn, container = await self._find_latest_visible_assistant_turn()
            root = container or turn
            if root is not None:
                try:
                    blocked = bool(
                        await root.evaluate(
                            r"""(el, selector) => {
                                const normalize = (value) => (value || '').toString().replace(/\s+/g, ' ').trim().toLowerCase();
                                const isVisible = (node) => {
                                    if (!node) return false;
                                    const rect = node.getBoundingClientRect();
                                    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                                    const style = window.getComputedStyle(node);
                                    if (!style) return false;
                                    return style.visibility !== 'hidden' && style.display !== 'none';
                                };

                                const buttons = Array.from(el.querySelectorAll(selector)).filter(isVisible);
                                for (const button of buttons) {
                                    const ariaLabel = normalize(button.getAttribute('aria-label'));
                                    if (!ariaLabel.includes('safety ratings')) continue;

                                    const text = normalize(button.textContent || '');
                                    if (text.includes('content blocked')) return true;

                                    const secondChild = button.childNodes && button.childNodes.length > 1
                                        ? normalize(button.childNodes[1].textContent || '')
                                        : '';
                                    if (secondChild === 'content blocked') return true;
                                }
                                return false;
                            }""",
                            self.SAFETY_RATINGS_BUTTON_SELECTOR,
                        )
                    )
                except Exception:
                    blocked = False

                if blocked:
                    return True

            if timeout_ms <= 0 or time.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _latest_assistant_turn_visible_text(self) -> str:
        """Return normalized visible text from the latest assistant turn."""
        turn, _container = await self._find_latest_visible_assistant_turn()
        if turn is None:
            return ""

        try:
            text = str(
                await turn.evaluate(
                    r"""(el) => {
                        const normalize = (value) => (value || '').toString().replace(/\s+/g, ' ').trim();
                        return normalize(el.innerText || el.textContent || '');
                    }"""
                )
                or ""
            )
        except Exception:
            return ""

        if text.strip().lower() == "content blocked":
            return ""
        return text.strip()

    async def _latest_assistant_turn_has_visible_text(self) -> bool:
        """Return whether the latest assistant turn has visible generated text."""
        return bool(await self._latest_assistant_turn_visible_text())

    def _anti_censorship_edit_save_timeout_ms(self) -> int:
        value = self._clamp_int(
            self.config_manager.get_setting(
                "aistudio_behavior",
                "anti_censorship_edit_save_timeout",
            ),
            10,
            1,
            60,
        )
        return int(value) * 1000

    def _anti_censorship_edit_save_retries(self) -> int:
        return self._clamp_int(
            self.config_manager.get_setting(
                "aistudio_behavior",
                "anti_censorship_edit_save_retries",
            ),
            2,
            0,
            5,
        )

    async def _track_assistant_edit_textarea(self, textarea):
        """Return a locator tied to the exact assistant edit textarea when possible."""
        if not self.page or textarea is None:
            return textarea

        token = f"irp-edit-{secrets.token_hex(8)}"
        try:
            await textarea.evaluate(
                "(el, value) => el.setAttribute('data-irp-assistant-edit-token', value)",
                token,
            )
            return self.page.locator(f"textarea[data-irp-assistant-edit-token='{token}']")
        except Exception:
            return textarea

    async def _assistant_edit_textarea_visible(self, textarea) -> bool:
        if textarea is None:
            return False

        try:
            count = await textarea.count()
            if count <= 0:
                return False
        except Exception:
            pass

        try:
            return bool(await textarea.is_visible())
        except Exception:
            return False

    async def _looks_like_prompt_composer_textarea(self, textarea) -> bool:
        """Return whether a textarea is AI Studio's main prompt composer."""
        if textarea is None:
            return False

        try:
            return bool(
                await textarea.evaluate(
                    r"""(el) => {
                        const normalize = (value) => (value || '').toString().replace(/\s+/g, ' ').trim().toLowerCase();
                        const placeholder = normalize(el.getAttribute('placeholder'));
                        if (placeholder.includes('start typing a prompt')) return true;
                        if (placeholder.includes('enter a prompt')) return true;
                        if (placeholder.includes('type a prompt')) return true;

                        const label = normalize(el.getAttribute('aria-label'));
                        if (label.includes('prompt')) return true;

                        const composerHost = el.closest(
                            'ms-prompt-input, ms-run-input, xap-prompt-input, ' +
                            '.prompt-input, .prompt-input-wrapper, .prompt-input-container, ' +
                            '.composer, .composer-container, .input-area, .input-container'
                        );
                        const turnHost = el.closest(
                            'div.chat-turn-container.code-block-aligner.model.render.ng-star-inserted, ' +
                            'ms-chat-turn'
                        );
                        return Boolean(composerHost && !turnHost);
                    }"""
                )
            )
        except Exception:
            return False

    async def _is_assistant_edit_textarea_candidate(self, textarea) -> bool:
        if textarea is None:
            return False
        try:
            if not await textarea.is_visible():
                return False
        except Exception:
            return False
        return not await self._looks_like_prompt_composer_textarea(textarea)

    async def _find_first_assistant_edit_textarea_within(self, root, timeout_ms: int = 0):
        if root is None:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            for selector in self.ASSISTANT_EDIT_TEXTAREA_SELECTORS:
                locator = root.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0

                for idx in range(min(count, 10)):
                    candidate = locator.nth(idx)
                    if await self._is_assistant_edit_textarea_candidate(candidate):
                        return candidate

            if timeout_ms <= 0 or time.time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def _find_assistant_edit_save_button(self, container, timeout_ms: int = 0):
        """Find the visible Save/Done button for an assistant edit session."""
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            button = None
            if container is not None:
                button = await self._find_first_visible_within(
                    container,
                    self.ASSISTANT_EDIT_SAVE_BUTTON_SELECTORS,
                    timeout_ms=0,
                )
            if button is None:
                button = await self._find_first_visible(
                    self.ASSISTANT_EDIT_SAVE_BUTTON_SELECTORS,
                    timeout_ms=0,
                )

            if button is not None:
                try:
                    if await button.is_enabled():
                        return button
                except Exception:
                    return button

            if timeout_ms <= 0 or time.time() >= deadline:
                return None
            await asyncio.sleep(0.15)

    async def _wait_for_assistant_edit_to_close(self, textarea, timeout_ms: int) -> bool:
        """Wait until AI Studio has left assistant edit mode."""
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            if not await self._assistant_edit_textarea_visible(textarea):
                return True
            if timeout_ms <= 0 or time.time() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def _press_assistant_edit_shortcut(self, textarea) -> bool:
        try:
            await textarea.press("Control+Enter", timeout=3000)
            return True
        except Exception:
            try:
                if self.page:
                    await self.page.keyboard.press("Control+Enter")
                    return True
            except Exception:
                return False
        return False

    async def _submit_assistant_edit(self, textarea, container) -> bool:
        """Click/wait for AI Studio's assistant edit save instead of racing the composer."""
        timeout_ms = self._anti_censorship_edit_save_timeout_ms()
        attempts = self._anti_censorship_edit_save_retries() + 1

        for attempt in range(1, attempts + 1):
            button_wait_ms = timeout_ms if attempt > 1 else min(timeout_ms, 2000)
            save_button = await self._find_assistant_edit_save_button(
                container,
                timeout_ms=button_wait_ms,
            )
            if save_button is not None:
                try:
                    await self._click_locator(save_button, timeout=3000)
                except Exception:
                    try:
                        await self._click_locator(save_button, timeout=3000, force=True)
                    except Exception as e:
                        Logger.debug(
                            "Google AI Studio: assistant edit save button click failed "
                            f"on attempt {attempt}: {e}"
                        )
            else:
                Logger.debug(
                    "Google AI Studio: assistant edit save button was not visible "
                    f"on attempt {attempt}; trying Ctrl+Enter."
                )
                await self._press_assistant_edit_shortcut(textarea)

            if await self._wait_for_assistant_edit_to_close(textarea, timeout_ms=timeout_ms):
                await self._ui_settle_pause(0.25)
                return True

            Logger.warning(
                "Google AI Studio: assistant edit did not finish saving "
                f"on attempt {attempt}/{attempts}."
            )
            await self._ui_settle_pause(0.35)

        return False

    async def _open_assistant_edit_mode(self, turn, container):
        """Open assistant edit mode and return its textarea, proving it is not the composer."""
        for attempt in range(1, 4):
            if not await self._hover_assistant_turn_controls(turn, container):
                Logger.warning(
                    "Google AI Studio: could not reveal assistant controls "
                    f"for anti-censorship replacement on attempt {attempt}/3."
                )
                await self._ui_settle_pause(0.3)
                continue

            await self._ui_settle_pause(0.25)
            edit_button = await self._find_first_visible_within(
                container,
                self.ASSISTANT_EDIT_BUTTON_SELECTORS,
                timeout_ms=1600,
            )
            if edit_button is None:
                Logger.warning(
                    "Google AI Studio: assistant edit button was not found "
                    f"on attempt {attempt}/3."
                )
                await self._ui_settle_pause(0.35)
                continue

            try:
                await self._click_locator(edit_button, timeout=3500)
            except Exception as e:
                try:
                    await self._click_locator(edit_button, timeout=3500, force=True)
                except Exception:
                    Logger.warning(
                        f"Google AI Studio: failed to click assistant edit button "
                        f"on attempt {attempt}/3: {e}"
                    )
                    await self._ui_settle_pause(0.35)
                    continue

            await self._ui_settle_pause(0.45)
            textarea = await self._find_assistant_edit_textarea(container, timeout_ms=3000)
            if textarea is not None:
                return textarea

            Logger.warning(
                "Google AI Studio: assistant edit button was clicked, but edit mode "
                f"did not open on attempt {attempt}/3."
            )
            await self._ui_settle_pause(0.45)

        return None

    async def _replace_latest_assistant_message(self, replacement_text: str) -> bool:
        """Edit the latest assistant turn in-place and replace it with the provided text."""
        if await self._is_system_prompt_panel_open():
            Logger.warning(
                "Google AI Studio: system-instructions panel was open before assistant "
                "replacement; closing it before looking for the edit button."
            )
            if not await self._close_system_prompt_panel():
                return False
            await self._ui_settle_pause(0.2)

        turn, container = await self._find_latest_visible_assistant_turn()
        if turn is None or container is None:
            Logger.warning("Google AI Studio: assistant turn for anti-censorship replacement was not found.")
            return False

        textarea = await self._open_assistant_edit_mode(turn, container)
        if textarea is None:
            Logger.warning(
                "Google AI Studio: assistant edit mode did not open; refusing to write "
                "the replacement into the prompt composer."
            )
            return False
        textarea = await self._track_assistant_edit_textarea(textarea)

        if not await self._set_text_control_value(textarea, str(replacement_text or "")):
            Logger.warning("Google AI Studio: failed to replace the blocked assistant message.")
            return False

        await self._ui_settle_pause(0.28)
        try:
            await self._click_locator(textarea, timeout=2000, force=True)
        except Exception:
            pass

        if not await self._submit_assistant_edit(textarea, container):
            Logger.warning("Google AI Studio: assistant edit save did not complete.")
            return False

        await self._refocus_composer_before_send()
        return True

    async def _find_assistant_edit_textarea(self, container, timeout_ms: int = 0):
        """Find the assistant-edit textarea, explicitly excluding the prompt composer."""
        scoped = await self._find_first_assistant_edit_textarea_within(
            container,
            timeout_ms=min(int(timeout_ms or 0), 1200),
        )
        if scoped is not None:
            return scoped

        if not self.page:
            return None

        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        while True:
            for selector in self.ASSISTANT_EDIT_TEXTAREA_SELECTORS:
                locator = self.page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    count = 0

                for idx in range(min(count, 10)):
                    candidate = locator.nth(idx)
                    if await self._is_assistant_edit_textarea_candidate(candidate):
                        return candidate

            if timeout_ms <= 0 or time.time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def _click_regenerate(self) -> bool:
        """Click the most recent visible regenerate button in the chat transcript."""
        if not self.page:
            return False

        turns = self.page.locator(self.ASSISTANT_TURN_SELECTOR)
        count = 0
        try:
            count = await turns.count()
        except Exception:
            count = 0
        if count == 0:
            Logger.warning("Google AI Studio: regenerate target turn was not found.")
            return False

        for idx in range(count - 1, -1, -1):
            turn = turns.nth(idx)
            try:
                if not await turn.is_visible():
                    continue
            except Exception:
                continue

            container = turn.locator("xpath=..")
            if not await self._hover_assistant_turn_controls(turn, container.first):
                continue
            button = container.locator("button[name='rerun-button']")
            if await button.count() == 0:
                button = turn.locator("xpath=ancestor::*[1]//button[@name='rerun-button']")
            if await button.count() == 0:
                continue

            try:
                await self._click_locator(button.first, timeout=3000)
                return True
            except Exception:
                continue

        Logger.warning("Google AI Studio: regenerate button was not found.")
        return False

    @staticmethod
    def _coerce_request_body_bytes(value: Any) -> bytes | None:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        return None

    def _extract_request_body_bytes(self, request) -> bytes | None:
        """Best-effort extract the intercepted request payload for httpx replay."""
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
                return json.dumps(
                    json_payload,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            except Exception:
                return None

        return None

    def _extract_aistudio_macros_from_text(self, text: str) -> tuple[str, Dict[str, Any]]:
        """Extract AI Studio-specific prompt macros from raw text."""
        return extract_macro_overrides(text, macro_actions=self.AI_STUDIO_MACRO_ACTIONS)

    def _strip_aistudio_macros_from_messages(self, messages: List[Any]) -> tuple[List[Any], Dict[str, Any]]:
        """Extract AI Studio-specific prompt macros from the last user message."""
        return strip_macros_from_messages(messages, macro_actions=self.AI_STUDIO_MACRO_ACTIONS)

    def _read_clean_regeneration_state(self) -> Optional[Dict[str, Any]]:
        """Load cached regeneration state and verify its required AI Studio fields."""
        raw = self.cache_manager.read_cache(self.clean_regen_state_cache_key)
        if raw is None:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            Logger.warning("Clean Regeneration (Google AI Studio): Cached state is invalid JSON, ignoring.")
            return None

        if not isinstance(data, dict):
            return None
        if not all(key in data for key in self.CLEAN_REGEN_STATE_KEYS):
            return None

        return dict(data)

    def _write_clean_regeneration_state(self, state: Dict[str, Any]) -> None:
        """Persist the normalized settings snapshot used by clean regeneration."""
        payload = {key: state.get(key) for key in self.CLEAN_REGEN_STATE_KEYS}
        self.cache_manager.write_cache(
            self.clean_regen_state_cache_key,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )

    def _build_clean_regeneration_state(
        self,
        settings: Dict[str, Any],
        system_prompt_text: str = "",
    ) -> Dict[str, Any]:
        """Build the normalized cache snapshot used to decide if regenerate is safe."""
        return {
            "deepthink_enabled": bool(settings.get("deepthink_enabled")),
            "thinking_level": str(settings.get("thinking_level") or ""),
            "send_deepthink": bool(settings.get("send_deepthink")),
            "search_enabled": bool(settings.get("search_enabled")),
            "url_context_enabled": bool(settings.get("url_context_enabled")),
            "use_system_prompt_field": bool(settings.get("use_system_prompt_field")),
            "system_prompt_text": str(system_prompt_text or ""),
            "send_as_text_file": bool(settings.get("send_as_text_file")),
            "text_file_message": str(settings.get("text_file_message") or ""),
            "anti_censorship": bool(settings.get("anti_censorship")),
            "anti_censorship_replacement_message": str(
                settings.get("anti_censorship_replacement_message") or "."
            ),
            "anti_censorship_continue_nudge": str(
                settings.get("anti_censorship_continue_nudge") or "Continue."
            ),
            "caars_enabled": bool(settings.get("caars_enabled")),
            "caars_savior_model": str(settings.get("caars_savior_model") or ""),
            "temperature": (
                round(float(settings.get("temperature", 1.0)), 4)
                if bool(settings.get("supports_temperature", True))
                else None
            ),
            "top_p": (
                round(float(settings.get("top_p", 0.95)), 4)
                if bool(settings.get("supports_top_p", True))
                else None
            ),
            "max_output_tokens": int(settings.get("max_output_tokens", 65536)),
            "model_base_id": str(settings.get("model_base_id") or ""),
        }

    async def _process_stream_event(
        self,
        event: Any,
        queue: asyncio.Queue,
        *,
        send_deepthink: bool,
    ) -> bool:
        """Translate one parsed AI Studio event into OpenAI-compatible stream chunks."""
        if not isinstance(event, list):
            return False

        node: Any
        try:
            node = event[0][0][0][0][0]
        except Exception:
            return False

        if not isinstance(node, list) or len(node) < 2:
            return False

        content = node[1] if isinstance(node[1], str) else ""
        is_thinking = len(node) >= 13 and node[12] == 1

        if not content:
            return False

        if is_thinking:
            if not self.thinking_active:
                if send_deepthink:
                    await self._enqueue_openai_delta(queue, "<think>")
                self.thinking_active = True
            if send_deepthink:
                await self._enqueue_openai_delta(queue, content)
                return True
            return False

        if self.thinking_active:
            if send_deepthink:
                await self._enqueue_openai_delta(queue, "</think>")
            self.thinking_active = False

        await self._enqueue_openai_delta(queue, content)
        return True

    async def _enqueue_openai_delta(
        self,
        queue: asyncio.Queue,
        content: str,
        *,
        finish_reason: str | None = None,
    ) -> None:
        """Wrap content in an OpenAI-compatible SSE chunk and enqueue it."""
        if (not content) and (not finish_reason):
            return

        model_name = self.current_model or "aistudio-auto"
        await queue.put(
            make_openai_delta_sse(
                model_name,
                content,
                finish_reason=finish_reason,
            )
        )

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        """Render messages through the shared prompt-formatting pipeline."""
        return format_request_messages(self.config_manager, messages)

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "aistudio-auto",
        stream: bool = False,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        abort_event: asyncio.Event | None = None,
    ):
        """Generate a response by driving the AI Studio UI and intercepting its stream.

        The method resolves prompt macros and request settings, prepares either a fresh
        chat or a clean-regeneration retry, intercepts the underlying GenerateContent
        request, and yields OpenAI-style server-sent event chunks back to the caller.
        """
        _ = stream
        await self._await_preflight_task()
        response_queue: asyncio.Queue = asyncio.Queue()
        completion_armed = asyncio.Event()
        completion_started = asyncio.Event()
        completion_claim_lock = asyncio.Lock()
        completion_claimed = False
        preflight_next_chat = self._preflight_next_chat_enabled()
        preflight_settings_for_next_chat: Dict[str, Any] | None = None
        preflight_system_prompt_for_next_chat = ""
        current_attempt_meta: Dict[str, Any] = {
            "hard_censorship_hint": False,
            "rate_limit_hint": False,
            "no_text_detected": False,
            "response_status": 0,
            "encountered_error": False,
            "emitted_text": False,
            "aborted": False,
        }

        await self._clear_persisted_system_instructions_if_needed()
        await self.require_english_ui()

        self.abort_requested = False
        self.current_abort_event = abort_event
        self.current_model = (model or "").strip() or "aistudio-auto"
        self.current_send_deepthink = None
        self.thinking_active = False

        macros_overrides: Dict[str, Any] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = self._strip_aistudio_macros_from_messages(message)
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = self._extract_aistudio_macros_from_text(message)

        effective_settings = self._resolve_ai_studio_request_settings(
            self.current_model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            overrides=macros_overrides,
        )
        if isinstance(message_for_formatting, str):
            effective_settings["use_system_prompt_field"] = False
        self.current_send_deepthink = bool(effective_settings["send_deepthink"])
        anti_censorship_enabled = bool(effective_settings.get("anti_censorship"))
        caars_enabled = bool(effective_settings.get("caars_enabled"))
        request_capture_mode = self._get_request_capture_mode()
        use_cdp_teeing = request_capture_mode == self.REQUEST_CAPTURE_MODE_CDP_TEEING
        route_handlers_registered = False
        cdp_session: Any = None
        cdp_listeners_registered = False
        cdp_tasks: set[asyncio.Task] = set()
        cdp_request_methods: Dict[str, str] = {}
        cdp_pending_response_meta: Dict[str, Dict[str, Any]] = {}
        cdp_active_request_id: str | None = None
        cdp_stream_started = False
        cdp_stream_finished = False
        cdp_had_data = False
        cdp_stream_state: Dict[str, Any] | None = None

        def _build_caars_savior_settings() -> Dict[str, Any]:
            savior_label = str(
                effective_settings.get("caars_savior_model") or "Gemini 3.1 Flash Lite"
            )
            savior_overrides = dict(macros_overrides)
            savior_overrides.pop("thinking_level_macro", None)
            savior_overrides["deepthink_enabled"] = False
            savior_overrides["send_deepthink"] = False
            savior_settings = self._resolve_ai_studio_request_settings(
                self.current_model or "aistudio-auto",
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                overrides=savior_overrides,
                model_label_override=savior_label,
            )
            if isinstance(message_for_formatting, str):
                savior_settings["use_system_prompt_field"] = False
            return savior_settings

        def _reset_attempt_state() -> None:
            nonlocal response_queue, completion_armed, completion_started
            nonlocal completion_claim_lock, completion_claimed, current_attempt_meta
            nonlocal cdp_active_request_id, cdp_stream_started, cdp_stream_finished
            nonlocal cdp_had_data, cdp_stream_state
            response_queue = asyncio.Queue()
            completion_armed = asyncio.Event()
            completion_started = asyncio.Event()
            completion_claim_lock = asyncio.Lock()
            completion_claimed = False
            current_attempt_meta = {
                "hard_censorship_hint": False,
                "rate_limit_hint": False,
                "no_text_detected": False,
                "response_status": 0,
                "encountered_error": False,
                "emitted_text": False,
                "aborted": False,
            }
            self.thinking_active = False
            cdp_active_request_id = None
            cdp_stream_started = False
            cdp_stream_finished = False
            cdp_had_data = False
            cdp_stream_state = None
            cdp_request_methods.clear()
            cdp_pending_response_meta.clear()

        async def _wait_for_attempt_start(log_label: str) -> bool:
            if completion_started.is_set():
                return True
            try:
                await asyncio.wait_for(completion_started.wait(), timeout=20.0)
                return True
            except asyncio.TimeoutError:
                completion_armed.clear()
                Logger.warning(
                    f"{log_label}: completion request was not observed in time. "
                    "This attempt will be abandoned."
                )
                return False

        def _stream_item_error_message(item: Any) -> str | None:
            if isinstance(item, dict) and ("error" in item):
                return str(item.get("error") or "")
            return None

        def _attempt_error_message(items: List[Any]) -> str | None:
            for item in items:
                message = _stream_item_error_message(item)
                if message:
                    return message
            return None

        def _is_terminal_stop_chunk(item: Any) -> bool:
            if not isinstance(item, str) or not item.startswith("data:"):
                return False
            try:
                parsed = json.loads(item[len("data:") :].strip())
            except Exception:
                return False
            choices = parsed.get("choices")
            if not isinstance(choices, list) or not choices:
                return False
            choice = choices[0] if isinstance(choices[0], dict) else {}
            finish_reason = choice.get("finish_reason")
            delta = choice.get("delta")
            return finish_reason == "stop" and (not delta)

        def _chunk_delta_content(item: Any) -> str | None:
            if not isinstance(item, str) or not item.startswith("data:"):
                return None
            try:
                parsed = json.loads(item[len("data:") :].strip())
            except Exception:
                return None
            choices = parsed.get("choices")
            if not isinstance(choices, list) or not choices:
                return None
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                return None
            content = delta.get("content")
            return content if isinstance(content, str) else None

        def _attempt_terminal_stop_chunk(items: List[Any]) -> str | None:
            for item in reversed(items):
                if _is_terminal_stop_chunk(item):
                    return item
            return None

        async def _attempt_clean_regeneration(
            *,
            clean_regeneration: bool,
            formatted_message: str,
            clean_regen_state: Dict[str, Any],
        ) -> bool:
            if not clean_regeneration:
                return False

            last_message = self.cache_manager.read_cache(self.clean_regen_message_cache_key)
            last_state = self._read_clean_regeneration_state()
            if (last_message != formatted_message) or (last_state != clean_regen_state):
                return False

            Logger.info(
                "Clean Regeneration (Google AI Studio): Message and settings match cache. "
                "Attempting to regenerate..."
            )
            try:
                await self._apply_request_controls(effective_settings)
                await asyncio.sleep(0.25)
            except Exception:
                pass

            _reset_attempt_state()
            completion_armed.set()
            if not await self._click_regenerate():
                completion_armed.clear()
                Logger.warning(
                    "Clean Regeneration (Google AI Studio): completion request was not observed "
                    "after clicking Regenerate. Falling back to a new chat."
                )
                return False

            return await _wait_for_attempt_start("Clean Regeneration (Google AI Studio)")

        async def _prepare_formatted_prompt_for_send(
            settings: Dict[str, Any],
            formatted_message: str,
        ) -> tuple[str | None, int | None]:
            if bool(settings.get("send_as_text_file")):
                file_payload = build_prompt_text_file_payload(formatted_message)
                try:
                    await self.upload_file(file_payload)
                except Exception as e:
                    return f"Google AI Studio upload failed: {e}", None
                await self._ui_settle_pause(0.35)
                text_file_message = str(settings.get("text_file_message") or "")
                if text_file_message.strip():
                    await self.enter_message(text_file_message)
                    await self._ui_settle_pause(0.1)
                return None, int(settings.get("file_upload_timeout", 20))

            await self.enter_message(formatted_message)
            await asyncio.sleep(0.1)
            return None, None

        async def _start_new_chat_attempt(
            *,
            formatted_message: str,
            system_prompt_text: str,
        ) -> str | None:
            _reset_attempt_state()
            preflight_consumed = False
            if preflight_next_chat:
                preflight_consumed = await self._consume_preflighted_chat(
                    effective_settings,
                    system_prompt_text,
                )

            if not preflight_consumed:
                Logger.info("Google AI Studio: preparing a new chat session...")
                await self.click_new_chat(source="auto")
                await asyncio.sleep(0.2)
                await self._apply_request_controls(effective_settings)
                await asyncio.sleep(0.2)
                if bool(effective_settings.get("use_system_prompt_field")):
                    await self._sync_system_prompt_field(system_prompt_text)
                    await self._ui_settle_pause(0.18)

            prepare_error, send_timeout = await _prepare_formatted_prompt_for_send(
                effective_settings,
                formatted_message,
            )
            if prepare_error:
                return prepare_error

            completion_armed.set()
            Logger.info("Google AI Studio: sending request...")
            await self.send_message(
                timeout=send_timeout,
                completion_started=completion_started,
            )

            if await _wait_for_attempt_start("Google AI Studio"):
                return None
            return "Google AI Studio: completion request was not observed after sending the prompt."

        async def _wait_for_caars_savior_activity(timeout_ms: int = 45000) -> tuple[bool, bool, bool]:
            deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
            stop_seen = False
            meaningful_chunks = 0
            last_visible_text = ""
            last_text_changed_at = 0.0

            while True:
                if self.abort_requested or (abort_event and abort_event.is_set()):
                    return False, False, False

                if await self._is_latest_assistant_turn_content_blocked(timeout_ms=0):
                    return False, True, False

                visible_text = await self._latest_assistant_turn_visible_text()
                if visible_text and visible_text != last_visible_text:
                    last_visible_text = visible_text
                    last_text_changed_at = time.time()
                    meaningful_chunks += 1
                    if meaningful_chunks >= self.CAARS_MEANINGFUL_CHUNK_TARGET:
                        return True, False, False

                stop_button = await self._find_stop_generation_button(timeout_ms=0)
                if stop_button is not None:
                    stop_seen = True
                elif stop_seen:
                    await self._ui_settle_pause(0.35)
                    blocked = await self._is_latest_assistant_turn_content_blocked(timeout_ms=0)
                    has_text = await self._latest_assistant_turn_has_visible_text()
                    return bool(has_text), bool(blocked), True
                elif visible_text and last_text_changed_at and (time.time() - last_text_changed_at) >= 1.5:
                    await self._ui_settle_pause(0.2)
                    if await self._find_stop_generation_button(timeout_ms=0) is None:
                        blocked = await self._is_latest_assistant_turn_content_blocked(timeout_ms=0)
                        return True, bool(blocked), True

                if time.time() >= deadline:
                    return False, False, False

                await asyncio.sleep(0.15)

        async def _run_caars_savior_prelude(
            *,
            formatted_message: str,
            system_prompt_text: str,
        ) -> str | None:
            savior_settings = _build_caars_savior_settings()
            _reset_attempt_state()
            Logger.info(
                "Google AI Studio CAARS: preparing savior prelude "
                f"with '{savior_settings.get('model_label')}'."
            )
            preflight_consumed = False
            if preflight_next_chat:
                preflight_consumed = await self._consume_preflighted_chat(
                    savior_settings,
                    system_prompt_text,
                )

            if not preflight_consumed:
                await self.click_new_chat(source="caars")
                await asyncio.sleep(0.2)
                await self._apply_request_controls(savior_settings)
                await asyncio.sleep(0.2)
                if bool(savior_settings.get("use_system_prompt_field")):
                    await self._sync_system_prompt_field(system_prompt_text)
                    await self._ui_settle_pause(0.18)

            prepare_error, send_timeout = await _prepare_formatted_prompt_for_send(
                savior_settings,
                formatted_message,
            )
            if prepare_error:
                return prepare_error

            Logger.info("Google AI Studio CAARS: sending savior model prelude...")
            await self.send_message(timeout=send_timeout)

            saw_text, saw_block, savior_completed = await _wait_for_caars_savior_activity()
            if saw_text:
                if savior_completed:
                    Logger.info(
                        "Google AI Studio CAARS: savior request completed before "
                        "the stop threshold; keeping the completed turn for replacement."
                    )
                else:
                    Logger.info(
                        "Google AI Studio CAARS: savior produced enough visible text "
                        "updates; stopping it."
                    )
                    if not await self._click_stop_generation():
                        Logger.warning(
                            "Google AI Studio CAARS: savior model produced text, but the Stop "
                            "button was not available."
                        )
                    await self._wait_for_generation_idle(timeout_ms=10000)
                    await self._ui_settle_pause(0.35)
            elif saw_block:
                Logger.info(
                    "Google AI Studio CAARS: savior turn was blocked; replacing it anyway."
                )
            else:
                await self._wait_for_generation_idle(timeout_ms=5000)
                await self._ui_settle_pause(0.35)
                saw_text = await self._latest_assistant_turn_has_visible_text()
                saw_block = await self._is_latest_assistant_turn_content_blocked(timeout_ms=0)
                if not (saw_text or saw_block):
                    Logger.warning(
                        "Google AI Studio CAARS: savior prelude finished without obvious "
                        "assistant text or a blocked-turn marker. Trying the edit step anyway."
                    )

            replacement_message = str(
                effective_settings.get("anti_censorship_replacement_message") or "."
            )
            Logger.info("Google AI Studio CAARS: replacing savior assistant turn...")
            if not await self._replace_latest_assistant_message(replacement_message):
                return (
                    "Google AI Studio CAARS could not replace the savior assistant turn "
                    "before sending the main continue nudge."
                )

            Logger.info("Google AI Studio CAARS: switching back to the main model...")
            await self._apply_request_controls(effective_settings)
            await self._ui_settle_pause(0.25)
            return None

        async def _send_continue_nudge_attempt(
            continue_nudge: str,
            *,
            log_label: str = "Google AI Studio Anti-Censorship",
        ) -> str | None:
            _reset_attempt_state()
            await self.enter_message(continue_nudge)
            await self._ui_settle_pause(0.1)
            completion_armed.set()
            if log_label == "Google AI Studio CAARS":
                Logger.info("Google AI Studio CAARS: sending main continue nudge...")
            else:
                Logger.info("Google AI Studio: sending anti-censorship continue nudge...")
            await self.send_message(completion_started=completion_started)
            if await _wait_for_attempt_start(log_label):
                return None
            return (
                f"{log_label} failed because the continue nudge did not start a "
                "completion request."
            )

        def _new_stream_state(status_code: int = 200) -> Dict[str, Any]:
            return {
                "parser": _AiStudioJsonEventStreamParser(),
                "response_status": int(status_code or 200),
                "aborted": False,
                "encountered_error": False,
                "emitted_text": False,
                "hard_censorship_hint": False,
                "rate_limit_hint": False,
            }

        async def _process_attempt_stream_event(
            stream_state: Dict[str, Any],
            parsed_event: Any,
        ) -> None:
            stream_state["hard_censorship_hint"] = (
                bool(stream_state.get("hard_censorship_hint"))
                or self._event_looks_hard_censored(parsed_event)
            )
            stream_state["rate_limit_hint"] = (
                bool(stream_state.get("rate_limit_hint"))
                or self._event_looks_rate_limited(parsed_event)
            )
            emitted = await self._process_stream_event(
                parsed_event,
                response_queue,
                send_deepthink=bool(effective_settings["send_deepthink"]),
            )
            stream_state["emitted_text"] = bool(stream_state.get("emitted_text")) or emitted

        async def _feed_attempt_stream_chunk(
            stream_state: Dict[str, Any],
            chunk: bytes,
        ) -> None:
            if not chunk or bool(stream_state.get("encountered_error")):
                return

            parser = stream_state.get("parser")
            if not isinstance(parser, _AiStudioJsonEventStreamParser):
                return

            try:
                parsed_events = parser.feed(chunk)
            except Exception as e:
                stream_state["encountered_error"] = True
                Logger.error(f"Google AI Studio: failed to parse response stream: {e}")
                await response_queue.put({"error": str(e)})
                return

            for parsed_event in parsed_events:
                try:
                    await _process_attempt_stream_event(stream_state, parsed_event)
                except Exception as e:
                    stream_state["encountered_error"] = True
                    Logger.error(f"Google AI Studio: failed to process response stream: {e}")
                    await response_queue.put({"error": str(e)})
                    return
                if bool(stream_state.get("encountered_error")):
                    return

        async def _finalize_attempt_stream_state(
            stream_state: Dict[str, Any],
            *,
            flush_parser: bool = True,
        ) -> bool:
            nonlocal current_attempt_meta

            if (
                flush_parser
                and not bool(stream_state.get("aborted"))
                and not bool(stream_state.get("encountered_error"))
            ):
                parser = stream_state.get("parser")
                if isinstance(parser, _AiStudioJsonEventStreamParser):
                    try:
                        parsed_events = parser.finish()
                    except Exception as e:
                        stream_state["encountered_error"] = True
                        Logger.error(f"Google AI Studio: failed to parse response stream: {e}")
                        await response_queue.put({"error": str(e)})
                    else:
                        for parsed_event in parsed_events:
                            try:
                                await _process_attempt_stream_event(stream_state, parsed_event)
                            except Exception as e:
                                stream_state["encountered_error"] = True
                                Logger.error(f"Google AI Studio: failed to process response stream: {e}")
                                await response_queue.put({"error": str(e)})
                                break
                            if bool(stream_state.get("encountered_error")):
                                break

            if self.thinking_active:
                if bool(effective_settings["send_deepthink"]):
                    await self._enqueue_openai_delta(response_queue, "</think>")
                self.thinking_active = False

            response_status = int(stream_state.get("response_status") or 0)
            stream_state["rate_limit_hint"] = bool(
                stream_state.get("rate_limit_hint") or response_status == 429
            )
            emitted_text = bool(stream_state.get("emitted_text"))
            encountered_error = bool(stream_state.get("encountered_error"))
            aborted = bool(stream_state.get("aborted"))
            no_text_detected = (not emitted_text) and (not encountered_error) and (not aborted)

            if no_text_detected:
                if bool(stream_state.get("rate_limit_hint")):
                    message_text = self._build_rate_limit_error_message(response_status)
                    Logger.warning(message_text)
                    await response_queue.put({"error": message_text})
                    encountered_error = True
                    stream_state["encountered_error"] = True
                elif anti_censorship_enabled:
                    Logger.warning(
                        "Google AI Studio returned no assistant text, but anti-censorship is enabled. "
                        "Deferring the final decision until blocked-turn checks finish."
                    )
                else:
                    message_text = (
                        "Google AI Studio returned no assistant text. "
                        "The request may have been submitted before the UI fully settled."
                    )
                    Logger.warning(message_text)
                    await response_queue.put({"error": message_text})
                    encountered_error = True
                    stream_state["encountered_error"] = True

            current_attempt_meta = {
                "hard_censorship_hint": bool(stream_state.get("hard_censorship_hint")),
                "rate_limit_hint": bool(stream_state.get("rate_limit_hint")),
                "no_text_detected": bool(no_text_detected),
                "response_status": int(response_status),
                "encountered_error": bool(encountered_error),
                "emitted_text": bool(emitted_text),
                "aborted": bool(aborted),
            }

            if emitted_text and (not encountered_error) and (not aborted) and (not self.abort_requested):
                await self._enqueue_openai_delta(response_queue, "", finish_reason="stop")

            return bool(encountered_error)

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"Google AI Studio: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"Google AI Studio: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        def _request_aborted() -> bool:
            return bool(self.abort_requested or (abort_event and abort_event.is_set()))

        async def _finish_cdp_stream(
            request_id: str,
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            nonlocal cdp_stream_finished, cdp_active_request_id, cdp_stream_state
            if request_id != cdp_active_request_id or cdp_stream_finished:
                return

            cdp_stream_finished = True
            if cdp_stream_state is None:
                meta = cdp_pending_response_meta.get(request_id) or {}
                cdp_stream_state = _new_stream_state(int(meta.get("status") or 200))
            cdp_stream_state["aborted"] = bool(cdp_stream_state.get("aborted")) or aborted
            cdp_stream_state["encountered_error"] = (
                bool(cdp_stream_state.get("encountered_error")) or encountered_error
            )

            await _finalize_attempt_stream_state(
                cdp_stream_state,
                flush_parser=not bool(cdp_stream_state.get("aborted")),
            )
            await response_queue.put(None)
            cdp_request_methods.pop(request_id, None)
            cdp_pending_response_meta.pop(request_id, None)
            cdp_active_request_id = None
            cdp_stream_state = None

            if (
                not aborted
                and not encountered_error
                and not bool(current_attempt_meta.get("encountered_error"))
                and not _request_aborted()
            ):
                Logger.success("Google AI Studio CDP stream completed.")

        async def _feed_cdp_stream_chunk(request_id: str, data: bytes) -> None:
            nonlocal cdp_had_data, cdp_stream_state
            if request_id != cdp_active_request_id or not data or cdp_stream_finished:
                return

            if cdp_stream_state is None:
                meta = cdp_pending_response_meta.get(request_id) or {}
                cdp_stream_state = _new_stream_state(int(meta.get("status") or 200))

            cdp_had_data = True
            if _request_aborted():
                await _finish_cdp_stream(request_id, aborted=True)
                return

            await _feed_attempt_stream_chunk(cdp_stream_state, data)
            if bool(cdp_stream_state.get("encountered_error")):
                await _finish_cdp_stream(request_id, encountered_error=True)
                return

            if _request_aborted():
                await _finish_cdp_stream(request_id, aborted=True)

        async def _feed_base64_cdp_stream_chunk(request_id: str, encoded_data: Any) -> None:
            if not encoded_data:
                return
            encoded_text = str(encoded_data)
            try:
                data = base64.b64decode(encoded_text, validate=True)
            except Exception:
                data = encoded_text.encode("utf-8", errors="ignore")
            await _feed_cdp_stream_chunk(request_id, data)

        async def _start_cdp_stream(request_id: str, meta: Dict[str, Any]) -> None:
            nonlocal cdp_stream_started, cdp_stream_state
            if (
                request_id != cdp_active_request_id
                or not cdp_session
                or cdp_stream_started
                or cdp_stream_finished
            ):
                return

            cdp_stream_started = True
            cdp_stream_state = _new_stream_state(int(meta.get("status") or 200))
            url = str(meta.get("url") or "")
            Logger.info("Teeing Google AI Studio API response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message_text = f"Google AI Studio CDP response streaming failed: {exc}"
                Logger.error(message_text)
                await response_queue.put({"error": message_text})
                await _finish_cdp_stream(request_id, encountered_error=True)
                return

            if isinstance(result, dict):
                await _feed_base64_cdp_stream_chunk(request_id, result.get("bufferedData"))

        async def _handle_cdp_request_will_be_sent(params: Any) -> None:
            nonlocal completion_claimed, cdp_active_request_id
            nonlocal cdp_stream_started, cdp_stream_finished, cdp_had_data, cdp_stream_state
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            request = params.get("request")
            if not request_id or not isinstance(request, dict):
                return

            method = str(request.get("method") or "").upper()
            cdp_request_methods[request_id] = method
            url = str(request.get("url") or "")
            if method != "POST" or not self._is_generate_content_url(url):
                return
            if not completion_armed.is_set():
                return

            async with completion_claim_lock:
                if completion_claimed:
                    return
                completion_claimed = True
                cdp_active_request_id = request_id
                cdp_stream_started = False
                cdp_stream_finished = False
                cdp_had_data = False
                cdp_stream_state = None
                completion_started.set()

            Logger.info("Observing Google AI Studio API request via CDP...")
            Logger.debug(f"Observed request to: {url}")
            pending_meta = cdp_pending_response_meta.get(request_id)
            if pending_meta:
                await _start_cdp_stream(request_id, pending_meta)

        async def _handle_cdp_response_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            response = params.get("response")
            if not request_id or not isinstance(response, dict):
                return
            url = str(response.get("url") or "")
            if not self._is_generate_content_url(url):
                return
            method = cdp_request_methods.get(request_id, "").upper()
            if method and method != "POST":
                return
            try:
                status = int(response.get("status") or 200)
            except Exception:
                status = 200
            meta = {"url": url, "status": status}
            cdp_pending_response_meta[request_id] = meta
            if request_id != cdp_active_request_id:
                return
            await _start_cdp_stream(request_id, meta)

        async def _handle_cdp_data_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            if request_id == cdp_active_request_id:
                await _feed_base64_cdp_stream_chunk(request_id, params.get("data"))

        async def _handle_cdp_loading_finished(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            if request_id == cdp_active_request_id:
                await _finish_cdp_stream(request_id)
            else:
                cdp_request_methods.pop(request_id, None)
                cdp_pending_response_meta.pop(request_id, None)

        async def _handle_cdp_loading_failed(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            if request_id != cdp_active_request_id:
                cdp_request_methods.pop(request_id, None)
                cdp_pending_response_meta.pop(request_id, None)
                return

            if _request_aborted():
                await _finish_cdp_stream(request_id, aborted=True)
                return

            error_text = str(params.get("errorText") or "network loading failed").strip()
            if "ERR_ABORTED" in error_text.upper() and cdp_had_data:
                Logger.debug(
                    "Google AI Studio CDP stream ended with net::ERR_ABORTED after data arrived; "
                    "treating it as complete."
                )
                await _finish_cdp_stream(request_id)
                return

            message_text = f"Google AI Studio CDP stream failed: {error_text}"
            Logger.error(message_text)
            await response_queue.put({"error": message_text})
            await _finish_cdp_stream(request_id, encountered_error=True)

        def _on_cdp_request_will_be_sent(params: Any) -> None:
            _schedule_cdp_task(_handle_cdp_request_will_be_sent(params), "requestWillBeSent")

        def _on_cdp_response_received(params: Any) -> None:
            _schedule_cdp_task(_handle_cdp_response_received(params), "responseReceived")

        def _on_cdp_data_received(params: Any) -> None:
            _schedule_cdp_task(_handle_cdp_data_received(params), "dataReceived")

        def _on_cdp_loading_finished(params: Any) -> None:
            _schedule_cdp_task(_handle_cdp_loading_finished(params), "loadingFinished")

        def _on_cdp_loading_failed(params: Any) -> None:
            _schedule_cdp_task(_handle_cdp_loading_failed(params), "loadingFailed")

        async def handle_route(route):
            nonlocal completion_claimed, current_attempt_meta
            request = route.request

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

            Logger.info("Intercepting Google AI Studio API request...")
            Logger.debug(f"Intercepted request to: {request.url}")

            headers = await request.all_headers()
            headers.pop("content-length", None)
            headers.pop("host", None)
            # Prefer an identity-encoded response so incremental JSON events can
            # reach the parser as they arrive instead of being hidden behind
            # response compression buffers.
            headers["accept-encoding"] = "identity"

            cookies = await self.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            request_body = self._extract_request_body_bytes(request)

            stream_state = _new_stream_state()
            response_headers: Dict[str, str] = {}
            full_response_body = bytearray()
            response_status = 200
            aborted = False

            try:
                client = await self._get_http_client()
                request_kwargs: Dict[str, Any] = {
                    "headers": headers,
                    "cookies": cookie_dict,
                    "timeout": 120.0,
                }
                if request_body is not None:
                    request_kwargs["content"] = request_body

                async with client.stream(request.method, request.url, **request_kwargs) as response:
                    response_status = int(response.status_code)
                    stream_state["response_status"] = response_status
                    for k, v in response.headers.items():
                        response_headers[k] = v
                    response_headers.pop("content-encoding", None)
                    response_headers.pop("content-length", None)
                    response_headers.pop("transfer-encoding", None)

                    async for chunk in response.aiter_bytes():
                        if self.abort_requested or (abort_event and abort_event.is_set()):
                            Logger.debug("Abort detected during Google AI Studio streaming, stopping...")
                            aborted = True
                            stream_state["aborted"] = True
                            break

                        full_response_body.extend(chunk)
                        await _feed_attempt_stream_chunk(stream_state, chunk)
                        if bool(stream_state.get("encountered_error")):
                            break
            except httpx.ReadError as e:
                if not aborted and not self.abort_requested:
                    stream_state["encountered_error"] = True
                    Logger.error(f"Google AI Studio: read error during intercepted request: {e}")
                    await response_queue.put({"error": str(e)})
            except Exception as e:
                if not aborted and not self.abort_requested:
                    stream_state["encountered_error"] = True
                    Logger.error(f"Google AI Studio: error during intercepted request: {e}")
                    await response_queue.put({"error": str(e)})

            await _finalize_attempt_stream_state(stream_state, flush_parser=not aborted)
            encountered_error = bool(stream_state.get("encountered_error"))

            if "content-type" not in response_headers:
                response_headers["content-type"] = "application/json+protobuf; charset=UTF-8"

            fulfill_body = bytes(full_response_body) if full_response_body else b"[[null]]"
            try:
                await route.fulfill(body=fulfill_body, status=response_status, headers=response_headers)
            except Exception as e:
                if "already handled" in str(e).lower():
                    Logger.debug(f"Google AI Studio: route was already handled before fulfill completed: {e}")
                else:
                    Logger.error(f"Google AI Studio: error fulfilling route: {e}")

            await response_queue.put(None)
            if not encountered_error and not aborted and not self.abort_requested:
                Logger.success("Google AI Studio response streaming completed.")

        try:
            if use_cdp_teeing:
                if not self.context or not self.page:
                    message_text = "Google AI Studio CDP setup failed: browser context is not available."
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
                try:
                    cdp_session = await self.context.new_cdp_session(self.page)
                    await cdp_session.send("Network.enable", {})
                    cdp_session.on("Network.requestWillBeSent", _on_cdp_request_will_be_sent)
                    cdp_session.on("Network.responseReceived", _on_cdp_response_received)
                    cdp_session.on("Network.dataReceived", _on_cdp_data_received)
                    cdp_session.on("Network.loadingFinished", _on_cdp_loading_finished)
                    cdp_session.on("Network.loadingFailed", _on_cdp_loading_failed)
                    cdp_listeners_registered = True
                    Logger.info("Google AI Studio Request Capture Mode: CDP Teeing.")
                except Exception as exc:
                    message_text = f"Google AI Studio CDP setup failed: {exc}"
                    Logger.error(message_text)
                    yield f"data: {json.dumps({'error': message_text})}\n\n"
                    return
            else:
                await self.page.route(self.GENERATE_ROUTE_GLOB, handle_route)
                route_handlers_registered = True
                Logger.info("Google AI Studio Request Capture Mode: Replay.")

            formatted_message, system_prompt_text = self._prepare_prompt_payload(message_for_formatting)
            aistudio_extra_prompt_texts: Dict[str, str] = {}
            text_file_message = str(effective_settings.get("text_file_message") or "")
            if text_file_message.strip():
                aistudio_extra_prompt_texts["text_file_message"] = text_file_message
            self._capture_diagnostics_prompt_snapshot(
                formatted_message,
                system_prompt_text=system_prompt_text,
                extra_prompt_texts=aistudio_extra_prompt_texts or None,
                metadata={
                    "model": self.current_model or "aistudio-auto",
                    "ui_model": str(effective_settings.get("model_label") or ""),
                    "deepthink_enabled": bool(effective_settings.get("deepthink_enabled")),
                    "thinking_level": str(effective_settings.get("thinking_level") or ""),
                    "send_deepthink": bool(effective_settings.get("send_deepthink")),
                    "search_enabled": bool(effective_settings.get("search_enabled")),
                    "url_context_enabled": bool(effective_settings.get("url_context_enabled")),
                    "use_system_prompt_field": bool(effective_settings.get("use_system_prompt_field")),
                    "send_as_text_file": bool(effective_settings.get("send_as_text_file")),
                    "anti_censorship": bool(effective_settings.get("anti_censorship")),
                    "caars_enabled": bool(effective_settings.get("caars_enabled")),
                    "caars_savior_model": str(effective_settings.get("caars_savior_model") or ""),
                },
            )
            await self._ensure_safety_filters_initialized()
            clean_regeneration_requested = bool(
                self.config_manager.get_setting("aistudio_behavior", "clean_regeneration")
            )
            if clean_regeneration_requested and preflight_next_chat:
                Logger.warning(
                    "Google AI Studio Preflight conflicts with Reuse Matching Chat. "
                    "Preflight is disabled for this request."
                )
                preflight_next_chat = False
            if not preflight_next_chat:
                self._clear_preflight_chat_state()

            clean_regeneration = clean_regeneration_requested and not caars_enabled
            clean_regen_state = self._build_clean_regeneration_state(
                effective_settings,
                system_prompt_text=system_prompt_text,
            )

            if caars_enabled:
                setup_error = await _run_caars_savior_prelude(
                    formatted_message=formatted_message,
                    system_prompt_text=system_prompt_text,
                )
                if setup_error:
                    yield f"data: {json.dumps({'error': setup_error})}\n\n"
                    return

                continue_nudge = str(
                    effective_settings.get("anti_censorship_continue_nudge") or "Continue."
                )
                setup_error = await _send_continue_nudge_attempt(
                    continue_nudge,
                    log_label="Google AI Studio CAARS",
                )
                if setup_error:
                    yield f"data: {json.dumps({'error': setup_error})}\n\n"
                    return
            else:
                regenerated = await _attempt_clean_regeneration(
                    clean_regeneration=clean_regeneration,
                    formatted_message=formatted_message,
                    clean_regen_state=clean_regen_state,
                )
                if regenerated:
                    setup_error = None
                else:
                    setup_error = await _start_new_chat_attempt(
                        formatted_message=formatted_message,
                        system_prompt_text=system_prompt_text,
                    )

                if setup_error:
                    yield f"data: {json.dumps({'error': setup_error})}\n\n"
                    return

            if not anti_censorship_enabled:
                if clean_regeneration:
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(clean_regen_state)

                stream_error_seen = False
                async for item in self._iterate_response_queue(
                    response_queue,
                    abort_event=abort_event,
                    first_chunk_timeout_s=self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S,
                    idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                ):
                    if isinstance(item, dict) and "error" in item:
                        stream_error_seen = True
                        yield f"data: {json.dumps(item)}\n\n"
                        break
                    yield item
                if (
                    preflight_next_chat
                    and not stream_error_seen
                    and not self.abort_requested
                    and not (abort_event and abort_event.is_set())
                ):
                    preflight_settings_for_next_chat = dict(effective_settings)
                    preflight_system_prompt_for_next_chat = str(system_prompt_text or "")
                return

            terminal_stop_chunk: str | None = None
            final_error_message: str | None = None
            hard_censorship_seen = False
            continue_nudges_sent = 0

            while True:
                attempt_items: List[Any] = []
                attempt_buffered_chunks: List[str] = []
                attempt_thinking_active = False
                attempt_stream_released = False
                async for item in self._iterate_response_queue(
                    response_queue,
                    abort_event=abort_event,
                    first_chunk_timeout_s=self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S,
                    idle_timeout_s=self.INTERCEPT_IDLE_TIMEOUT_S,
                ):
                    attempt_items.append(item)
                    if isinstance(item, str) and (not _is_terminal_stop_chunk(item)):
                        content = _chunk_delta_content(item)
                        if content == "<think>":
                            attempt_thinking_active = True
                        elif content == "</think>":
                            attempt_thinking_active = False

                        if (not attempt_stream_released) and content:
                            blocked_now = await self._is_latest_assistant_turn_content_blocked(
                                timeout_ms=75
                            )
                            if not blocked_now:
                                attempt_stream_released = True

                        if attempt_stream_released:
                            if attempt_buffered_chunks:
                                for buffered_chunk in attempt_buffered_chunks:
                                    yield buffered_chunk
                                attempt_buffered_chunks.clear()
                            yield item
                        else:
                            attempt_buffered_chunks.append(item)
                    if _stream_item_error_message(item):
                        break

                if self.abort_requested or (abort_event and abort_event.is_set()):
                    final_error_message = None
                    terminal_stop_chunk = None
                    break

                attempt_error = _attempt_error_message(attempt_items)
                hard_censored = False
                if not attempt_stream_released:
                    hard_censored = await self._is_latest_assistant_turn_content_blocked(
                        timeout_ms=750
                    )
                    if (not hard_censored) and bool(current_attempt_meta.get("hard_censorship_hint")):
                        hard_censored = True

                if hard_censored:
                    hard_censorship_seen = True
                    clear_clean_regeneration_cache(
                        self.cache_manager,
                        self.clean_regen_message_cache_key,
                        self.clean_regen_state_cache_key,
                    )

                    if continue_nudges_sent >= self.MAX_ANTI_CENSORSHIP_NUDGES:
                        final_error_message = (
                            "Google AI Studio hard-censored the response, and the anti-censorship "
                            "workaround could not recover it after 3 continue nudges."
                        )
                        terminal_stop_chunk = None
                        break

                    replacement_message = str(
                        effective_settings.get("anti_censorship_replacement_message") or "."
                    )
                    continue_nudge = str(
                        effective_settings.get("anti_censorship_continue_nudge") or "Continue."
                    )
                    Logger.warning(
                        "Google AI Studio: hard censorship detected. Replacing the blocked "
                        "assistant turn and sending a continue nudge..."
                    )
                    if not await self._replace_latest_assistant_message(replacement_message):
                        final_error_message = (
                            "Google AI Studio hard-censored the response, and IntenseRP could not "
                            "replace the blocked assistant message for retry."
                        )
                        terminal_stop_chunk = None
                        break

                    continue_nudges_sent += 1
                    retry_error = await _send_continue_nudge_attempt(continue_nudge)
                    if retry_error:
                        final_error_message = retry_error
                        terminal_stop_chunk = None
                        break
                    continue

                terminal_stop_chunk = _attempt_terminal_stop_chunk(attempt_items)
                final_error_message = attempt_error
                if (not final_error_message) and (
                    bool(current_attempt_meta.get("no_text_detected")) or (not attempt_stream_released)
                ):
                    final_error_message = (
                        "Google AI Studio returned no assistant text. "
                        "The request may have been submitted before the UI fully settled."
                    )
                if final_error_message or (not attempt_stream_released):
                    terminal_stop_chunk = None
                break

            if clean_regeneration:
                if (not hard_censorship_seen) and (not final_error_message):
                    self.cache_manager.write_cache(self.clean_regen_message_cache_key, formatted_message)
                    self._write_clean_regeneration_state(clean_regen_state)
                else:
                    clear_clean_regeneration_cache(
                        self.cache_manager,
                        self.clean_regen_message_cache_key,
                        self.clean_regen_state_cache_key,
                    )

            if terminal_stop_chunk and (not final_error_message):
                yield terminal_stop_chunk

            if final_error_message:
                yield f"data: {json.dumps({'error': final_error_message})}\n\n"
            elif (
                preflight_next_chat
                and not self.abort_requested
                and not (abort_event and abort_event.is_set())
            ):
                preflight_settings_for_next_chat = (
                    _build_caars_savior_settings() if caars_enabled else dict(effective_settings)
                )
                preflight_system_prompt_for_next_chat = str(system_prompt_text or "")
        finally:
            self.current_abort_event = None
            self.abort_requested = False
            self.current_model = None
            self.current_send_deepthink = None
            self.thinking_active = False
            if route_handlers_registered:
                try:
                    await self.page.unroute(self.GENERATE_ROUTE_GLOB, handle_route)
                except Exception:
                    try:
                        await self.page.unroute(self.GENERATE_ROUTE_GLOB)
                    except Exception:
                        pass
            if cdp_session and cdp_listeners_registered:
                for event_name, listener in (
                    ("Network.requestWillBeSent", _on_cdp_request_will_be_sent),
                    ("Network.responseReceived", _on_cdp_response_received),
                    ("Network.dataReceived", _on_cdp_data_received),
                    ("Network.loadingFinished", _on_cdp_loading_finished),
                    ("Network.loadingFailed", _on_cdp_loading_failed),
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
                    Logger.debug(f"Google AI Studio: CDP detach failed: {exc}")
            if preflight_settings_for_next_chat is not None:
                self._schedule_preflight_next_chat(
                    preflight_settings_for_next_chat,
                    preflight_system_prompt_for_next_chat,
                )
