import base64
import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional, Union

from drivers.base_driver import BaseDriver
from drivers.providers import DriverProvider
from drivers.shared_utils import (
    COMMON_REQUEST_MACRO_ACTIONS,
    IncrementalTextAccumulator,
    build_prompt_text_file_payload,
    compute_missing_suffix,
    extract_macro_overrides,
    format_request_messages,
    make_openai_delta_sse,
    resolve_rendered_injection,
    split_leading_system_messages,
    strip_macros_from_messages,
)
from utils.logger import Logger
from utils.model_ids import (
    MODE_CHAT,
    MODE_REASONER,
    resolve_behavior_mode,
    resolve_real_model_label_from_model_id,
)


PERPLEXITY_MODEL_OPTIONS: list[str] = [
    "Best (Auto)",
    "Sonar 2",
    "GPT-5.4",
    "GPT-5.5",
    "Gemini 3.1 Pro",
    "Claude Sonnet 4.6",
    "Claude Opus 4.7",
    "Kimi K2.6",
    "Nemotron 3 Super",
]


class _PerplexityMarkdownBlockState:
    """Track one Perplexity markdown answer block as JSON patches arrive."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.answer_text = ""

    def apply_block(self, block: dict[str, Any]) -> None:
        markdown_block = block.get("markdown_block")
        if isinstance(markdown_block, dict):
            self._apply_markdown_block(markdown_block)

        diff_block = block.get("diff_block")
        if not isinstance(diff_block, dict):
            return

        patches = diff_block.get("patches")
        if not isinstance(patches, list):
            return

        for patch in patches:
            if isinstance(patch, dict):
                self._apply_patch(patch)

    def text(self) -> str:
        chunk_text = "".join(self.chunks)
        if chunk_text and len(chunk_text) >= len(self.answer_text):
            return chunk_text
        return self.answer_text or chunk_text

    def _apply_markdown_block(self, markdown_block: dict[str, Any]) -> None:
        answer = markdown_block.get("answer")
        if isinstance(answer, str):
            self.answer_text = answer

        chunks = markdown_block.get("chunks")
        if isinstance(chunks, list):
            self.chunks = [str(item or "") for item in chunks]

    def _apply_patch(self, patch: dict[str, Any]) -> None:
        op = str(patch.get("op") or "").strip().lower()
        path = str(patch.get("path") or "").strip()
        value = patch.get("value")

        if path == "":
            if op in {"replace", "add"} and isinstance(value, dict):
                self._apply_markdown_block(value)
            return

        if path == "/answer":
            if op in {"replace", "add"} and isinstance(value, str):
                self.answer_text = value
            elif op == "remove":
                self.answer_text = ""
            return

        if not path.startswith("/chunks/"):
            return

        index_text = path.rsplit("/", 1)[-1]
        if index_text == "-":
            index = len(self.chunks)
        else:
            try:
                index = int(index_text)
            except Exception:
                return
        if index < 0:
            return

        while len(self.chunks) <= index:
            self.chunks.append("")

        if op in {"replace", "add"}:
            self.chunks[index] = str(value or "")
        elif op == "remove":
            self.chunks[index] = ""


class _PerplexityAnswerStreamParser:
    """
    Parse Perplexity's SSE stream into assistant-answer deltas.

    Perplexity sends JSON Patch-style updates inside `blocks`. The useful
    answer can appear as a combined `ask_text` block or as numbered markdown
    blocks such as `ask_text_0_markdown`, `ask_text_1_markdown`, etc.
    """

    FALLBACK_USAGE = "ask_text"
    ANSWER_USAGE_RE = re.compile(r"^ask_text(?:_(\d+)_markdown)?$")

    def __init__(self) -> None:
        self._line_buffer = bytearray()
        self._event_name = ""
        self._blocks_by_usage: dict[str, _PerplexityMarkdownBlockState] = {}
        self._emitted_answer = IncrementalTextAccumulator()
        self.finish_emitted = False
        self.emitted_text = False
        self.provider_final_seen = False
        self.event_count = 0

    def feed(self, chunk: bytes) -> list[tuple[str, str | None]]:
        if not chunk:
            return []

        self._line_buffer.extend(chunk)
        emitted: list[tuple[str, str | None]] = []

        while True:
            newline = self._line_buffer.find(b"\n")
            if newline == -1:
                break
            raw_line = bytes(self._line_buffer[:newline])
            del self._line_buffer[: newline + 1]
            emitted.extend(self._process_line(raw_line.decode("utf-8", errors="ignore")))

        return emitted

    def finish(self) -> list[tuple[str, str | None]]:
        emitted: list[tuple[str, str | None]] = []
        if self._line_buffer.strip():
            emitted.extend(
                self._process_line(self._line_buffer.decode("utf-8", errors="ignore"))
            )
        self._line_buffer.clear()
        if self.emitted_text and not self.finish_emitted:
            emitted.append(("", "stop"))
            self.finish_emitted = True
        return emitted

    def _process_line(self, line: str) -> list[tuple[str, str | None]]:
        line = str(line or "").strip()
        if not line:
            return []

        if line.startswith("event:"):
            self._event_name = line[len("event:") :].strip()
            if self._event_name in {"final_sse_message", "end_of_stream"}:
                self.provider_final_seen = True
            return []

        if not line.startswith("data:"):
            return []

        data = line[len("data:") :].strip()
        if not data:
            return []
        if data == "[DONE]":
            self.provider_final_seen = True
            return []

        try:
            payload = json.loads(data)
        except Exception:
            return []

        self.event_count += 1
        if not isinstance(payload, dict):
            return []

        emitted: list[tuple[str, str | None]] = []
        for usage, block in self._iter_answer_blocks(payload):
            self._state_for_usage(usage).apply_block(block)

        current_answer = self._current_answer_text()
        missing = self._emitted_answer.missing_suffix(current_answer)
        if missing:
            self._emitted_answer.append(missing)
            self.emitted_text = True
            emitted.append((missing, None))

        if bool(payload.get("final_sse_message")) or str(payload.get("status") or "").upper() == "COMPLETED":
            self.provider_final_seen = True

        return emitted

    def _finish_delta(self) -> list[tuple[str, str | None]]:
        if self.finish_emitted:
            return []
        self.finish_emitted = True
        if not self.emitted_text:
            return []
        return [("", "stop")]

    def _iter_answer_blocks(self, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        blocks = payload.get("blocks")
        if not isinstance(blocks, list):
            return []

        answer_blocks: list[tuple[str, dict[str, Any]]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            usage = str(block.get("intended_usage") or "").strip()
            if self.ANSWER_USAGE_RE.match(usage):
                answer_blocks.append((usage, block))

        return answer_blocks

    def _state_for_usage(self, usage: str) -> _PerplexityMarkdownBlockState:
        state = self._blocks_by_usage.get(usage)
        if state is None:
            state = _PerplexityMarkdownBlockState()
            self._blocks_by_usage[usage] = state
        return state

    def _current_answer_text(self) -> str:
        fallback = self._blocks_by_usage.get(self.FALLBACK_USAGE)
        fallback_text = fallback.text() if fallback else ""
        if fallback_text:
            return fallback_text

        numbered: list[tuple[int, str]] = []
        for usage, state in self._blocks_by_usage.items():
            match = self.ANSWER_USAGE_RE.match(usage)
            if not match or match.group(1) is None:
                continue
            text = state.text()
            if text:
                numbered.append((int(match.group(1)), text))

        if numbered:
            return "\n\n".join(text for _index, text in sorted(numbered))

        return ""

    @staticmethod
    def _compute_missing_suffix(emitted: str, candidate: str) -> str:
        return compute_missing_suffix(emitted, candidate)


class PerplexityDriver(BaseDriver):
    BASE_URL = "https://www.perplexity.ai/"
    AUTH_URL_PREFIX = "https://www.perplexity.ai/auth"
    ASK_ROUTE_FRAGMENT = "/rest/sse/perplexity_ask"
    SESSION_URL = "https://www.perplexity.ai/api/auth/session"
    SESSION_URL_FRAGMENT = "/api/auth/session"
    SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"

    CHAT_EDITOR_SELECTOR = "div[contenteditable='true']"
    SUBMIT_BUTTON_SELECTOR = "button[type='button'][aria-label='Submit']"
    FILE_INPUT_SELECTOR = "input[type='file']"
    UPLOAD_SPINNER_SELECTOR = "svg.inline-flex.fill-current.shrink-0.animate-spin"
    SPACES_URL = "https://www.perplexity.ai/spaces"
    SPACE_TITLE = "IntenseRP Next"
    SPACE_DESCRIPTION = (
        "This space is managed by IntenseRP Next, please don't touch or delete it "
        "if you plan to use IntenseRP Next with Perplexity. It will be "
        "auto-recreated automatically when you use IRP Next if you delete it, though."
    )
    SPACE_INSTRUCTIONS_LIMIT = 8000
    SPACE_INSTRUCTIONS_TEXTAREA_SELECTOR = (
        "textarea[aria-label='Input for editing Space answer instructions']"
    )
    SIGN_IN_LABEL_XPATH = (
        "xpath=//div[normalize-space(.)='Sign In' and "
        "contains(concat(' ', normalize-space(@class), ' '), "
        "' font-sans text-sm text-super leading-tight ')]"
    )
    EMAIL_LOGIN_INPUT_SELECTOR = "input[placeholder='Enter your email'], input[name='email']"
    VERIFY_REQUEST_HEADING_SELECTOR = "xpath=//h2[normalize-space(.)='Check your email']"
    VERIFY_CONTINUE_BUTTON_SELECTOR = "xpath=//button[.//span[normalize-space(.)='Continue']]"

    INTERCEPT_FIRST_CHUNK_TIMEOUT_S = 45.0
    INTERCEPT_IDLE_TIMEOUT_S = 75.0
    PASSIVE_RESPONSE_BODY_TIMEOUT_S = 600.0
    MODEL_SWITCH_TIERS = {"pro", "max"}
    MAX_ONLY_MODELS = {"GPT-5.5", "Claude Opus 4.7"}
    FORCED_THINKING_MODELS = {"Gemini 3.1 Pro", "Nemotron 3 Super"}
    REQUEST_MODE_LABELS = (
        "Search",
        "Learn step by step",
        "Deep research",
        "Model council",
    )

    def __init__(self, config_manager):
        super().__init__(config_manager=config_manager, provider=DriverProvider.PERPLEXITY)
        self.subscription_tier = "free"
        self.current_model: Optional[str] = None
        self.current_send_deepthink: Optional[bool] = None
        self._abort_ui_task: asyncio.Task | None = None
        self._space_id: Optional[str] = None
        self._last_space_instructions_text: Optional[str] = None

    def get_start_url(self) -> str:
        return self.BASE_URL

    def _spaces_enabled(self) -> bool:
        try:
            return bool(self.config_manager.get_setting("perplexity_behavior", "use_spaces"))
        except Exception:
            return False

    def _space_instruction_sync_enabled(self) -> bool:
        if not self._spaces_enabled():
            return False
        try:
            return bool(
                self.config_manager.get_setting(
                    "perplexity_behavior",
                    "paste_system_instructions_into_space",
                )
            )
        except Exception:
            return False

    async def before_initial_navigation(self) -> None:
        if not self.page:
            return

        def _on_response(response) -> None:
            try:
                url = str(getattr(response, "url", "") or "")
                method = str(getattr(getattr(response, "request", None), "method", "") or "")
            except Exception:
                return
            if self.SESSION_URL_FRAGMENT not in url or method.upper() not in {"", "GET"}:
                return
            try:
                asyncio.create_task(self._inspect_session_response(response))
            except Exception:
                return

        try:
            self.page.on("response", _on_response)
        except Exception:
            return

    async def after_start(self, status_callback=None) -> None:
        await self.check_ui_language(status_callback=status_callback)
        await self._dismiss_onboarding()
        try:
            await self._refresh_subscription_tier()
        except Exception:
            pass
        try:
            if self._spaces_enabled():
                if status_callback:
                    status_callback("Preparing Perplexity Space...")
                await self._ensure_space_ready()
            else:
                await self._wait_for_chat_ready(timeout_ms=60000)
        except Exception as exc:
            Logger.warning(f"Perplexity: chat editor was not ready after startup: {exc}")

    async def cleanup_background_tasks(self) -> None:
        await self._cancel_task(self._abort_ui_task, label="stopping Perplexity abort UI task")
        self._abort_ui_task = None

    def request_abort(self) -> None:
        super().request_abort()
        if not self.request_was_sent():
            return  # Nothing reached the site yet; preparation stops on its own.
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

    async def _inspect_session_response(self, response) -> None:
        try:
            if int(getattr(response, "status", 0) or 0) >= 400:
                return
        except Exception:
            pass
        try:
            text = await response.text()
            payload = json.loads(text)
        except Exception:
            return
        self._update_subscription_tier(payload)

    def _update_subscription_tier(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        user = payload.get("user")
        if not isinstance(user, dict):
            return
        tier = str(user.get("subscription_tier") or "").strip().lower()
        if tier:
            self.subscription_tier = tier
            Logger.debug(f"Perplexity: detected subscription tier '{tier}'.")

    async def _refresh_subscription_tier(self) -> None:
        available, payload = await self._read_session_payload(timeout_ms=15000)
        if not available:
            return
        self._update_subscription_tier(payload)

    async def _dismiss_onboarding(self) -> None:
        if not self.page:
            return
        try:
            if "/onboarding" in str(self.page.url or ""):
                close_btn = self.page.locator("button[aria-label='Close']")
                if await close_btn.count() > 0:
                    await close_btn.first.click(timeout=3000)
                    await asyncio.sleep(0.3)
        except Exception:
            pass

    def _sign_in_label_locator(self):
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        return self.page.locator(self.SIGN_IN_LABEL_XPATH)

    def _email_login_input_locator(self):
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        return self.page.locator(self.EMAIL_LOGIN_INPUT_SELECTOR)

    async def _read_session_payload(
        self, timeout_ms: int | None = 10000
    ) -> tuple[bool, Any]:
        result = await self._run_browser_request(
            method="GET",
            url=self.SESSION_URL,
            headers={"accept": "application/json"},
            timeout_ms=timeout_ms or 10000,
        )
        if not isinstance(result, dict):
            return False, None

        try:
            status = int(result.get("status") or 0)
        except Exception:
            status = 0

        if not bool(result.get("ok")):
            if status in {401, 403}:
                return True, {}
            Logger.debug(
                f"Perplexity: session check failed with status {status or 'unknown'}."
            )
            return False, None

        try:
            return True, json.loads(str(result.get("text") or ""))
        except Exception as exc:
            Logger.debug(f"Perplexity: could not parse session check response: {exc}")
            return False, None

    @staticmethod
    def _session_payload_is_authenticated(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        user = payload.get("user")
        return isinstance(user, dict) and bool(user)

    async def _read_session_auth_state(
        self, timeout_ms: int | None = 10000
    ) -> bool | None:
        available, payload = await self._read_session_payload(timeout_ms=timeout_ms)
        if not available:
            return None
        self._update_subscription_tier(payload)
        return self._session_payload_is_authenticated(payload)

    async def _has_session_cookie(self) -> bool:
        if not self.context:
            return False

        try:
            cookies = await self.context.cookies(self.BASE_URL)
        except TypeError:
            try:
                cookies = await self.context.cookies([self.BASE_URL])
            except Exception:
                cookies = []
        except Exception:
            cookies = []

        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name == self.SESSION_COOKIE_NAME and value:
                return True
        return False

    async def _is_logged_in(self) -> bool:
        if not await self._has_session_cookie():
            return False

        auth_state = await self._read_session_auth_state(timeout_ms=5000)
        if auth_state is not None:
            return auth_state

        Logger.debug(
            "Perplexity: could not verify the session endpoint; falling back to "
            "the session cookie."
        )
        return True

    async def _wait_for_sign_in_label(self, timeout_ms: int | None = 10000):
        start = time.monotonic()
        timeout_s = None if not timeout_ms else max(int(timeout_ms), 0) / 1000.0
        while True:
            labels = self._sign_in_label_locator()
            try:
                count = await labels.count()
            except Exception:
                count = 0
            for idx in range(count):
                candidate = labels.nth(idx)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    continue
            if timeout_s is not None and time.monotonic() - start >= timeout_s:
                raise TimeoutError("Timed out waiting for Perplexity Sign In label.")
            await asyncio.sleep(0.2)

    async def _click_locator_center(
        self,
        locator: Any,
        *,
        label: str,
        timeout_ms: int | None = 10000,
    ) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        try:
            await locator.scroll_into_view_if_needed(timeout=timeout_ms or 0)
        except Exception:
            pass

        box = await locator.bounding_box()
        if not box:
            raise RuntimeError(f"Perplexity {label} is visible but has no clickable bounds.")

        await self.page.mouse.click(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )

    async def _click_sign_in_label(self, timeout_ms: int | None = 10000) -> None:
        sign_in_label = await self._wait_for_sign_in_label(timeout_ms=timeout_ms)
        await self._click_locator_center(
            sign_in_label, label="Sign In label", timeout_ms=timeout_ms
        )

    async def _wait_for_email_login_input(self, timeout_ms: int | None = 15000):
        start = time.monotonic()
        timeout_s = None if not timeout_ms else max(int(timeout_ms), 0) / 1000.0
        while True:
            inputs = self._email_login_input_locator()
            try:
                count = await inputs.count()
            except Exception:
                count = 0
            for idx in range(count):
                candidate = inputs.nth(idx)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    continue
            if timeout_s is not None and time.monotonic() - start >= timeout_s:
                raise TimeoutError("Timed out waiting for Perplexity email login input.")
            await asyncio.sleep(0.2)

    async def _open_email_login_form(self, timeout_ms: int | None = 15000):
        try:
            return await self._wait_for_email_login_input(timeout_ms=3000)
        except Exception:
            pass

        try:
            await self._click_sign_in_label(timeout_ms=3000)
        except Exception as exc:
            Logger.debug(f"Perplexity: old Sign In entry point was not available: {exc}")

        return await self._wait_for_email_login_input(timeout_ms=timeout_ms)

    async def _wait_for_verify_request_page(self, timeout_ms: int | None = 15000) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        await self.page.locator(self.VERIFY_REQUEST_HEADING_SELECTOR).first.wait_for(
            state="visible",
            timeout=timeout_ms or 0,
        )

    async def _click_continue_with_email(self, timeout_ms: int | None = 10000) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        if await self._click_button_with_first_span_text(
            "Continue with email",
            timeout_ms=timeout_ms or 0,
        ):
            return
        raise TimeoutError("Timed out waiting for Perplexity Continue with email button.")

    @staticmethod
    def _normalize_email_code(code: Any) -> str:
        return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]

    async def _submit_email_verification_code(self, code: str) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        normalized_code = self._normalize_email_code(code)
        if len(normalized_code) != 6:
            raise ValueError("Perplexity verification code must be 6 digits.")

        first_digit_input = self.page.locator(
            "input[aria-label='Digit 1 of 6'], input[label='Digit 1 of 6']"
        ).first
        await first_digit_input.wait_for(state="visible", timeout=10000)
        await self._click_locator_center(
            first_digit_input,
            label="verification digit 1",
            timeout_ms=5000,
        )
        try:
            await first_digit_input.fill("", timeout=5000)
        except Exception:
            pass
        await self.page.keyboard.type(normalized_code, delay=40)

        try:
            await self._wait_until_logged_in(timeout_ms=12000)
            return
        except TimeoutError:
            pass

        continue_button = self.page.locator(self.VERIFY_CONTINUE_BUTTON_SELECTOR).first
        try:
            await continue_button.wait_for(state="visible", timeout=5000)
        except Exception:
            if await self._is_logged_in():
                return
            raise
        await self._click_locator_center(
            continue_button,
            label="verification Continue button",
            timeout_ms=5000,
        )

    async def _wait_for_code_prompt_or_login(self, timeout_ms: int | None = 120000) -> None:
        code_task = asyncio.create_task(
            self.request_user_text(
                "Perplexity Login",
                (
                    "Perplexity sent a 6-digit code to your email. Enter it here "
                    "to continue, or enter it directly in the browser window."
                ),
                label="6-digit code",
                placeholder="123456",
                max_length=6,
                min_length=6,
                digits_only=True,
                level="warning",
                force_notify=True,
            )
        )
        login_task = asyncio.create_task(self._wait_until_logged_in(timeout_ms=timeout_ms))

        try:
            done, _pending = await asyncio.wait(
                {code_task, login_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if login_task in done:
                await login_task
                if not code_task.done():
                    code_task.cancel()
                    try:
                        await code_task
                    except asyncio.CancelledError:
                        pass
                return

            code = self._normalize_email_code(await code_task)
            if code:
                if not login_task.done():
                    login_task.cancel()
                    try:
                        await login_task
                    except asyncio.CancelledError:
                        pass
                await self._submit_email_verification_code(code)
                await self._wait_until_logged_in(timeout_ms=timeout_ms)
                return

            await login_task
        finally:
            for task in (code_task, login_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def _wait_until_logged_in(self, timeout_ms: int | None = 0) -> None:
        start = time.monotonic()
        timeout_s = None if not timeout_ms else max(int(timeout_ms), 0) / 1000.0
        while True:
            if await self._is_logged_in():
                return
            if timeout_s is not None and time.monotonic() - start >= timeout_s:
                raise TimeoutError("Timed out waiting for Perplexity login.")
            await asyncio.sleep(0.5)

    async def login(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        await self._dismiss_onboarding()
        if await self._is_logged_in():
            Logger.info("Perplexity: already signed in.")
            self._mark_active_ece_pair_used()
            return

        auto_login = False
        try:
            auto_login = bool(self.config_manager.get_setting("providers_credentials", "auto_login"))
        except Exception:
            auto_login = False

        if not auto_login:
            Logger.info("Perplexity: Auto Login disabled. Waiting for manual login...")
            self.notify_user(
                "Perplexity Login",
                "Please sign in to Perplexity in the browser window. IntenseRP will continue once the session cookie appears.",
                level="info",
            )
            self._background_login_needed("Auto Login is off, so sign-in has to happen in the browser")
            await self._wait_until_logged_in(timeout_ms=0)
            await self._dismiss_onboarding()
            Logger.success("Perplexity: login detected.")
            self._mark_active_ece_pair_used()
            return

        pair = self.ece_active_pair()
        email = str(getattr(pair, "email", "") or "").strip()
        if not email:
            Logger.warning(
                "Perplexity: Auto Login is enabled but no account email is configured. "
                "Waiting for manual login..."
            )
            self.notify_user(
                "Perplexity Login",
                "Auto Login is enabled, but no Perplexity account email is saved. Please sign in manually.",
                level="warning",
            )
            self._background_login_needed("Auto Login has no saved account for this provider")
            await self._wait_until_logged_in(timeout_ms=0)
            await self._dismiss_onboarding()
            self._mark_active_ece_pair_used()
            return

        Logger.info("Perplexity: Auto Login enabled. Starting email-code login...")
        try:
            email_input = await self._open_email_login_form(timeout_ms=15000)
            await self._click_locator_center(
                email_input,
                label="email login input",
                timeout_ms=5000,
            )
            try:
                await email_input.fill("", timeout=5000)
            except Exception:
                pass
            await self.page.keyboard.type(email, delay=20)

            await self._click_continue_with_email(timeout_ms=10000)
            await self._wait_for_verify_request_page(timeout_ms=15000)
        except Exception as exc:
            Logger.warning(f"Perplexity: failed to start email-code login: {exc}")
            self.notify_user(
                "Perplexity Login",
                "Please complete Perplexity sign-in manually in the browser window.",
                level="warning",
            )
            self._background_login_needed("sign-in needs to be finished in the browser")
            await self._wait_until_logged_in(timeout_ms=0)
            await self._dismiss_onboarding()
            self._mark_active_ece_pair_used()
            return

        Logger.info("Perplexity: waiting for email verification code.")
        try:
            await self._wait_for_code_prompt_or_login(timeout_ms=120000)
        except TimeoutError:
            Logger.warning("Perplexity: login code was not completed within 120s. Waiting for manual completion...")
            self._background_login_needed("sign-in needs to be finished in the browser")
            await self._wait_until_logged_in(timeout_ms=0)

        await self._dismiss_onboarding()
        Logger.success("Perplexity: login detected.")
        self.ece_mark_used(email)

    async def _wait_for_chat_ready(self, timeout_ms: int | None = 60000) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")
        await self.page.wait_for_selector(self.CHAT_EDITOR_SELECTOR, timeout=timeout_ms or 0)

    @staticmethod
    def _extract_space_id_from_url(url: str) -> str | None:
        match = re.search(r"/spaces/([^/?#]+)", str(url or ""))
        if not match:
            return None
        space_id = match.group(1).strip()
        return space_id or None

    def _space_url(self, space_id: str) -> str:
        return f"{self.SPACES_URL}/{str(space_id or '').strip()}"

    async def _wait_for_space_chat_ready(self, timeout_ms: int | None = 60000) -> None:
        await self._wait_for_chat_ready(timeout_ms=timeout_ms)
        if not self.page:
            return
        space_id = self._extract_space_id_from_url(str(self.page.url or ""))
        if space_id:
            self._space_id = space_id

    async def _open_cached_space(self) -> bool:
        if not self.page or not self._space_id:
            return False

        try:
            await self.page.goto(
                self._space_url(self._space_id),
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await self._dismiss_onboarding()
            await self._wait_for_space_chat_ready(timeout_ms=20000)
            return True
        except Exception as exc:
            Logger.warning(
                "Perplexity Spaces: cached Space URL did not open as a usable "
                f"chat. It may have been deleted; finding or recreating it now. ({exc})"
            )
            self._space_id = None
            self._last_space_instructions_text = None
            return False

    async def _current_space_is_ready(self) -> bool:
        if not self.page or not self._space_id:
            return False
        current_id = self._extract_space_id_from_url(str(self.page.url or ""))
        if current_id != self._space_id:
            return False
        try:
            await self._wait_for_chat_ready(timeout_ms=1000)
            return True
        except Exception:
            return False

    async def _ensure_signed_in_before_spaces(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        auth_state = await self._read_session_auth_state(timeout_ms=10000)
        if auth_state is True:
            return

        Logger.info(
            "Perplexity Spaces: checking sign-in on the main Perplexity page "
            "before opening Spaces..."
        )
        await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=45000)
        await self._dismiss_onboarding()
        if await self._is_logged_in():
            return

        self._space_id = None
        self._last_space_instructions_text = None
        Logger.info(
            "Perplexity Spaces: sign-in is not confirmed; completing login before "
            "opening Spaces."
        )
        await self.login()

    async def _ensure_space_ready(self) -> None:
        if not self.page or not self._spaces_enabled():
            return

        await self._ensure_signed_in_before_spaces()
        if await self._current_space_is_ready():
            return
        if self._space_id and await self._open_cached_space():
            return

        await self._find_or_create_space()

    @staticmethod
    def _spaces_dashboard_state_script() -> str:
        return (
            """() => {
                const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                    const style = window.getComputedStyle(el);
                    return style && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const linkPathname = (link) => {
                    const href = (link && link.getAttribute && link.getAttribute('href')) || '';
                    try {
                        return new URL(href, window.location.href).pathname;
                    } catch (_exc) {
                        return href.split(/[?#]/, 1)[0];
                    }
                };

                const table = document.querySelector("div[role='table']");
                if (isVisible(table)) return 'table';

                const examplesLink = Array.from(document.querySelectorAll('a[href]')).find((link) => {
                    if (!isVisible(link) || linkPathname(link) !== '/spaces/templates') return false;
                    const child = link.firstElementChild;
                    return !!child
                        && child.tagName.toLowerCase() === 'span'
                        && normalize(child.textContent) === 'View examples';
                });
                return examplesLink ? 'empty' : '';
            }"""
        )

    async def _wait_for_spaces_dashboard_state(self, timeout_ms: int | None = 60000) -> str:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        handle = await self.page.wait_for_function(
            self._spaces_dashboard_state_script(),
            timeout=timeout_ms or 0,
        )
        try:
            state = await handle.json_value()
        except Exception:
            state = ""
        return str(state or "")

    async def _read_spaces_dashboard_state(self) -> str:
        if not self.page:
            return ""
        try:
            state = await self.page.evaluate(self._spaces_dashboard_state_script())
        except Exception:
            return ""
        return str(state or "")

    async def _settle_spaces_dashboard_state(self, state: str) -> str:
        if state not in {"table", "empty"}:
            return state

        await asyncio.sleep(5.0)
        refreshed_state = await self._read_spaces_dashboard_state()
        return refreshed_state or state

    async def _find_or_create_space(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        Logger.info("Perplexity Spaces: opening Spaces dashboard...")
        await self.page.goto(self.SPACES_URL, wait_until="domcontentloaded", timeout=45000)
        await self._dismiss_onboarding()
        dashboard_state = await self._wait_for_spaces_dashboard_state(timeout_ms=60000)
        dashboard_state = await self._settle_spaces_dashboard_state(dashboard_state)

        if dashboard_state == "table" and await self._click_existing_space():
            await self._wait_for_space_chat_ready(timeout_ms=60000)
            Logger.info(f"Perplexity Spaces: opened '{self.SPACE_TITLE}'.")
            return

        if dashboard_state == "empty":
            Logger.info("Perplexity Spaces: no existing Spaces were found; creating one.")

        await self._create_space()

    async def _click_existing_space(self) -> bool:
        if not self.page:
            return False

        try:
            result = await self.page.evaluate(
                """(title) => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const table = document.querySelector("div[role='table']");
                    if (!table) return 'missing-table';
                    const rows = Array.from(table.querySelectorAll("div[class*='group/dashboard-row']"));
                    for (const row of rows) {
                        const cell = Array.from(row.children || []).find((child) => (
                            child && child.getAttribute && child.getAttribute('role') === 'cell'
                        ));
                        if (!cell) continue;
                        const link = cell.firstElementChild;
                        const firstDiv = link && link.firstElementChild;
                        const titleDiv = firstDiv && firstDiv.children && firstDiv.children[1];
                        if (normalize(titleDiv && titleDiv.textContent) !== title) continue;
                        const target = link || row;
                        target.dispatchEvent(new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }));
                        return 'clicked';
                    }
                    return 'missing';
                }""",
                self.SPACE_TITLE,
            )
            return result == "clicked"
        except Exception as exc:
            Logger.warning(f"Perplexity Spaces: failed to inspect Spaces dashboard: {exc}")
            return False

    async def _set_text_control_value(
        self,
        locator: Any,
        text: str,
        *,
        label: str,
        timeout_ms: int = 10000,
    ) -> bool:
        expected = str(text or "")
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            try:
                await locator.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass
            try:
                await locator.click(timeout=timeout_ms)
                await locator.focus(timeout=1000)
            except Exception:
                try:
                    await locator.evaluate("(el) => el && el.focus && el.focus()")
                except Exception:
                    pass
            await locator.fill(expected, timeout=timeout_ms)
            try:
                value = str(await locator.input_value(timeout=1000) or "")
                if value == expected:
                    return True
            except Exception:
                return True
        except Exception as exc:
            Logger.debug(f"Perplexity Spaces: Playwright fill failed for {label}: {exc}")

        try:
            await locator.evaluate(
                """(el, value) => {
                    const text = String(value || '');
                    const proto = Object.getPrototypeOf(el);
                    const descriptor = proto && Object.getOwnPropertyDescriptor(proto, 'value');
                    if (descriptor && descriptor.set) {
                        descriptor.set.call(el, text);
                    } else {
                        el.value = text;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                expected,
            )
            try:
                value = str(await locator.input_value(timeout=1000) or "")
                if value != expected:
                    Logger.warning(
                        f"Perplexity Spaces: {label} did not retain the expected text."
                    )
                    return False
            except Exception:
                pass
            return True
        except Exception as exc:
            Logger.warning(f"Perplexity Spaces: failed to set {label}: {exc}")
            return False

    async def _click_button_with_first_span_text(
        self,
        text: str,
        *,
        button_type: str | None = None,
        timeout_ms: int = 10000,
    ) -> bool:
        if not self.page:
            return False

        deadline = time.monotonic() + max(int(timeout_ms or 0), 0) / 1000.0
        while True:
            try:
                result = await self.page.evaluate(
                    """({label, buttonType}) => {
                        const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                            const style = window.getComputedStyle(el);
                            return style && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        const isTopmost = (el) => {
                            const box = el && el.getBoundingClientRect && el.getBoundingClientRect();
                            if (!box || box.width <= 0 || box.height <= 0) return false;
                            const x = box.left + box.width / 2;
                            const y = box.top + box.height / 2;
                            const top = document.elementFromPoint(x, y);
                            return !!top && (top === el || el.contains(top));
                        };
                        const buttons = Array.from(document.querySelectorAll('button'));
                        const target = buttons.find((button) => {
                            if (buttonType && (button.getAttribute('type') || '').toLowerCase() !== buttonType) {
                                return false;
                            }
                            if (button.disabled || (button.getAttribute('aria-disabled') || '').toLowerCase() === 'true') {
                                return false;
                            }
                            const first = button.firstElementChild;
                            if (!first || first.tagName.toLowerCase() !== 'span') return false;
                            return isVisible(button) && isTopmost(button) && normalize(first.textContent) === label;
                        });
                        if (!target) return 'missing';
                        target.dispatchEvent(new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }));
                        return 'clicked';
                    }""",
                    {
                        "label": str(text or ""),
                        "buttonType": str(button_type or "").lower(),
                    },
                )
                if result == "clicked":
                    return True
            except Exception:
                pass

            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _create_space(self) -> None:
        if not self.page:
            raise RuntimeError("Page is not initialized.")

        Logger.info(f"Perplexity Spaces: creating '{self.SPACE_TITLE}'...")
        create_new_button = self.page.locator("button[aria-label='Create a new Space']").first
        await create_new_button.wait_for(state="visible", timeout=20000)
        await create_new_button.click(timeout=5000)

        title_input = self.page.locator("input[aria-label='Space title']").first
        if not await self._set_text_control_value(
            title_input,
            self.SPACE_TITLE,
            label="Space title",
        ):
            raise RuntimeError("Could not set Perplexity Space title.")

        description_input = self.page.locator("textarea[aria-label='Space description']").first
        if not await self._set_text_control_value(
            description_input,
            self.SPACE_DESCRIPTION,
            label="Space description",
        ):
            raise RuntimeError("Could not set Perplexity Space description.")

        instructions_input = self.page.locator("textarea[aria-label='Space instructions']").first
        try:
            if await instructions_input.count() > 0:
                await self._set_text_control_value(
                    instructions_input,
                    "",
                    label="Space instructions",
                )
        except Exception:
            pass

        if not await self._click_button_with_first_span_text("Create", timeout_ms=20000):
            raise RuntimeError("Could not click the final Perplexity Space Create button.")

        await self._dismiss_space_share_prompt()
        await self._wait_for_space_chat_ready(timeout_ms=60000)
        self._last_space_instructions_text = ""
        Logger.success(f"Perplexity Spaces: created '{self.SPACE_TITLE}'.")

    async def _dismiss_space_share_prompt(self) -> None:
        clicked = await self._click_button_with_first_span_text("Skip", timeout_ms=15000)
        if clicked:
            Logger.info("Perplexity Spaces: dismissed post-create share prompt.")

    async def _read_space_instructions_button_state(self) -> str:
        if not self.page:
            return ""
        try:
            return str(
                await self.page.evaluate(
                    """() => {
                        const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                        const buttons = Array.from(document.querySelectorAll("button[role='button'], button"));
                        for (const button of buttons) {
                            const ariaLabel = normalize(button.getAttribute('aria-label'));
                            if (ariaLabel === 'Edit instructions') {
                                return 'edit';
                            }
                            if (ariaLabel === 'Add instructions' || ariaLabel === 'Add instructions...') {
                                return 'add';
                            }
                            const children = Array.from(button.children || []);
                            const divTexts = children
                                .filter((child) => child.tagName && child.tagName.toLowerCase() === 'div')
                                .map((child) => normalize(child.textContent));
                            const fullText = normalize(button.textContent);
                            if (divTexts[0] === 'Edit instructions' || fullText === 'Edit instructions') {
                                return 'edit';
                            }
                            if (divTexts[1] === 'Add instructions...' || fullText.includes('Add instructions...')) {
                                return 'add';
                            }
                        }
                        return '';
                    }"""
                )
                or ""
            ).strip()
        except Exception:
            return ""

    async def _click_space_instructions_button(self, state: str) -> bool:
        if not self.page:
            return False
        state = str(state or "").strip().lower()
        if state not in {"add", "edit"}:
            return False
        try:
            result = await self.page.evaluate(
                """(wanted) => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const buttonState = (button) => {
                        const ariaLabel = normalize(button.getAttribute('aria-label'));
                        if (ariaLabel === 'Edit instructions') {
                            return 'edit';
                        }
                        if (ariaLabel === 'Add instructions' || ariaLabel === 'Add instructions...') {
                            return 'add';
                        }
                        const children = Array.from(button.children || []);
                        const divTexts = children
                            .filter((child) => child.tagName && child.tagName.toLowerCase() === 'div')
                            .map((child) => normalize(child.textContent));
                        const fullText = normalize(button.textContent);
                        if (divTexts[0] === 'Edit instructions' || fullText === 'Edit instructions') {
                            return 'edit';
                        }
                        if (divTexts[1] === 'Add instructions...' || fullText.includes('Add instructions...')) {
                            return 'add';
                        }
                        return '';
                    };
                    const buttons = Array.from(document.querySelectorAll("button[role='button'], button"));
                    const target = buttons.find((button) => isVisible(button) && buttonState(button) === wanted);
                    if (!target) return 'missing';
                    target.dispatchEvent(new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    }));
                    return 'clicked';
                }""",
                state,
            )
            return result == "clicked"
        except Exception:
            return False

    async def _sync_space_instructions(self, desired_text: str) -> None:
        if not self.page or not self._spaces_enabled():
            return

        desired = str(desired_text or "")
        if len(desired) > self.SPACE_INSTRUCTIONS_LIMIT:
            Logger.warning(
                "Perplexity Spaces: requested Space instructions exceeded 8000 "
                "characters; trimming to Perplexity's limit."
            )
            desired = desired[: self.SPACE_INSTRUCTIONS_LIMIT]

        await self._ensure_space_ready()
        state = await self._read_space_instructions_button_state()
        if state == "add" and not desired.strip():
            self._last_space_instructions_text = ""
            return
        if not state:
            Logger.warning("Perplexity Spaces: instructions button was not found.")
            return
        if not await self._click_space_instructions_button(state):
            Logger.warning("Perplexity Spaces: failed to open instructions editor.")
            return

        textarea = self.page.locator(self.SPACE_INSTRUCTIONS_TEXTAREA_SELECTOR).first
        try:
            await textarea.wait_for(state="visible", timeout=10000)
            current_value = str(await textarea.input_value(timeout=3000) or "")
        except Exception as exc:
            Logger.warning(f"Perplexity Spaces: instructions textarea was not ready: {exc}")
            return

        if current_value == desired:
            await self._click_button_with_first_span_text(
                "Cancel",
                button_type="button",
                timeout_ms=5000,
            )
            self._last_space_instructions_text = desired
            return

        if not await self._set_text_control_value(
            textarea,
            desired,
            label="Space answer instructions",
        ):
            await self._click_button_with_first_span_text(
                "Cancel",
                button_type="button",
                timeout_ms=5000,
            )
            return

        if not await self._click_button_with_first_span_text(
            "Save",
            button_type="button",
            timeout_ms=10000,
        ):
            Logger.warning("Perplexity Spaces: failed to save Space instructions.")
            return

        try:
            await textarea.wait_for(state="hidden", timeout=5000)
        except Exception:
            pass
        self._last_space_instructions_text = desired
        Logger.info(
            f"Perplexity Spaces: synced Space instructions ({len(desired)} chars)."
        )

    async def set_sidebar_status(self, open: bool) -> None:
        _ = open
        return None

    async def click_new_chat(self, source: str = "auto") -> None:
        self._raise_if_request_cancelled()
        _ = source
        if not self.page:
            return
        if self._spaces_enabled():
            await self._ensure_space_ready()
        else:
            await self.page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=45000)
            await self._dismiss_onboarding()
            await self._wait_for_chat_ready(timeout_ms=60000)

    def _model_switching_available(self) -> bool:
        tier = str(self.subscription_tier or "free").strip().lower()
        return tier in self.MODEL_SWITCH_TIERS

    def _get_configured_model_label(self) -> str:
        try:
            value = self.config_manager.get_setting("perplexity_behavior", "model")
        except Exception:
            value = None
        value = str(value or "").strip()
        return value or PERPLEXITY_MODEL_OPTIONS[0]

    def api_real_model_labels(self) -> list[str]:
        return list(PERPLEXITY_MODEL_OPTIONS)

    def _get_model_label_for_request(self, model: Any = None) -> str:
        override = resolve_real_model_label_from_model_id(
            self.provider,
            model,
            self.api_real_model_labels(),
        )
        return override or self._get_configured_model_label()

    @staticmethod
    def _canonicalize_model_label(value: str) -> str:
        raw = str(value or "").strip().lower()
        raw = raw.replace("(auto)", "auto")
        return re.sub(r"[^a-z0-9]+", "", raw)

    async def _read_current_model_selection(self) -> str:
        if not self.page:
            return ""
        try:
            return str(
                await self.page.evaluate(
                    """(labels) => {
                        const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                        const wanted = labels.map((label) => [label, normalize(label).toLowerCase()]);
                        const candidates = Array.from(document.querySelectorAll("button[aria-label='Model'], button"));
                        for (const el of candidates) {
                            const text = normalize(el.textContent);
                            if (!text) continue;
                            const lower = text.toLowerCase();
                            for (const [label, canon] of wanted) {
                                if (lower.includes(canon)) {
                                    return label;
                                }
                            }
                        }
                        return '';
                    }""",
                    PERPLEXITY_MODEL_OPTIONS,
                )
                or ""
            ).strip()
        except Exception as exc:
            Logger.debug(f"Perplexity: failed to read current model selection: {exc}")
            return ""

    def _model_allowed_for_tier(self, model_label: str) -> bool:
        tier = str(self.subscription_tier or "free").strip().lower()
        if tier == "max":
            return True
        if tier == "pro":
            return str(model_label or "").strip() not in self.MAX_ONLY_MODELS
        return False

    def should_apply_configured_model_before_request(self) -> bool:
        return False

    async def apply_configured_model(self, model: Any = None) -> None:
        desired = self._get_model_label_for_request(model)
        if not desired:
            return
        if not self._model_switching_available():
            Logger.debug("Perplexity: model switching is skipped on free accounts.")
            return
        try:
            enable_thinking = bool(self.config_manager.get_setting("perplexity_behavior", "enable_deepthink"))
        except Exception:
            enable_thinking = False
        await self._select_model_and_thinking(desired, enable_thinking)

    async def set_deepthink_state(self, state: bool, model: Any = None) -> None:
        if not self._model_switching_available():
            if state:
                Logger.warning("Perplexity: Thinking mode cannot be toggled on free accounts.")
            return
        await self._select_model_and_thinking(self._get_model_label_for_request(model), bool(state))

    async def _find_model_picker_trigger(self):
        if not self.page:
            return None

        trigger = self.page.locator("button[aria-label='Model']")
        if await trigger.count() > 0:
            return trigger

        for label in PERPLEXITY_MODEL_OPTIONS:
            trigger = self.page.locator("button").filter(has_text=label)
            if await trigger.count() > 0:
                return trigger
        return None

    async def _open_model_picker(self) -> bool:
        if not self.page:
            return False

        trigger = await self._find_model_picker_trigger()
        if not trigger:
            await self._ensure_request_mode_search()
            trigger = await self._find_model_picker_trigger()
        if not trigger:
            Logger.warning("Perplexity: model picker button was not found.")
            return False

        try:
            await trigger.first.click(timeout=5000)
            await self.page.wait_for_selector("div[role='menuitemradio']", timeout=5000)
            return True
        except Exception as exc:
            Logger.warning(f"Perplexity: failed to open model picker: {exc}")
            return False

    async def _select_model_and_thinking(self, model_label: str, thinking_enabled: bool) -> None:
        if not self.page:
            return

        desired = str(model_label or "").strip() or PERPLEXITY_MODEL_OPTIONS[0]
        if desired not in PERPLEXITY_MODEL_OPTIONS:
            Logger.warning(f"Perplexity: unknown configured model '{desired}'.")
            return
        if not self._model_allowed_for_tier(desired):
            Logger.warning(
                f"Perplexity: model '{desired}' is not available on the detected "
                f"'{self.subscription_tier}' subscription tier."
            )
            return

        current = await self._read_current_model_selection()
        need_model_click = self._canonicalize_model_label(current) != self._canonicalize_model_label(desired)
        need_thinking_click = desired not in self.FORCED_THINKING_MODELS

        if not need_model_click and not need_thinking_click:
            return

        if not await self._open_model_picker():
            return

        try:
            if need_model_click:
                result = await self.page.evaluate(
                    """(wanted) => {
                        const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();
                        const target = normalize(wanted);
                        const radios = Array.from(document.querySelectorAll("div[role='menuitemradio']"));
                        for (const radio of radios) {
                            const text = normalize(radio.textContent);
                            if (!text.includes(target)) continue;
                            const parentClass = (radio.parentElement && radio.parentElement.getAttribute('class') || '').toString();
                            if (!parentClass.includes('rounded-lg')) {
                                return 'locked';
                            }
                            radio.click();
                            return 'clicked';
                        }
                        return 'missing';
                    }""",
                    desired,
                )
                if result == "locked":
                    Logger.warning(f"Perplexity: model '{desired}' is locked for this account.")
                    return
                if result != "clicked":
                    Logger.warning(f"Perplexity: model '{desired}' was not found in the picker.")
                    return
                await asyncio.sleep(0.35)

            await self._set_model_picker_thinking_state(desired, bool(thinking_enabled))
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

    async def _set_model_picker_thinking_state(self, model_label: str, wanted: bool) -> None:
        if not self.page:
            return
        if model_label in self.FORCED_THINKING_MODELS:
            return

        result = await self.page.evaluate(
            """(wanted) => {
                const radios = Array.from(document.querySelectorAll("div[role='menuitemradio']"));
                const selected = radios.find((radio) => {
                    const aria = (radio.getAttribute('aria-checked') || '').toLowerCase();
                    const dataState = (radio.getAttribute('data-state') || '').toLowerCase();
                    return aria === 'true' || dataState === 'checked';
                }) || radios.find((radio) => {
                    const cls = (radio.getAttribute('class') || '').toString();
                    return cls.includes('bg-') || cls.includes('selected');
                });
                const scopes = [];
                if (selected) {
                    scopes.push(selected.parentElement);
                    scopes.push(selected.parentElement && selected.parentElement.parentElement);
                }
                scopes.push(document);
                for (const scope of scopes) {
                    if (!scope) continue;
                    const boxes = Array.from(scope.querySelectorAll("div[role='menuitemcheckbox']"));
                    const box = boxes.find((el) => {
                        const text = (el.textContent || '').toString().toLowerCase();
                        return text.includes('thinking') || text.includes('reasoning') || boxes.length === 1;
                    });
                    if (!box) continue;
                    const disabled = (box.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
                    if (disabled) return 'disabled';
                    const checked = (box.getAttribute('aria-checked') || '').toLowerCase() === 'true';
                    if (checked === !!wanted) return 'already';
                    box.click();
                    return 'clicked';
                }
                return 'missing';
            }""",
            bool(wanted),
        )
        if result == "disabled":
            Logger.debug("Perplexity: Thinking toggle is forced/disabled for the selected model.")
        elif result == "missing":
            Logger.debug("Perplexity: Thinking toggle was not found in the model picker.")

    async def _open_tools_menu(self) -> bool:
        if not self.page:
            return False
        button = self.page.locator("button[aria-label='Add files or tools']")
        if await button.count() == 0:
            Logger.warning("Perplexity: Add files or tools button was not found.")
            return False
        try:
            await button.first.click(timeout=5000)
            await asyncio.sleep(0.15)
            return True
        except Exception as exc:
            Logger.warning(f"Perplexity: failed to open tools menu: {exc}")
            return False

    async def _open_web_search_submenu(self) -> bool:
        if not self.page:
            return False

        handle = None
        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const divChildren = (el) => Array.from((el && el.children) || []).filter((child) => (
                        child.tagName && child.tagName.toLowerCase() === 'div'
                    ));
                    const menuItems = Array.from(document.querySelectorAll("div[role='menuitem']"));
                    return menuItems.find((item) => {
                        const childDiv = divChildren(item)[0];
                        const secondChildDiv = divChildren(childDiv)[1];
                        const labelWrapper = divChildren(secondChildDiv)[0];
                        const labelDiv = divChildren(labelWrapper)[0];
                        return normalize(labelDiv && labelDiv.textContent) === 'Connectors and sources';
                    }) || null;
                }"""
            )
            element = handle.as_element() if handle else None
            if not element:
                Logger.warning("Perplexity: Web search submenu button was not found.")
                return False
            await element.click(timeout=5000)
            await asyncio.sleep(0.2)
            return True
        except Exception as exc:
            Logger.warning(f"Perplexity: failed to open Web search submenu: {exc}")
            return False
        finally:
            if handle:
                try:
                    await handle.dispose()
                except Exception:
                    pass

    async def _set_web_search_checkbox(self, wanted: bool) -> bool:
        if not self.page:
            return False

        if not await self._open_tools_menu():
            return False
        if not await self._open_web_search_submenu():
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        try:
            result = await self.page.evaluate(
                """(wanted) => {
                    const boxes = Array.from(document.querySelectorAll("div[role='menuitemcheckbox']"));
                    const box = boxes.find((el) => {
                        const text = (el.textContent || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();
                        return text === 'web' || text.includes('web');
                    });
                    if (!box) return 'missing';
                    const checked = (box.getAttribute('aria-checked') || '').toLowerCase() === 'true';
                    if (checked === !!wanted) return 'already';
                    box.click();
                    return 'clicked';
                }""",
                bool(wanted),
            )
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass

        return result in {"already", "clicked"}

    async def set_search_state(self, state: bool) -> None:
        if not bool(state):
            return
        ok = await self._ensure_request_mode_search()
        if not ok:
            Logger.warning("Perplexity: could not confirm Search request mode.")

    async def _ensure_request_mode_search(self) -> bool:
        if not self.page:
            return False

        read_mode_js = """(button, labels) => {
            const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
            const text = normalize(button && button.textContent);
            const lower = text.toLowerCase();
            for (const label of labels || []) {
                if (lower.includes(normalize(label).toLowerCase())) {
                    return label;
                }
            }
            return text;
        }"""

        read_pressed_js = """(button) => {
            const dataPressed = (button && button.getAttribute('data-pressed') || '').toLowerCase();
            const ariaPressed = (button && button.getAttribute('aria-pressed') || '').toLowerCase();
            if (dataPressed === 'false' || ariaPressed === 'false') return false;
            if (dataPressed === 'true' || ariaPressed === 'true') return true;
            return null;
        }"""

        handle = None
        opened_menu = False
        try:
            mode_labels = list(self.REQUEST_MODE_LABELS)
            handle = await self.page.evaluate_handle(
                """(labels) => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const hasModeIcon = (button) => Array.from(button.querySelectorAll('use')).some((use) => {
                        const href = (use.getAttribute('href') || use.getAttribute('xlink:href') || '').toString();
                        return /pplx-icon-(search|book|telescope|gavel)/.test(href);
                    });
                    const buttons = Array.from(document.querySelectorAll('button'));
                    return buttons.find((button) => {
                        if (!isVisible(button)) return false;
                        const hasMenu = (button.getAttribute('aria-haspopup') || '').toLowerCase() === 'menu';
                        if (!hasMenu) return false;
                        const hasPressedState = button.hasAttribute('data-pressed') || button.hasAttribute('aria-pressed');
                        const text = normalize(button.textContent).toLowerCase();
                        const hasModeLabel = labels.some((label) => text.includes(normalize(label).toLowerCase()));
                        return hasPressedState || hasModeLabel || hasModeIcon(button);
                    }) || null;
                }""",
                mode_labels,
            )
            button = handle.as_element() if handle else None
            if not button:
                Logger.warning("Perplexity: request mode button was not found.")
                return False

            pressed = await button.evaluate(read_pressed_js)
            if pressed is False:
                await button.click(timeout=5000)
                await asyncio.sleep(0.25)
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass

            current_mode = str(await button.evaluate(read_mode_js, mode_labels) or "").strip()
            pressed = await button.evaluate(read_pressed_js)
            if current_mode == "Search" and pressed is not False:
                return True

            await button.hover(timeout=5000)
            opened_menu = True
            try:
                await self.page.wait_for_selector("div[role='menuitemradio']", timeout=5000)
            except Exception:
                await button.click(timeout=5000)
                await self.page.wait_for_selector("div[role='menuitemradio']", timeout=5000)
            await asyncio.sleep(0.15)

            result = await self.page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const radioLabel = (radio) => {
                        const first = radio && radio.children && radio.children[0];
                        const label = first
                            && first.children
                            && first.children[1]
                            && first.children[1].children
                            && first.children[1].children[0]
                            && first.children[1].children[0].children
                            && first.children[1].children[0].children[0];
                        return normalize(label && label.textContent) || normalize(radio && radio.textContent);
                    };
                    const radios = Array.from(document.querySelectorAll("div[role='menuitemradio']"));
                    const target = radios.find((radio) => isVisible(radio) && radioLabel(radio) === 'Search');
                    if (!target) return 'missing';
                    const aria = (target.getAttribute('aria-checked') || '').toLowerCase();
                    const dataState = (target.getAttribute('data-state') || '').toLowerCase();
                    if (aria === 'true' || dataState === 'checked') return 'already';
                    target.dispatchEvent(new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    }));
                    return 'clicked';
                }"""
            )
            if result not in {"already", "clicked"}:
                Logger.warning("Perplexity: Search request mode option was not found.")
                return False

            await asyncio.sleep(0.2)
            if handle:
                try:
                    await handle.dispose()
                except Exception:
                    pass
                handle = await self.page.evaluate_handle(
                    """(labels) => {
                        const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                            const style = window.getComputedStyle(el);
                            return style && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        const buttons = Array.from(document.querySelectorAll('button'));
                        return buttons.find((button) => {
                            if (!isVisible(button)) return false;
                            if ((button.getAttribute('aria-haspopup') || '').toLowerCase() !== 'menu') return false;
                            const text = normalize(button.textContent).toLowerCase();
                            return labels.some((label) => text.includes(normalize(label).toLowerCase()));
                        }) || null;
                    }""",
                    mode_labels,
                )
                button = handle.as_element() if handle else None

            current_mode = str(await button.evaluate(read_mode_js, mode_labels) or "").strip() if button else ""
            pressed = await button.evaluate(read_pressed_js) if button else None
            if current_mode != "Search" or pressed is False:
                Logger.warning(
                    f"Perplexity: request mode still shows '{current_mode or 'unknown'}' after selecting Search."
                )
                return False
            return True
        except Exception as exc:
            Logger.warning(f"Perplexity: failed to ensure request mode Search: {exc}")
            return False
        finally:
            if opened_menu:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
            if handle:
                try:
                    await handle.dispose()
                except Exception:
                    pass

    async def _file_upload_is_capped(self) -> bool:
        if not self.page:
            return False
        if not await self._open_tools_menu():
            return False
        try:
            capped = await self.page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const group = Array.from(document.querySelectorAll('div')).find((el) => (
                        el.classList && el.classList.contains('group/file-upload')
                    ));
                    const items = Array.from(document.querySelectorAll("[role='menuitem']"));
                    const uploadItem = (group && group.querySelector("[role='menuitem']"))
                        || items.find((item) => (
                            isVisible(item)
                            && /upload files?( or images)?/i.test(normalize(item.textContent))
                        ));
                    if (!uploadItem) return false;
                    const disabled = (uploadItem.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
                        || uploadItem.hasAttribute('data-disabled');
                    if (disabled) return true;
                    const text = normalize(uploadItem.textContent).toLowerCase();
                    if (/(limit|upgrade|unavailable|not available)/.test(text)) return true;
                    const lock = Array.from(uploadItem.querySelectorAll('use')).some((use) => {
                        const href = (use.getAttribute('href') || use.getAttribute('xlink:href') || '').toString();
                        return href.includes('pplx-icon-lock');
                    });
                    return !!lock;
                }"""
            )
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
        return bool(capped)

    async def _upload_file_via_tools_menu(self, file_spec: Any) -> bool:
        if not self.page:
            return False
        if not await self._open_tools_menu():
            return False

        handle = None
        try:
            handle = await self.page.evaluate_handle(
                """() => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el || !el.getClientRects || el.getClientRects().length === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const items = Array.from(document.querySelectorAll("[role='menuitem']"));
                    return items.find((item) => (
                        isVisible(item)
                        && /upload files?( or images)?/i.test(normalize(item.textContent))
                    )) || null;
                }"""
            )
            element = handle.as_element() if handle else None
            if not element:
                Logger.warning("Perplexity: Upload files or images menu item was not found.")
                return False

            async with self.page.expect_file_chooser(timeout=6000) as fc_info:
                await element.click(timeout=3000)
            chooser = await fc_info.value
            await chooser.set_files(file_spec)
            return True
        except Exception as exc:
            Logger.warning(f"Perplexity: plus-menu file upload failed: {exc}")
            return False
        finally:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            if handle:
                try:
                    await handle.dispose()
                except Exception:
                    pass

    async def upload_file(self, file_spec: Any) -> None:
        await self._upload_file(file_spec)

    @staticmethod
    def _set_system_clipboard_text(text: str) -> str | None:
        try:
            from PySide6.QtGui import QGuiApplication

            app = QGuiApplication.instance()
            if app is None:
                return None
            clipboard = app.clipboard()
            if clipboard is None:
                return None
            previous = clipboard.text()
            clipboard.setText(text)
            return previous
        except Exception as exc:
            Logger.debug(f"Perplexity: failed to set system clipboard text: {exc}")
            return None

    @staticmethod
    def _restore_system_clipboard_text(text: str | None) -> None:
        if text is None:
            return
        try:
            from PySide6.QtGui import QGuiApplication

            app = QGuiApplication.instance()
            if app is None:
                return
            clipboard = app.clipboard()
            if clipboard is not None:
                clipboard.setText(text)
        except Exception:
            pass

    async def _upload_file(self, file_spec: Any) -> bool:
        self._check_before_new_input(upload=True)
        if not self.page:
            return False

        try:
            if await self._file_upload_is_capped():
                Logger.warning("Perplexity: file upload cap appears to be reached; falling back to pasted text.")
                return False
        except Exception:
            pass

        file_input = self.page.locator(self.FILE_INPUT_SELECTOR)
        try:
            if await file_input.count() == 0:
                try:
                    await self.page.wait_for_selector(self.FILE_INPUT_SELECTOR, timeout=6000)
                except Exception:
                    pass
                file_input = self.page.locator(self.FILE_INPUT_SELECTOR)
            if await file_input.count() == 0:
                if not await self._upload_file_via_tools_menu(file_spec):
                    return False
            else:
                await file_input.first.set_input_files(file_spec)
        except Exception as exc:
            Logger.warning(f"Perplexity: file upload failed: {exc}")
            return False

        spinner = self.page.locator(self.UPLOAD_SPINNER_SELECTOR)
        appeared = False
        for _ in range(30):
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
                    if await spinner.count() == 0 or not await spinner.first.is_visible():
                        break
                except Exception:
                    break
                await asyncio.sleep(0.1)

        return True

    async def enter_message(self, message: str) -> None:
        self._check_before_new_input()
        if not self.page:
            return

        text = str(message or "")
        preview = text[:50] + "..." if len(text) > 50 else text
        Logger.debug(f"Perplexity: entering message: {preview}")

        editor = self.page.locator(self.CHAT_EDITOR_SELECTOR)
        if await editor.count() == 0:
            Logger.warning("Perplexity: contenteditable chat editor was not found.")
            return

        if await self._paste_message_via_clipboard(editor.first, text):
            return

        Logger.warning("Perplexity: clipboard paste failed; falling back to direct DOM insertion.")
        await self._insert_message_via_dom(editor.first, text)

    async def _paste_message_via_clipboard(self, editor, text: str) -> bool:
        if not self.page:
            return False

        previous_clipboard: str | None = None
        restore_clipboard = False
        try:
            await editor.click(timeout=5000)
            await asyncio.sleep(0.05)
            try:
                await editor.focus(timeout=1000)
            except Exception:
                try:
                    await editor.evaluate("(el) => el.focus()")
                except Exception:
                    pass
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")

            # In Background Mode on Linux the browser lives on a private display with its
            # own clipboard: the app's (desktop) clipboard would paste nothing there. The
            # Background Mode supervisor plays the desktop clipboard on that display
            # instead, so the paste looks the same to the page and the user's real
            # clipboard is never touched.
            placed_in_background = False
            if self._background_private_clipboard:
                previous_clipboard = None
                placed_in_background = await self._set_background_clipboard_text(text)
            else:
                previous_clipboard = self._set_system_clipboard_text(text)
            if previous_clipboard is not None:
                restore_clipboard = True
            elif not placed_in_background:
                try:
                    if self.context:
                        await self.context.grant_permissions(
                            ["clipboard-read", "clipboard-write"],
                            origin="https://www.perplexity.ai",
                        )
                except Exception:
                    pass
                await self.page.evaluate(
                    """async (value) => {
                        await navigator.clipboard.writeText(String(value || ''));
                    }""",
                    text,
                )

            await self.page.keyboard.press("Control+V")
            await asyncio.sleep(0.35)

            pasted = await editor.evaluate(
                """(el) => (el.innerText || el.textContent || '').toString()"""
            )
            pasted_text = str(pasted or "").replace("\r\n", "\n").strip()
            expected_text = str(text or "").replace("\r\n", "\n").strip()
            if not expected_text:
                return not pasted_text
            if expected_text in pasted_text:
                return True

            compact_expected = re.sub(r"\s+", " ", expected_text).strip()
            compact_pasted = re.sub(r"\s+", " ", pasted_text).strip()
            if compact_expected and compact_expected in compact_pasted:
                return True
            if len(compact_expected) >= 200:
                prefix = compact_expected[:200]
                suffix = compact_expected[-200:]
                min_length = int(len(compact_expected) * 0.9)
                return (
                    len(compact_pasted) >= min_length
                    and prefix in compact_pasted
                    and suffix in compact_pasted
                )
            return False
        except Exception as exc:
            Logger.debug(f"Perplexity: clipboard paste path failed: {exc}")
            return False
        finally:
            if restore_clipboard:
                # Give Chromium a beat to consume the paste before putting the user's
                # clipboard back. This keeps the manual-paste path without being rude.
                await asyncio.sleep(0.1)
                self._restore_system_clipboard_text(previous_clipboard)

    async def _insert_message_via_dom(self, editor, text: str) -> None:
        await editor.evaluate(
            """(el, text) => {
                const lines = String(text || '').replace(/\\r\\n/g, '\\n').split('\\n');
                const makeParagraph = (line) => {
                    const p = document.createElement('p');
                    if (line === '') {
                        p.appendChild(document.createElement('br'));
                    } else {
                        const span = document.createElement('span');
                        span.textContent = line;
                        p.appendChild(span);
                    }
                    return p;
                };
                el.focus();
                el.innerHTML = '';
                for (const line of lines) {
                    el.appendChild(makeParagraph(line));
                }
                const range = document.createRange();
                range.selectNodeContents(el);
                range.collapse(false);
                const selection = window.getSelection();
                if (selection) {
                    selection.removeAllRanges();
                    selection.addRange(range);
                }
                try {
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        cancelable: true,
                        inputType: 'insertText',
                        data: text
                    }));
                } catch (e) {
                    el.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
                }
            }""",
            text,
        )

    async def send_message(self, timeout: int | None = None) -> None:
        if not self.page:
            return

        max_wait_s = 0 if timeout is None else max(int(timeout), 0)
        deadline = time.monotonic() + max_wait_s
        last_error: Exception | None = None

        while True:
            self._raise_if_request_cancelled()
            button = self.page.locator(self.SUBMIT_BUTTON_SELECTOR)
            try:
                if await button.count() > 0 and await button.first.is_visible():
                    disabled_attr = await button.first.get_attribute("disabled")
                    aria_disabled = str(await button.first.get_attribute("aria-disabled") or "").lower()
                    enabled = await button.first.is_enabled()
                    if disabled_attr is None and aria_disabled != "true" and enabled:
                        if not await self._ensure_request_mode_search():
                            last_error = RuntimeError("Could not confirm Perplexity request mode is Search.")
                            break
                        await self._remember_send_control_signature(button.first)
                        self._mark_request_sending()
                        await button.first.click(timeout=3000)
                        return
                    last_error = RuntimeError("Submit button is disabled.")
            except Exception as exc:
                last_error = exc

            if max_wait_s <= 0 or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)

        if last_error:
            Logger.warning(f"Perplexity: failed to click submit button: {last_error}")
        else:
            Logger.warning("Perplexity: submit button not found.")

    async def _click_stop_button(self, timeout_s: float = 8.0) -> bool:
        if not self.page:
            return False

        selectors = [
            "button[aria-label*='Stop']",
            "button[aria-label*='Cancel']",
            "button[aria-label*='Interrupt']",
        ]
        deadline = time.monotonic() + max(float(timeout_s or 0.0), 0.0)
        while True:
            for selector in selectors:
                button = self.page.locator(selector)
                try:
                    if await button.count() > 0 and await button.first.is_visible():
                        await button.first.click(timeout=1500)
                        return True
                except Exception:
                    continue
            if timeout_s <= 0 or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)

    def _resolve_deepthink_flags(self, model: str) -> tuple[bool, bool]:
        try:
            enable_deepthink = bool(self.config_manager.get_setting("perplexity_behavior", "enable_deepthink"))
        except Exception:
            enable_deepthink = False
        try:
            send_deepthink = bool(self.config_manager.get_setting("perplexity_behavior", "send_deepthink"))
        except Exception:
            send_deepthink = False

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

    def _resolve_request_settings(self, model: str, overrides: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        ui_model_label = self._get_model_label_for_request(model)
        enable_deepthink, send_deepthink = self._resolve_deepthink_flags(model)
        try:
            enable_search = bool(self.config_manager.get_setting("perplexity_behavior", "enable_search"))
        except Exception:
            enable_search = False
        try:
            send_as_text_file = bool(self.config_manager.get_setting("perplexity_behavior", "send_as_text_file"))
        except Exception:
            send_as_text_file = False
        use_spaces = self._spaces_enabled()
        sync_space_instructions = self._space_instruction_sync_enabled()

        settings = {
            "model_label": ui_model_label,
            "deepthink_enabled": bool(enable_deepthink),
            "send_deepthink": bool(send_deepthink),
            "search_enabled": bool(enable_search),
            "send_as_text_file": bool(send_as_text_file),
            "use_spaces": bool(use_spaces),
            "sync_space_instructions": bool(sync_space_instructions),
        }
        for key, value in (overrides or {}).items():
            if key in settings:
                settings[key] = bool(value)
        if not settings["use_spaces"]:
            settings["sync_space_instructions"] = False
        return settings

    async def _enqueue_openai_delta(
        self,
        response_queue: asyncio.Queue,
        content: str,
        *,
        finish_reason: str | None = None,
    ) -> None:
        if not content and not finish_reason:
            return
        model_name = self.current_model or "perplexity-auto"
        await response_queue.put(
            make_openai_delta_sse(
                model_name,
                content,
                finish_reason=finish_reason,
            )
        )

    def _format_messages(self, messages: Union[str, List[Any]]) -> str:
        return format_request_messages(self.config_manager, messages)

    def _message_format_separator(self) -> str:
        try:
            apply_formatting = bool(self.config_manager.get_setting("formatting", "apply_formatting"))
        except Exception:
            apply_formatting = False
        if not apply_formatting:
            return "\n"
        try:
            divider = self.config_manager.get_setting("formatting", "formatting_divider") or ""
        except Exception:
            divider = ""
        return str(divider).replace("\\n", "\n")

    @staticmethod
    def _message_content_as_text(message: Any) -> str:
        content = None
        try:
            content = getattr(message, "content")
        except Exception:
            content = None
        if content is None and isinstance(message, dict):
            content = message.get("content", "")
        return "" if content is None else str(content)

    def _strip_leading_rendered_injection(self, text: str, injection_text: str) -> str:
        if not text or not injection_text:
            return text
        if text == injection_text:
            return ""
        if text.startswith(injection_text):
            remainder = text[len(injection_text) :]
            return remainder[1:] if remainder.startswith("\n") else remainder
        return text

    def _strip_formatted_prefix(self, text: str, prefix: str) -> str:
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

    def _append_space_instruction_part(self, parts: list[str], text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return True
        candidate = "\n\n".join(parts + [normalized])
        if len(candidate) > self.SPACE_INSTRUCTIONS_LIMIT:
            return False
        parts.append(normalized)
        return True

    def _prepare_space_prompt_payload(
        self,
        message_for_formatting: Union[str, List[Any]],
        *,
        sync_space_instructions: bool,
    ) -> tuple[str, str]:
        formatted_message = self._format_messages(message_for_formatting)
        if isinstance(message_for_formatting, str) or not sync_space_instructions:
            return formatted_message, ""

        leading_system_messages, _remaining_messages = split_leading_system_messages(
            message_for_formatting
        )

        space_instruction_parts: list[str] = []
        selected_leading_messages: list[Any] = []
        for item in leading_system_messages:
            content = self._message_content_as_text(item).strip()
            if content and not self._append_space_instruction_part(
                space_instruction_parts,
                content,
            ):
                break
            selected_leading_messages.append(item)

        injection_position, rendered_injection = resolve_rendered_injection(
            self.config_manager,
            message_for_formatting,
        )
        rendered_injection = str(rendered_injection or "").strip()
        use_before_injection = (
            bool(rendered_injection)
            and str(injection_position or "").strip().lower() == "before"
        )
        move_injection_to_space = False
        if use_before_injection:
            move_injection_to_space = self._append_space_instruction_part(
                space_instruction_parts,
                rendered_injection,
            )

        injection_to_restore = ""
        if use_before_injection:
            stripped = self._strip_leading_rendered_injection(
                formatted_message,
                rendered_injection,
            )
            if stripped != formatted_message:
                if move_injection_to_space:
                    formatted_message = stripped
                else:
                    injection_to_restore = rendered_injection
                    formatted_message = stripped

        if selected_leading_messages:
            leading_prefix = self._format_messages(selected_leading_messages)
            if use_before_injection:
                leading_prefix = self._strip_leading_rendered_injection(
                    leading_prefix,
                    rendered_injection,
                )
            formatted_message = self._strip_formatted_prefix(
                formatted_message,
                leading_prefix,
            )

        if injection_to_restore:
            formatted_message = (
                injection_to_restore + ("\n" + formatted_message if formatted_message else "")
            )

        return formatted_message, "\n\n".join(space_instruction_parts)

    async def generate_response(
        self,
        message: Union[str, List[Any]],
        model: str = "perplexity-auto",
        stream: bool = False,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        abort_event: asyncio.Event | None = None,
    ):
        _ = stream, temperature, top_p, max_tokens
        if not self.page or not self.context:
            yield f"data: {json.dumps({'error': 'Perplexity driver is not running.'})}\n\n"
            return

        await self.require_english_ui()

        response_queue: asyncio.Queue = asyncio.Queue()
        completion_armed = asyncio.Event()
        completion_started = asyncio.Event()
        completion_claim_lock = asyncio.Lock()
        completion_claimed = False

        self.abort_requested = False
        self.current_abort_event = abort_event
        resolved_model = str(model or "").strip() or "perplexity-auto"
        self.current_model = resolved_model
        self.current_send_deepthink = False

        macros_overrides: Dict[str, bool] = {}
        message_for_formatting = message
        if isinstance(message, list):
            message_for_formatting, macros_overrides = strip_macros_from_messages(
                message, macro_actions=COMMON_REQUEST_MACRO_ACTIONS
            )
        elif isinstance(message, str):
            message_for_formatting, macros_overrides = extract_macro_overrides(
                message, macro_actions=COMMON_REQUEST_MACRO_ACTIONS
            )
        if macros_overrides:
            Logger.debug(f"Perplexity macros applied: {macros_overrides}")

        effective_settings = self._resolve_request_settings(resolved_model, overrides=macros_overrides)
        ui_model_label = str(
            effective_settings.get("model_label") or self._get_model_label_for_request(resolved_model)
        )
        self.current_send_deepthink = bool(effective_settings["send_deepthink"])
        formatted_message, space_instructions_text = self._prepare_space_prompt_payload(
            message_for_formatting,
            sync_space_instructions=bool(effective_settings["sync_space_instructions"]),
        )

        perplexity_extra_prompt_texts: Dict[str, str] = {}
        if bool(effective_settings["use_spaces"]):
            perplexity_extra_prompt_texts["space_instructions"] = space_instructions_text
        text_file_message = ""
        if bool(effective_settings["send_as_text_file"]):
            try:
                text_file_message = str(
                    self.config_manager.get_setting("perplexity_behavior", "text_file_message") or ""
                )
            except Exception:
                text_file_message = ""
            if text_file_message.strip():
                perplexity_extra_prompt_texts["text_file_message"] = text_file_message

        self._capture_diagnostics_prompt_snapshot(
            formatted_message,
            extra_prompt_texts=perplexity_extra_prompt_texts or None,
            metadata={
                "model": resolved_model,
                "ui_model": ui_model_label,
                "subscription_tier": self.subscription_tier,
                "deepthink_enabled": bool(effective_settings["deepthink_enabled"]),
                "send_deepthink": bool(effective_settings["send_deepthink"]),
                "search_enabled": bool(effective_settings["search_enabled"]),
                "send_as_text_file": bool(effective_settings["send_as_text_file"]),
                "use_spaces": bool(effective_settings["use_spaces"]),
                "sync_space_instructions": bool(effective_settings["sync_space_instructions"]),
                "space_instructions_chars": len(space_instructions_text),
            },
        )

        try:
            message_send_timeout = int(
                self.config_manager.get_setting("perplexity_behavior", "message_send_timeout") or 8
            )
        except Exception:
            message_send_timeout = 8

        cdp_session: Any = None
        cdp_listeners_registered = False
        cdp_tasks: set[asyncio.Task] = set()
        request_methods: dict[str, str] = {}
        stream_parsers: dict[str, _PerplexityAnswerStreamParser] = {}

        def _schedule_cdp_task(coro: Any, label: str) -> None:
            try:
                task = asyncio.create_task(coro)
            except Exception as exc:
                Logger.debug(f"Perplexity: failed to schedule CDP handler for {label}: {exc}")
                return

            cdp_tasks.add(task)

            def _on_done(done_task: asyncio.Task) -> None:
                cdp_tasks.discard(done_task)
                try:
                    done_task.exception()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    Logger.debug(f"Perplexity: CDP handler for {label} failed: {exc}")

            task.add_done_callback(_on_done)

        async def finish_stream(
            stream_id: str,
            parser: _PerplexityAnswerStreamParser,
            *,
            aborted: bool = False,
            encountered_error: bool = False,
        ) -> None:
            if not aborted and not encountered_error:
                for content, finish_reason in parser.finish():
                    await self._enqueue_openai_delta(
                        response_queue, content, finish_reason=finish_reason
                    )

            if not aborted and not encountered_error and not parser.emitted_text:
                message = (
                    "Perplexity returned no assistant text. The request may have failed before "
                    "the answer block started."
                )
                Logger.warning(message)
                await response_queue.put({"error": message})
                encountered_error = True

            if not parser.finish_emitted and parser.emitted_text and not aborted and not encountered_error:
                await self._enqueue_openai_delta(response_queue, "", finish_reason="stop")

            await response_queue.put(None)
            stream_parsers.pop(stream_id, None)
            if not aborted and not encountered_error and not self.abort_requested:
                Logger.success("Perplexity CDP stream completed.")

        async def feed_stream_chunk(
            stream_id: str,
            parser: _PerplexityAnswerStreamParser,
            data: bytes,
        ) -> None:
            if self.abort_requested or (abort_event and abort_event.is_set()):
                await finish_stream(stream_id, parser, aborted=True)
                return
            if not data:
                return
            for content, finish_reason in parser.feed(data):
                await self._enqueue_openai_delta(
                    response_queue, content, finish_reason=finish_reason
                )

        async def feed_base64_stream_chunk(
            stream_id: str,
            parser: _PerplexityAnswerStreamParser,
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
                stream_parsers[request_id] = _PerplexityAnswerStreamParser()

            parser = stream_parsers[request_id]
            Logger.info("Teeing Perplexity API response via CDP...")
            Logger.debug(f"Teeing request to: {url}")
            try:
                result = await cdp_session.send(
                    "Network.streamResourceContent",
                    {"requestId": request_id},
                )
            except Exception as exc:
                message = f"Perplexity CDP response streaming failed: {exc}"
                Logger.error(message)
                await response_queue.put({"error": message})
                await finish_stream(request_id, parser, encountered_error=True)
                return

            if isinstance(result, dict):
                await feed_base64_stream_chunk(
                    request_id, parser, result.get("bufferedData")
                )

        async def handle_response_received(params: Any) -> None:
            if not isinstance(params, dict):
                return
            request_id = str(params.get("requestId") or "").strip()
            response = params.get("response")
            if not request_id or not isinstance(response, dict):
                return
            url = str(response.get("url") or "")
            if self.ASK_ROUTE_FRAGMENT not in url:
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
            if not parser:
                return
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
                    "Perplexity CDP stream ended with net::ERR_ABORTED after "
                    "answer data arrived; treating it as complete."
                )
                await finish_stream(request_id, parser)
                return
            message = f"Perplexity CDP stream failed: {error_text}"
            Logger.error(message)
            await response_queue.put({"error": message})
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
                message = f"Perplexity CDP setup failed: {exc}"
                Logger.error(message)
                yield f"data: {json.dumps({'error': message})}\n\n"
                return

            await self.click_new_chat(source="auto")
            if bool(effective_settings["use_spaces"]):
                await self._sync_space_instructions(space_instructions_text)
            await self.set_deepthink_state(
                bool(effective_settings["deepthink_enabled"]),
                model=resolved_model,
            )
            await self.set_search_state(bool(effective_settings["search_enabled"]))
            await asyncio.sleep(0.2)

            if bool(effective_settings["send_as_text_file"]):
                file_payload = build_prompt_text_file_payload(formatted_message)
                uploaded = await self._upload_file(file_payload)
                if uploaded:
                    if text_file_message.strip():
                        await self.enter_message(text_file_message)
                    completion_armed.set()
                    Logger.info("Perplexity: sending request...")
                    upload_timeout = 20
                    try:
                        upload_timeout = int(
                            self.config_manager.get_setting("perplexity_behavior", "file_upload_timeout") or 20
                        )
                    except Exception:
                        upload_timeout = 20
                    await self.send_message(timeout=upload_timeout)
                else:
                    Logger.warning("Perplexity: falling back to pasted text for this request.")
                    await self.enter_message(formatted_message)
                    completion_armed.set()
                    Logger.info("Perplexity: sending request...")
                    await self.send_message(timeout=message_send_timeout)
            else:
                await self.enter_message(formatted_message)
                completion_armed.set()
                Logger.info("Perplexity: sending request...")
                await self.send_message(timeout=message_send_timeout)

            async for item in self._iterate_response_queue(
                response_queue,
                abort_event=abort_event,
                first_chunk_timeout_s=self.INTERCEPT_FIRST_CHUNK_TIMEOUT_S,
                idle_timeout_s=self.PASSIVE_RESPONSE_BODY_TIMEOUT_S,
                on_timeout=lambda: self._click_stop_button(timeout_s=4.0),
                activity_counter=lambda: 1 if completion_started.is_set() else 0,
            ):
                if isinstance(item, dict) and "error" in item:
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                yield item
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
                    Logger.debug(f"Perplexity: CDP detach failed: {exc}")
