import asyncio
import json
import random
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Callable, Literal, Union

from drivers.base_driver import BaseDriver
from drivers.parallel_manager import ParallelDriversManager
from drivers.providers import DriverProvider, is_provider_locked, provider_lock_reason
from drivers.shared_utils import format_request_messages
from remote_control import RemoteControlActions, RemoteControlWeb
from utils.ip_utils import is_ip_address_allowed
from utils.logger import Logger
from utils.model_ids import (
    MODE_CHAT,
    MODE_REASONER,
    build_openai_model_list,
    get_model_ids_for_provider,
    get_owned_by_for_provider,
    get_parallel_model_ids_for_providers,
    is_umm_enabled,
    is_supported_model_id,
    resolve_parallel_provider_from_model_id,
)
from utils.providers_in_parallel import is_parallel_request_queue_feature_enabled

_RATE_LIMIT_LIKE_RE = re.compile(
    r"(rate\s*limit|too\s*many\s*requests|\b429\b|quota|limit\s*reached)",
    flags=re.IGNORECASE,
)
_NON_RETRYABLE_PROVIDER_ERROR_RE = re.compile(
    r"(peak\s*hours|at\s*capacity|model\s*concurrency\s*limit|concurrency\s*limit|"
    r"regeneration[_\s-]*limit|sensitive[_\s-]*query|provider-side\s+sensitive|"
    r"data_inspection_failed|qwen\s+censored)",
    flags=re.IGNORECASE,
)
_TERMINAL_PARTIAL_ERROR_RE = re.compile(
    r"(data_inspection_failed|qwen\s+censored)",
    flags=re.IGNORECASE,
)

DEFAULT_MAX_REQUEST_QUEUE_SIZE = 128
API_CORS_PATH_PREFIX = "/v1/"
API_CORS_ALLOWED_METHODS = ("GET", "POST", "OPTIONS")
API_CORS_ALLOW_METHODS_HEADER = ", ".join(API_CORS_ALLOWED_METHODS)
API_CORS_DEFAULT_ALLOW_HEADERS = "Content-Type, Authorization"
API_CORS_MAX_AGE_SECONDS = "600"
REASONING_EFFORT_DISABLED_VALUES = {
    "",
    "auto",
    "none",
    "off",
    "false",
    "0",
    "no",
    "disable",
    "disabled",
    "minimum",
    "min",
    "minimal",
    "low",
}
REASONING_EFFORT_AISTUDIO_LEVEL_BY_VALUE = {
    "minimum": "minimal",
    "min": "minimal",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "med": "medium",
    "high": "high",
    "max": "high",
    "maximum": "high",
    "xhigh": "high",
    "x-high": "high",
    "extra-high": "high",
    "extra-highest": "high",
}
BEHAVIOR_SUFFIX_RE = re.compile(r"-(auto|chat|reasoner)$", flags=re.IGNORECASE)
AISTUDIO_THINKING_SUFFIX_RE = re.compile(
    r"-(minimal|low|medium|high|r[0-4])$",
    flags=re.IGNORECASE,
)
QueueStateListener = Callable[[], None]
RequestType = Literal["chat", "text"]
DryRunCaptureCallback = Callable[["DryRunCapture"], None]


@dataclass(frozen=True)
class RuntimeExecutionSlot:
    id: str
    provider: DriverProvider
    driver: BaseDriver
    label: str


@dataclass(frozen=True)
class DryRunCapture:
    request_type: RequestType
    raw_json: str
    formatted_text: str
    model: str
    stream: bool
    captured_at: float
    api_key_name: str | None = None


class DryRunDriver:
    """Tiny API runtime stand-in used when Dry Run Mode skips provider launch."""

    def __init__(self, config_manager: Any):
        self.config_manager = config_manager
        self.provider = self._resolve_provider()
        self.provider_label = "Dry Run"
        self.is_running = True

    def _resolve_provider(self) -> DriverProvider:
        try:
            raw_provider = self.config_manager.get_setting("providers_credentials", "provider")
        except Exception:
            raw_provider = None

        provider = DriverProvider.from_setting(raw_provider)
        return provider if provider is not None else DriverProvider.DEEPSEEK

    async def close(self) -> None:
        self.is_running = False

    def request_abort(self) -> None:
        return None


def _parse_sse_json(chunk: Any) -> dict | None:
    if not isinstance(chunk, str):
        return None
    if not chunk.startswith("data:"):
        return None

    data_str = chunk[len("data:") :]
    if data_str.startswith(" "):
        data_str = data_str[1:]
    data_str = data_str.strip()

    if not data_str or data_str == "[DONE]":
        return None

    try:
        parsed = json.loads(data_str)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_error_message(payload: dict) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        return str(msg or "")
    return str(err or "")


def _payload_has_meaningful_content(payload: dict) -> bool:
    try:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return False

        choice0 = choices[0]
        if not isinstance(choice0, dict):
            return False

        delta = choice0.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content.strip():
                return True

        # Non-streaming responses can carry content in 'message'
        message = choice0.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return True
    except Exception:
        return False

    return False


def _is_rate_limit_like_error(message: str) -> bool:
    return bool(_RATE_LIMIT_LIKE_RE.search(str(message or "")))


def _is_non_retryable_provider_error(message: str) -> bool:
    return bool(_NON_RETRYABLE_PROVIDER_ERROR_RE.search(str(message or "")))


def _is_terminal_partial_error(message: str) -> bool:
    return bool(_TERMINAL_PARTIAL_ERROR_RE.search(str(message or "")))


def _make_openai_error_sse_chunk(message: str) -> str:
    error_chunk = {
        "error": {
            "message": str(message or ""),
            "type": "internal_error",
            "param": None,
            "code": None,
        }
    }
    return f"data: {json.dumps(error_chunk)}\n\n"


def _is_api_cors_path(path: Any) -> bool:
    normalized_path = str(path or "")
    return normalized_path.startswith(API_CORS_PATH_PREFIX)


def _build_api_cors_headers(raw_request: Request, *, preflight: bool = False) -> dict[str, str]:
    headers = {"Access-Control-Allow-Origin": "*"}
    if not preflight:
        return headers

    requested_headers = str(raw_request.headers.get("access-control-request-headers") or "").strip()
    headers.update(
        {
            "Access-Control-Allow-Methods": API_CORS_ALLOW_METHODS_HEADER,
            "Access-Control-Allow-Headers": requested_headers or API_CORS_DEFAULT_ALLOW_HEADERS,
            "Access-Control-Max-Age": API_CORS_MAX_AGE_SECONDS,
        }
    )
    return headers

class Message(BaseModel):
    role: str
    content: str
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    messages: List[Message]
    model: str = "deepseek-auto"
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[Any] = None
    reasoning: Optional[Any] = None
    inference_provider: Optional[Any] = None
    huggingchat_inference_provider: Optional[Any] = None
    huggingchat_thinking_effort: Optional[Any] = None


CompletionPromptInput = Union[str, List[str]]
class TextCompletionRequest(BaseModel):
    prompt: CompletionPromptInput
    model: str = "deepseek-auto"
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning_effort: Optional[Any] = None
    reasoning: Optional[Any] = None
    inference_provider: Optional[Any] = None
    huggingchat_inference_provider: Optional[Any] = None
    huggingchat_thinking_effort: Optional[Any] = None


QueuedRequest = Union[ChatCompletionRequest, TextCompletionRequest]


def _normalize_text_completion_prompt(prompt: CompletionPromptInput) -> str:
    if isinstance(prompt, str):
        return prompt

    if not isinstance(prompt, list):
        raise HTTPException(status_code=422, detail="`prompt` must be a string or a list of strings.")

    if not prompt:
        raise HTTPException(status_code=422, detail="`prompt` must not be empty.")

    for item in prompt:
        if not isinstance(item, str):
            raise HTTPException(
                status_code=422,
                detail="Only string prompts are currently supported for `/v1/completions`.",
            )

    if len(prompt) > 1:
        raise HTTPException(
            status_code=400,
            detail="Only a single prompt is currently supported for `/v1/completions`.",
        )

    return prompt[0]


def _normalize_usage_object(usage: Any) -> dict[str, Any]:
    if isinstance(usage, dict):
        usage_obj = dict(usage)
        try:
            usage_obj["prompt_tokens"] = int(usage_obj.get("prompt_tokens") or 0)
        except Exception:
            usage_obj["prompt_tokens"] = 0
        try:
            usage_obj["completion_tokens"] = int(usage_obj.get("completion_tokens") or 0)
        except Exception:
            usage_obj["completion_tokens"] = 0
        try:
            usage_obj["total_tokens"] = int(usage_obj.get("total_tokens") or 0)
        except Exception:
            usage_obj["total_tokens"] = usage_obj["prompt_tokens"] + usage_obj["completion_tokens"]
        return usage_obj

    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _make_text_completion_sse_chunk(parsed: dict, *, default_model: str) -> str | None:
    if "error" in parsed:
        return f"data: {json.dumps(parsed)}\n\n"

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    choice0 = choices[0] if isinstance(choices[0], dict) else {}
    try:
        index = int(choice0.get("index") or 0)
    except Exception:
        index = 0

    text = ""
    delta = choice0.get("delta")
    if isinstance(delta, dict):
        delta_content = delta.get("content")
        if isinstance(delta_content, str):
            text = delta_content

    if not text:
        message = choice0.get("message")
        if isinstance(message, dict):
            message_content = message.get("content")
            if isinstance(message_content, str):
                text = message_content

    finish_reason = choice0.get("finish_reason")
    created = parsed.get("created")
    try:
        created = int(created or 0)
    except Exception:
        created = 0
    if created <= 0:
        created = int(time.time())

    completion_chunk = {
        "id": "cmpl-custom",
        "object": "text_completion",
        "created": created,
        "model": str(parsed.get("model") or default_model or ""),
        "choices": [
            {
                "text": text,
                "index": index,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(completion_chunk)}\n\n"


def _normalize_reasoning_effort(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "high" if value else "off"
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[\s_]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-")


def _extract_reasoning_effort_value(request: Any) -> Any:
    value = getattr(request, "reasoning_effort", None)
    fields_set = getattr(request, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(request, "__fields_set__", None)
    if isinstance(fields_set, set) and "reasoning_effort" in fields_set:
        return value
    if value is not None:
        return value

    reasoning = getattr(request, "reasoning", None)
    if isinstance(reasoning, dict):
        return reasoning.get("effort")

    try:
        return getattr(reasoning, "effort")
    except Exception:
        return None


def _set_openai_sse_chunk_model(chunk: Any, model: Any) -> Any:
    target_model = str(model or "").strip()
    if not target_model:
        return chunk

    parsed = _parse_sse_json(chunk)
    if parsed is None or "error" in parsed:
        return chunk

    parsed["model"] = target_model
    return f"data: {json.dumps(parsed)}\n\n"


@dataclass
class QueueEntry:
    id: str
    queued_at: float
    request: QueuedRequest
    request_type: RequestType
    target_provider: DriverProvider
    response_queue: asyncio.Queue
    abort_event: asyncio.Event
    target_slot_id: str = ""
    target_slot_label: Optional[str] = None
    api_key_name: Optional[str] = None
    driver_model: Optional[str] = None

class RequestQueueFullError(Exception):
    def __init__(self, *, max_size: int, current_size: int):
        super().__init__(f"Request queue is full ({current_size}/{max_size})")
        self.max_size = max_size
        self.current_size = current_size

class RequestQueue:
    def __init__(self, max_size: int = DEFAULT_MAX_REQUEST_QUEUE_SIZE):
        self._items = deque()
        self._condition = asyncio.Condition()
        self._max_size = max(1, int(max_size))
        self._listeners: list[QueueStateListener] = []

    def add_listener(self, listener: QueueStateListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: QueueStateListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as exc:
                Logger.debug(f"RequestQueue listener failed: {exc}")

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def size(self) -> int:
        return len(self._items)

    async def put(self, item: QueueEntry) -> None:
        async with self._condition:
            current_size = len(self._items)
            if current_size >= self._max_size:
                raise RequestQueueFullError(
                    max_size=self._max_size,
                    current_size=current_size,
                )
            self._items.append(item)
            self._condition.notify(1)
        self._notify_listeners()

    async def get(self) -> QueueEntry:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            item = self._items.popleft()
        self._notify_listeners()
        return item

    async def wait_for_item(self) -> None:
        async with self._condition:
            while not self._items:
                await self._condition.wait()

    async def get_nowait(self) -> QueueEntry | None:
        async with self._condition:
            if not self._items:
                return None
            item = self._items.popleft()
        self._notify_listeners()
        return item

    async def snapshot(self) -> list[QueueEntry]:
        async with self._condition:
            return list(self._items)

    async def drain(self) -> list[QueueEntry]:
        async with self._condition:
            drained = list(self._items)
            self._items.clear()
        if drained:
            self._notify_listeners()
        return drained

    async def remove_by_id(self, request_id: str) -> QueueEntry | None:
        normalized_id = str(request_id or "").strip()
        if not normalized_id:
            return None

        async with self._condition:
            if not self._items:
                return None

            removed_entry: QueueEntry | None = None
            kept = deque()
            while self._items:
                item = self._items.popleft()
                if removed_entry is None and str(getattr(item, "id", "") or "") == normalized_id:
                    removed_entry = item
                    continue
                kept.append(item)

            self._items = kept
        if removed_entry is not None:
            self._notify_listeners()
        return removed_entry

    async def remove_by_abort_event(self, abort_event: asyncio.Event) -> bool:
        async with self._condition:
            if not self._items:
                return False

            removed = False
            kept = deque()
            while self._items:
                item = self._items.popleft()
                if (not removed) and item.abort_event is abort_event:
                    removed = True
                    continue
                kept.append(item)

            self._items = kept
        if removed:
            self._notify_listeners()
        return removed

class API:
    def __init__(
        self,
        driver: BaseDriver | ParallelDriversManager | DryRunDriver,
        *,
        remote_actions: RemoteControlActions | None = None,
        dry_run: bool = False,
        dry_run_capture_callback: DryRunCaptureCallback | None = None,
    ):
        self.app = FastAPI()
        self._install_api_cors_middleware()
        self.driver = driver
        self._dry_run_enabled = bool(dry_run)
        self._dry_run_capture_callback = dry_run_capture_callback
        self._queue_state_listeners: list[QueueStateListener] = []
        self._request_queue_listener = self._notify_queue_state_changed
        self._drivers_by_provider: dict[DriverProvider, BaseDriver] = self._build_runtime_drivers_map()
        self._execution_slots = self._build_execution_slots()
        self._execution_slots_by_id: dict[str, RuntimeExecutionSlot] = {
            slot.id: slot for slot in self._execution_slots
        }
        self._execution_slots_by_provider: dict[DriverProvider, list[RuntimeExecutionSlot]] = {}
        for slot in self._execution_slots:
            self._execution_slots_by_provider.setdefault(slot.provider, []).append(slot)

        self._request_queues_by_slot_id: dict[str, RequestQueue] = {
            slot.id: RequestQueue() for slot in self._execution_slots
        }
        for queue in self._request_queues_by_slot_id.values():
            queue.add_listener(self._request_queue_listener)

        self._global_processing_lock = asyncio.Lock()
        self._parallel_request_queue_enabled = (
            (not self._dry_run_enabled)
            and is_parallel_request_queue_feature_enabled(getattr(self.driver, "config_manager", None))
            and len(self._execution_slots) >= 2
        )

        self.request_queue = next(iter(self._request_queues_by_slot_id.values()), RequestQueue())
        self.current_entry: Optional[QueueEntry] = None
        self.current_abort_event: asyncio.Event = None
        self.current_entries_by_slot_id: dict[str, QueueEntry] = {}
        self.current_abort_events_by_slot_id: dict[str, asyncio.Event] = {}
        self._slot_last_used_order: dict[str, int] = {}
        self._slot_use_counter = 0
        self.remote_control: RemoteControlWeb | None = None
        if remote_actions is not None:
            self.remote_control = RemoteControlWeb(
                getattr(self.driver, "config_manager", None),
                enforce_ip_whitelist=self._enforce_ip_whitelist,
                actions=remote_actions,
            )
        self.setup_routes()
        if self.remote_control is not None:
            self.remote_control.register_routes(self.app)
        if self._dry_run_enabled:
            self.worker_tasks = {}
        else:
            self.start_worker()

    def _install_api_cors_middleware(self) -> None:
        @self.app.middleware("http")
        async def api_cors_middleware(raw_request: Request, call_next):
            if not _is_api_cors_path(raw_request.url.path):
                return await call_next(raw_request)

            origin = raw_request.headers.get("origin")
            if not origin:
                return await call_next(raw_request)

            is_preflight = (
                raw_request.method.upper() == "OPTIONS"
                and raw_request.headers.get("access-control-request-method")
            )
            if is_preflight:
                requested_method = str(
                    raw_request.headers.get("access-control-request-method") or ""
                ).upper()
                headers = _build_api_cors_headers(raw_request, preflight=True)
                if requested_method not in API_CORS_ALLOWED_METHODS:
                    return Response(
                        "Disallowed CORS method",
                        status_code=400,
                        media_type="text/plain",
                        headers=headers,
                    )
                return Response(
                    "OK",
                    status_code=200,
                    media_type="text/plain",
                    headers=headers,
                )

            response = await call_next(raw_request)
            response.headers.update(_build_api_cors_headers(raw_request))
            return response

    def _build_runtime_drivers_map(self) -> dict[DriverProvider, BaseDriver]:
        if isinstance(self.driver, ParallelDriversManager):
            drivers_by_provider: dict[DriverProvider, BaseDriver] = {}
            for provider, driver in self.driver.iter_drivers():
                if driver is not None:
                    drivers_by_provider.setdefault(provider, driver)
            return drivers_by_provider

        provider = getattr(self.driver, "provider", None)
        effective_provider = provider if isinstance(provider, DriverProvider) else DriverProvider.DEEPSEEK
        return {effective_provider: self.driver}

    def _build_execution_slots(self) -> list[RuntimeExecutionSlot]:
        slots: list[RuntimeExecutionSlot] = []
        slot_counts_by_provider: dict[DriverProvider, int] = {}

        if isinstance(self.driver, ParallelDriversManager):
            driver_entries = self.driver.iter_drivers()
        else:
            driver_entries = list(self._drivers_by_provider.items())

        for provider, driver in driver_entries:
            next_index = slot_counts_by_provider.get(provider, 0) + 1
            slot_counts_by_provider[provider] = next_index

            label = provider.value if next_index == 1 else f"{provider.value} {next_index}"
            slots.append(
                RuntimeExecutionSlot(
                    id=f"{provider.key}:{next_index}",
                    provider=provider,
                    driver=driver,
                    label=label,
                )
            )

        return slots

    def _is_multi_provider_runtime(self) -> bool:
        return len(self._drivers_by_provider) >= 2

    def _get_default_slot(self) -> RuntimeExecutionSlot:
        if self._execution_slots:
            return self._execution_slots[0]
        return RuntimeExecutionSlot(
            id=f"{DriverProvider.DEEPSEEK.key}:1",
            provider=DriverProvider.DEEPSEEK,
            driver=self.driver,
            label=DriverProvider.DEEPSEEK.value,
        )

    def _get_execution_slot(self, slot_id: str) -> RuntimeExecutionSlot:
        slot = self._execution_slots_by_id.get(slot_id)
        if slot is None:
            raise KeyError(f"No execution slot is registered for slot: {slot_id}")
        return slot

    def _get_execution_slots_for_provider(self, provider: DriverProvider) -> list[RuntimeExecutionSlot]:
        return list(self._execution_slots_by_provider.get(provider) or [])

    def _get_request_queue_for_slot(self, slot_id: str) -> RequestQueue:
        queue = self._request_queues_by_slot_id.get(slot_id)
        if queue is None:
            raise KeyError(f"No request queue is registered for slot: {slot_id}")
        return queue

    def _get_driver_for_provider(self, provider: DriverProvider) -> BaseDriver:
        driver = self._drivers_by_provider.get(provider)
        if driver is None:
            raise KeyError(f"No runtime driver is registered for provider: {provider.value}")
        return driver

    def _get_api_real_model_labels(self, provider: DriverProvider) -> list[str]:
        try:
            driver = self._get_driver_for_provider(provider)
        except Exception:
            return []

        getter = getattr(driver, "api_real_model_labels", None)
        if not callable(getter):
            return []

        try:
            labels = getter()
        except Exception as exc:
            Logger.debug(f"{provider.value}: failed to read API real model labels: {exc}")
            return []

        out: list[str] = []
        for label in labels or []:
            safe = str(label or "").strip()
            if safe:
                out.append(safe)
        return out

    def _get_api_real_model_labels_by_provider(
        self,
        providers: list[DriverProvider] | None = None,
    ) -> dict[DriverProvider, list[str]]:
        runtime_providers = providers or list(self._drivers_by_provider.keys())
        return {
            provider: self._get_api_real_model_labels(provider)
            for provider in runtime_providers
        }

    def _get_request_queue_for_provider(self, provider: DriverProvider) -> RequestQueue:
        slots = self._get_execution_slots_for_provider(provider)
        if not slots:
            raise KeyError(f"No request queue is registered for provider: {provider.value}")
        return self._get_request_queue_for_slot(slots[0].id)

    def _get_active_loadout_name_for_provider(self, provider: DriverProvider) -> str | None:
        try:
            driver = self._get_driver_for_provider(provider)
        except Exception:
            driver = None

        config_view = getattr(driver, "config_manager", None)
        getter = getattr(config_view, "get_active_loadout_name", None)
        if callable(getter):
            try:
                return getter(runtime=True)
            except Exception:
                pass

        shared_config = getattr(self.driver, "config_manager", None)
        fallback = getattr(shared_config, "get_runtime_active_loadout_name", None)
        if callable(fallback):
            try:
                return fallback(provider)
            except Exception:
                pass

        return None

    def _get_default_provider(self) -> DriverProvider:
        return self._get_default_slot().provider

    def _ensure_provider_unlocked(self, provider: DriverProvider) -> None:
        cfg = getattr(self.driver, "config_manager", None)
        if not is_provider_locked(provider, cfg):
            return

        detail = provider_lock_reason(provider) or f"{provider.value} is currently locked."
        raise HTTPException(status_code=403, detail=detail)

    @staticmethod
    def _slot_can_handle_request_model(slot: RuntimeExecutionSlot, model: Any) -> bool:
        checker = getattr(slot.driver, "can_handle_request_model", None)
        if not callable(checker):
            return True

        try:
            return bool(checker(model))
        except Exception as exc:
            Logger.debug(
                f"{slot.label}: failed to check account-specific model availability: {exc}"
            )
            return True

    def _select_execution_slot_for_provider(
        self,
        provider: DriverProvider,
        model: Any = None,
    ) -> RuntimeExecutionSlot:
        self._ensure_provider_unlocked(provider)
        all_slots = self._get_execution_slots_for_provider(provider)
        if not all_slots:
            raise KeyError(f"No execution slot is registered for provider: {provider.value}")

        slots = [slot for slot in all_slots if bool(getattr(slot.driver, "is_running", False))]
        if not slots:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"No running {provider.value} instance is available. "
                    "Restart services to relaunch this provider."
                ),
            )

        if model is not None:
            model_capable_slots = [
                slot for slot in slots
                if self._slot_can_handle_request_model(slot, model)
            ]
            if model_capable_slots:
                slots = model_capable_slots

        if len(slots) == 1:
            selected = slots[0]
        else:
            def load_for_slot(slot: RuntimeExecutionSlot) -> int:
                queue = self._request_queues_by_slot_id.get(slot.id)
                queued = int(getattr(queue, "size", 0) or 0)
                processing = 1 if slot.id in self.current_entries_by_slot_id else 0
                return queued + processing

            min_load = min(load_for_slot(slot) for slot in slots)
            least_loaded = [slot for slot in slots if load_for_slot(slot) == min_load]
            unused = [slot for slot in least_loaded if slot.id not in self._slot_last_used_order]
            if unused:
                selected = random.choice(unused)
            else:
                oldest_order = min(
                    self._slot_last_used_order.get(slot.id, 0) for slot in least_loaded
                )
                oldest = [
                    slot
                    for slot in least_loaded
                    if self._slot_last_used_order.get(slot.id, 0) == oldest_order
                ]
                selected = random.choice(oldest)

        self._slot_use_counter += 1
        self._slot_last_used_order[selected.id] = self._slot_use_counter
        return selected

    def _resolve_request_provider(self, model: Any) -> DriverProvider:
        if not self._is_multi_provider_runtime():
            provider = self._get_default_provider()
            self._ensure_provider_unlocked(provider)
            return provider

        cfg = getattr(self.driver, "config_manager", None)
        runtime_providers = list(self._drivers_by_provider.keys())
        unlocked_runtime_providers = [
            provider for provider in runtime_providers if not is_provider_locked(provider, cfg)
        ]
        locked_runtime_providers = [
            provider for provider in runtime_providers if is_provider_locked(provider, cfg)
        ]
        real_model_labels_by_provider = self._get_api_real_model_labels_by_provider()
        provider = resolve_parallel_provider_from_model_id(
            model,
            unlocked_runtime_providers,
            config_manager=cfg,
            real_model_labels_by_provider=real_model_labels_by_provider,
        )
        if provider is None:
            locked_provider = resolve_parallel_provider_from_model_id(
                model,
                locked_runtime_providers,
                config_manager=cfg,
                real_model_labels_by_provider=real_model_labels_by_provider,
            )
            if locked_provider is not None:
                self._ensure_provider_unlocked(locked_provider)

            real_model_detail = ""
            if is_umm_enabled(cfg):
                real_model_detail = (
                    " Universal Model Names can also expose real-model IDs here; "
                    "only exact conflicts get provider-prefixed forms like "
                    "`aistudio-gemini-3-1-pro-reasoner`."
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Providers in Parallel requires routable model IDs, such as provider-prefixed "
                    "behavior IDs "
                    "(for example `deepseek-auto` or `glm-chat`). "
                    "Universal model names like `intenserp-auto` are not valid here."
                    f"{real_model_detail}"
                ),
            )

        if provider not in self._drivers_by_provider:
            raise HTTPException(
                status_code=400,
                detail=f"Provider `{provider.value}` is not enabled in Providers in Parallel.",
            )

        self._ensure_provider_unlocked(provider)
        return provider

    def _resolve_request_slot(self, model: Any) -> RuntimeExecutionSlot:
        provider = self._resolve_request_provider(model)
        return self._select_execution_slot_for_provider(provider, model)

    def _ensure_supported_model_id(self, model: Any, provider: DriverProvider) -> None:
        normalized = str(model or "").strip()
        cfg = getattr(self.driver, "config_manager", None)
        real_model_labels = self._get_api_real_model_labels(provider)
        if is_supported_model_id(
            provider,
            normalized,
            cfg,
            real_model_labels=real_model_labels,
        ):
            return

        if self._is_multi_provider_runtime():
            real_model_labels_by_provider = self._get_api_real_model_labels_by_provider()
            supported_ids = [
                model_id
                for item_provider, model_id in get_parallel_model_ids_for_providers(
                    list(self._drivers_by_provider.keys()) or [provider],
                    cfg,
                    real_model_labels_by_provider=real_model_labels_by_provider,
                )
                if item_provider == provider
            ]
        else:
            supported_ids = get_model_ids_for_provider(
                provider,
                cfg,
                real_model_labels=real_model_labels,
            )
        detail = (
            f"Unsupported model `{normalized or '<empty>'}` for provider `{provider.value}`. "
            f"Supported IDs: {', '.join(f'`{model_id}`' for model_id in supported_ids)}."
        )
        if (not self._is_multi_provider_runtime()) and is_umm_enabled(cfg):
            detail += " Provider-prefixed IDs are also still accepted."
        if provider == DriverProvider.AI_STUDIO:
            detail += (
                " Google AI Studio also accepts legacy IDs with Thinking Level suffixes like "
                "`aistudio-auto-high` or `aistudio-auto-r4`."
            )

        raise HTTPException(status_code=400, detail=detail)

    @staticmethod
    def _run_model_availability_validator(
        model: Any,
        slot: RuntimeExecutionSlot,
        validator_name: str,
    ) -> None:
        validator = getattr(slot.driver, validator_name, None)
        if not callable(validator):
            return

        try:
            validator(model)
        except HTTPException:
            raise
        except Exception as exc:
            detail = str(exc) or f"Model `{model or '<empty>'}` is not available."
            raise HTTPException(status_code=400, detail=detail) from exc

    @classmethod
    def _ensure_request_model_available(cls, model: Any, slot: RuntimeExecutionSlot) -> None:
        cls._run_model_availability_validator(
            model,
            slot,
            "validate_request_model_available",
        )

    @classmethod
    def _ensure_explicit_request_model_available(cls, model: Any, slot: RuntimeExecutionSlot) -> None:
        cls._run_model_availability_validator(
            model,
            slot,
            "validate_explicit_request_model_available",
        )

    def _accept_api_reasoning_effort(self) -> bool:
        cfg = getattr(self.driver, "config_manager", None)
        if cfg is None:
            return True

        try:
            value = cfg.get_setting("network_settings", "accept_reasoning_effort")
        except Exception:
            return True

        if value is None:
            return True
        return bool(value)

    def _provider_accepts_api_reasoning_effort(self, provider: DriverProvider) -> bool:
        if not self._accept_api_reasoning_effort():
            return False

        cfg = getattr(self.driver, "config_manager", None)
        if cfg is None:
            return True

        try:
            raw_values = cfg.get_setting("network_settings", "accept_reasoning_effort_providers")
        except Exception:
            return True

        if raw_values is None:
            return True
        if isinstance(raw_values, str):
            values = [raw_values]
        elif isinstance(raw_values, (list, tuple, set)):
            values = list(raw_values)
        else:
            return True

        allowed: set[DriverProvider] = set()
        for raw_value in values:
            text = str(raw_value or "").strip()
            if not text:
                continue
            resolved = DriverProvider.from_setting(text)
            if resolved is not None:
                allowed.add(resolved)

        return provider in allowed

    @staticmethod
    def _replace_behavior_suffix(model: Any, mode: str) -> str:
        normalized_mode = str(mode or "").strip().lower()
        current = str(model or "").strip()
        if not current or normalized_mode not in {MODE_CHAT, MODE_REASONER}:
            return current

        replaced = BEHAVIOR_SUFFIX_RE.sub(f"-{normalized_mode}", current)
        return replaced if replaced != current else current

    @staticmethod
    def _strip_aistudio_thinking_suffix(model: Any) -> str:
        return AISTUDIO_THINKING_SUFFIX_RE.sub("", str(model or "").strip())

    @staticmethod
    def _aistudio_level_for_reasoning_effort(effort: str) -> str:
        if effort in {
            "",
            "auto",
            "none",
            "off",
            "false",
            "0",
            "no",
            "disable",
            "disabled",
        }:
            return ""
        return REASONING_EFFORT_AISTUDIO_LEVEL_BY_VALUE.get(effort, "high")

    @staticmethod
    def _reasoning_effort_enables_reasoning(effort: str) -> bool:
        return effort not in REASONING_EFFORT_DISABLED_VALUES

    @staticmethod
    def _huggingchat_effort_for_reasoning_effort(effort: str) -> str:
        if effort in {"", "auto"}:
            return ""
        if effort in {
            "none",
            "off",
            "false",
            "0",
            "no",
            "disable",
            "disabled",
        }:
            return "default"
        if effort in {"minimum", "min", "minimal"}:
            return "low"
        if effort in {"low", "medium", "med", "high"}:
            return "medium" if effort == "med" else effort
        if effort in {"max", "maximum", "xhigh", "x-high", "extra-high"}:
            return "high"
        return effort

    GLM_REASONING_EFFORT_DISABLED_VALUES = {
        "",
        "auto",
        "none",
        "off",
        "false",
        "0",
        "no",
        "disable",
        "disabled",
    }

    @classmethod
    def _glm_deepthink_effort_for_reasoning_effort(cls, effort: str) -> str:
        if effort in cls.GLM_REASONING_EFFORT_DISABLED_VALUES:
            return ""
        if effort in {"max", "maximum", "xhigh", "x-high", "extra-high", "extra-highest"}:
            return "max"
        if effort in {"low", "min", "minimum", "minimal"}:
            return "low"
        return "high"

    @classmethod
    def _glm_reasoning_effort_enables_reasoning(cls, effort: str) -> bool:
        return bool(cls._glm_deepthink_effort_for_reasoning_effort(effort))

    @classmethod
    def _huggingchat_reasoning_effort_enables_reasoning(cls, effort: str) -> bool:
        return cls._huggingchat_effort_for_reasoning_effort(effort) not in {"", "default"}

    def _resolve_driver_model_for_request(
        self,
        request: QueuedRequest,
        provider: DriverProvider,
    ) -> str:
        requested_model = str(getattr(request, "model", "") or "").strip()
        if not self._provider_accepts_api_reasoning_effort(provider):
            return requested_model

        effort = _normalize_reasoning_effort(_extract_reasoning_effort_value(request))

        if provider == DriverProvider.AI_STUDIO:
            base_model = self._strip_aistudio_thinking_suffix(requested_model)
            level = self._aistudio_level_for_reasoning_effort(effort)
            if not level:
                return self._replace_behavior_suffix(base_model, MODE_CHAT)
            reasoner_model = self._replace_behavior_suffix(base_model, MODE_REASONER)
            return f"{reasoner_model}-{level}" if reasoner_model else reasoner_model

        if provider == DriverProvider.GLM_CHAT:
            mode = (
                MODE_REASONER
                if self._glm_reasoning_effort_enables_reasoning(effort)
                else MODE_CHAT
            )
            return self._replace_behavior_suffix(requested_model, mode)

        if provider == DriverProvider.HUGGINGCHAT:
            mode = (
                MODE_REASONER
                if self._huggingchat_reasoning_effort_enables_reasoning(effort)
                else MODE_CHAT
            )
            return self._replace_behavior_suffix(requested_model, mode)

        mode = MODE_REASONER if self._reasoning_effort_enables_reasoning(effort) else MODE_CHAT
        return self._replace_behavior_suffix(requested_model, mode)

    def _extract_provider_request_overrides(
        self,
        request: QueuedRequest,
        provider: DriverProvider,
    ) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        if provider == DriverProvider.GLM_CHAT:
            if self._provider_accepts_api_reasoning_effort(provider):
                effort = _normalize_reasoning_effort(_extract_reasoning_effort_value(request))
                deepthink_effort = self._glm_deepthink_effort_for_reasoning_effort(effort)
                if deepthink_effort:
                    overrides["deepthink_effort"] = deepthink_effort
            return overrides

        if provider != DriverProvider.HUGGINGCHAT:
            return overrides

        for attr_name in (
            "inference_provider",
            "huggingchat_inference_provider",
            "huggingchat_thinking_effort",
        ):
            value = getattr(request, attr_name, None)
            if value is None:
                continue
            overrides[attr_name] = value

        if "huggingchat_inference_provider" in overrides:
            overrides["inference_provider"] = overrides["huggingchat_inference_provider"]
        if "huggingchat_thinking_effort" in overrides:
            overrides["thinking_effort"] = overrides["huggingchat_thinking_effort"]
        elif self._provider_accepts_api_reasoning_effort(provider):
            effort = _normalize_reasoning_effort(_extract_reasoning_effort_value(request))
            thinking_effort = self._huggingchat_effort_for_reasoning_effort(effort)
            if thinking_effort:
                overrides["thinking_effort"] = thinking_effort
                overrides["deepthink_enabled"] = (
                    self._huggingchat_reasoning_effort_enables_reasoning(effort)
                )
        return overrides

    def _find_processing_slot_id_for_abort_event(
        self, abort_event: asyncio.Event | None
    ) -> str | None:
        if abort_event is None:
            return None

        for slot_id, current_abort_event in self.current_abort_events_by_slot_id.items():
            if current_abort_event is abort_event:
                return slot_id

        return None

    def _find_processing_provider_for_abort_event(
        self, abort_event: asyncio.Event | None
    ) -> DriverProvider | None:
        slot_id = self._find_processing_slot_id_for_abort_event(abort_event)
        if slot_id is None:
            return None

        try:
            return self._get_execution_slot(slot_id).provider
        except Exception:
            return None

    def _request_abort_for_abort_event(self, abort_event: asyncio.Event | None) -> None:
        slot_id = self._find_processing_slot_id_for_abort_event(abort_event)
        if slot_id is None:
            return

        try:
            self._get_execution_slot(slot_id).driver.request_abort()
        except Exception:
            pass

    async def _remove_queued_request_by_abort_event(self, abort_event: asyncio.Event) -> bool:
        for queue in self._request_queues_by_slot_id.values():
            try:
                removed = await queue.remove_by_abort_event(abort_event)
            except Exception:
                removed = False
            if removed:
                return True

        return False

    async def snapshot_requests(self) -> list[tuple[str, QueueEntry]]:
        entries: list[tuple[str, QueueEntry]] = []

        for slot in self._execution_slots:
            current = self.current_entries_by_slot_id.get(slot.id)
            if current is not None:
                current_status = "cancelled" if current.abort_event.is_set() else "processing"
                entries.append((current_status, current))

        for slot in self._execution_slots:
            queue = self._get_request_queue_for_slot(slot.id)
            queued = await queue.snapshot()
            for entry in queued:
                status = "cancelled" if entry.abort_event.is_set() else "pending"
                entries.append((status, entry))

        def _sort_key(item: tuple[str, QueueEntry]) -> tuple[int, float, str]:
            status, entry = item
            status_order = 0 if status in {"processing", "cancelled"} else 1
            queued_at = float(getattr(entry, "queued_at", 0.0) or 0.0)
            entry_id = str(getattr(entry, "id", "") or "")
            return (status_order, queued_at, entry_id)

        return sorted(entries, key=_sort_key)

    def add_queue_state_listener(self, listener: QueueStateListener) -> None:
        if listener not in self._queue_state_listeners:
            self._queue_state_listeners.append(listener)

    def remove_queue_state_listener(self, listener: QueueStateListener) -> None:
        try:
            self._queue_state_listeners.remove(listener)
        except ValueError:
            pass

    def _notify_queue_state_changed(self) -> None:
        for listener in list(self._queue_state_listeners):
            try:
                listener()
            except Exception as exc:
                Logger.debug(f"Queue state listener failed: {exc}")

    async def _close_entry_with_message(self, entry: QueueEntry, message: str) -> None:
        abort_event = getattr(entry, "abort_event", None)
        if abort_event is not None:
            try:
                abort_event.set()
            except Exception:
                pass

        try:
            await entry.response_queue.put(_make_openai_error_sse_chunk(message))
        except Exception:
            pass

        try:
            await entry.response_queue.put(None)
        except Exception:
            pass

    async def abort_current_request(self, reason: str | None = None) -> bool:
        current_slot_ids = list(self.current_entries_by_slot_id.keys())
        if not current_slot_ids:
            return False

        message = (reason or "Request aborted.").strip() or "Request aborted."
        aborted_any = False

        for slot_id in current_slot_ids:
            aborted = await self.abort_current_request_for_slot(slot_id, reason=message)
            aborted_any = aborted_any or aborted

        return aborted_any

    async def abort_current_request_for_provider(
        self,
        provider: DriverProvider,
        reason: str | None = None,
    ) -> bool:
        message = (reason or "Request aborted.").strip() or "Request aborted."
        aborted_any = False

        for slot in self._get_execution_slots_for_provider(provider):
            aborted = await self.abort_current_request_for_slot(slot.id, reason=message)
            aborted_any = aborted_any or aborted

        return aborted_any

    async def abort_current_request_for_slot(
        self,
        slot_id: str,
        reason: str | None = None,
    ) -> bool:
        entry = self.current_entries_by_slot_id.get(slot_id)
        if entry is None:
            return False

        message = (reason or "Request aborted.").strip() or "Request aborted."
        abort_event = getattr(entry, "abort_event", None)
        if abort_event is not None:
            try:
                abort_event.set()
            except Exception:
                pass

        try:
            self._get_execution_slot(slot_id).driver.request_abort()
        except Exception:
            pass

        await self._close_entry_with_message(entry, message)
        self._notify_queue_state_changed()
        return True

    async def cancel_request(self, request_id: str, reason: str | None = None) -> bool:
        normalized_id = str(request_id or "").strip()
        if not normalized_id:
            return False

        message = (reason or "Request cancelled.").strip() or "Request cancelled."

        for slot_id, entry in list(self.current_entries_by_slot_id.items()):
            if str(getattr(entry, "id", "") or "") != normalized_id:
                continue
            return await self.abort_current_request_for_slot(slot_id, reason=message)

        for queue in self._request_queues_by_slot_id.values():
            try:
                removed_entry = await queue.remove_by_id(normalized_id)
            except Exception:
                removed_entry = None
            if removed_entry is None:
                continue

            await self._close_entry_with_message(removed_entry, message)
            self._notify_queue_state_changed()
            return True

        return False

    async def cancel_queued_requests(self, reason: str | None = None) -> int:
        message = (reason or "Request cancelled.").strip() or "Request cancelled."

        cancelled = 0
        for queue in self._request_queues_by_slot_id.values():
            try:
                entries = await queue.drain()
            except Exception:
                entries = []

            for entry in entries:
                await self._close_entry_with_message(entry, message)
                cancelled += 1

        return cancelled

    def is_provider_processing(self, provider: DriverProvider) -> bool:
        for slot in self._get_execution_slots_for_provider(provider):
            if slot.id in self.current_entries_by_slot_id:
                return True
        return False

    async def cancel_queued_requests_for_provider(
        self,
        provider: DriverProvider,
        reason: str | None = None,
    ) -> int:
        message = (reason or "Request cancelled.").strip() or "Request cancelled."
        cancelled = 0

        for slot in self._get_execution_slots_for_provider(provider):
            queue = self._get_request_queue_for_slot(slot.id)
            try:
                entries = await queue.drain()
            except Exception:
                entries = []

            for entry in entries:
                await self._close_entry_with_message(entry, message)
                cancelled += 1

        if cancelled:
            self._notify_queue_state_changed()
        return cancelled

    def _authenticate_request(self, raw_request: Request) -> Optional[str]:
        cfg = getattr(self.driver, "config_manager", None)
        if not cfg or not cfg.get_setting("network_settings", "use_api_keys"):
            return None

        auth_header = raw_request.headers.get("Authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing API key")

        token = auth_header.split(" ", 1)[1].strip()
        pairs = cfg.get_setting("network_settings", "api_keys") or []

        matched_name = None
        for p in pairs:
            name = ""
            key_val = ""
            if isinstance(p, dict):
                name = str(p.get("name", ""))
                key_val = str(p.get("key", ""))
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                name = str(p[0])
                key_val = str(p[1])

            if key_val and token == key_val:
                matched_name = name or "Unnamed"
                break

        if not matched_name:
            raise HTTPException(status_code=401, detail="Invalid API key")

        Logger.info(f"Authenticated request using API key: {matched_name}")
        return matched_name

    def _enforce_ip_whitelist(self, raw_request: Request) -> Optional[str]:
        cfg = getattr(self.driver, "config_manager", None)
        if not cfg or not cfg.get_setting("network_settings", "use_ip_whitelist"):
            return None

        client = getattr(raw_request, "client", None)
        client_host = str(getattr(client, "host", "") or "").strip()
        if not client_host:
            Logger.warning("Blocked request: could not determine client IP while IP whitelist is enabled.")
            raise HTTPException(status_code=403, detail="Client IP not allowed")

        allowed_ips = cfg.get_setting("network_settings", "ip_whitelist") or []
        if not is_ip_address_allowed(client_host, allowed_ips):
            Logger.warning(f"Blocked request from IP {client_host}: not in whitelist.")
            raise HTTPException(status_code=403, detail="Client IP not allowed")

        return client_host

    def _authorize_request(self, raw_request: Request) -> Optional[str]:
        self._enforce_ip_whitelist(raw_request)
        return self._authenticate_request(raw_request)

    @staticmethod
    def _request_model_payload(request: BaseModel) -> Any:
        dumper = getattr(request, "model_dump", None)
        if callable(dumper):
            try:
                return dumper(mode="json")
            except TypeError:
                return dumper()

        legacy_dumper = getattr(request, "dict", None)
        if callable(legacy_dumper):
            return legacy_dumper()
        return {}

    async def _read_raw_request_payload(self, raw_request: Request, request: BaseModel) -> Any:
        try:
            body = await raw_request.body()
        except Exception:
            body = b""

        if body:
            try:
                return json.loads(body.decode("utf-8"))
            except Exception:
                try:
                    return json.loads(body)
                except Exception:
                    pass

        return self._request_model_payload(request)

    def _format_dry_run_request(self, request: QueuedRequest, request_type: RequestType) -> str:
        cfg = getattr(self.driver, "config_manager", None)
        if cfg is None:
            return ""

        try:
            if request_type == "text":
                prompt = _normalize_text_completion_prompt(getattr(request, "prompt", ""))
                return format_request_messages(cfg, prompt)
            return format_request_messages(cfg, getattr(request, "messages", []))
        except Exception as exc:
            Logger.warning(f"Dry Run Mode: failed to format captured request: {exc}")
            return f"Failed to format request: {exc}"

    async def _capture_dry_run_request(
        self,
        request: QueuedRequest,
        raw_request: Request,
        *,
        request_type: RequestType,
        api_key_name: str | None,
    ) -> None:
        raw_payload = await self._read_raw_request_payload(raw_request, request)
        try:
            raw_json = json.dumps(raw_payload, indent=2, ensure_ascii=False)
        except Exception:
            raw_json = json.dumps(self._request_model_payload(request), indent=2, ensure_ascii=False)

        formatted_text = self._format_dry_run_request(request, request_type)
        capture = DryRunCapture(
            request_type=request_type,
            raw_json=raw_json,
            formatted_text=formatted_text,
            model=str(getattr(request, "model", "") or ""),
            stream=bool(getattr(request, "stream", False)),
            captured_at=time.time(),
            api_key_name=api_key_name,
        )

        callback = self._dry_run_capture_callback
        if callable(callback):
            try:
                callback(capture)
            except Exception as exc:
                Logger.warning(f"Dry Run Mode: failed to update display window: {exc}")

        Logger.info(
            "Dry Run Mode captured "
            f"{request_type} request for model `{capture.model or '<empty>'}`."
        )
        raise HTTPException(
            status_code=418,
            detail=(
                "I'm a teapot: Dry Run Mode captured the request. "
                "It is now inspectable in the Dry Run Display window."
            ),
        )

    def setup_routes(self):
        @self.app.get("/v1/models")
        async def list_models(raw_request: Request):
            self._authorize_request(raw_request)

            cfg = getattr(self.driver, "config_manager", None)
            all_runtime_providers = list(self._drivers_by_provider.keys()) or [DriverProvider.DEEPSEEK]
            if self._dry_run_enabled:
                runtime_providers = all_runtime_providers
            else:
                runtime_providers = [
                    provider
                    for provider in all_runtime_providers
                    if not is_provider_locked(provider, cfg)
                ]
            if not runtime_providers:
                return {
                    "object": "list",
                    "data": [],
                }

            if self._is_multi_provider_runtime():
                model_data = []
                real_model_labels_by_provider = self._get_api_real_model_labels_by_provider(
                    runtime_providers,
                )
                for provider, model_id in get_parallel_model_ids_for_providers(
                    runtime_providers,
                    cfg,
                    real_model_labels_by_provider=real_model_labels_by_provider,
                ):
                    model_data.extend(
                        build_openai_model_list(
                            [model_id],
                            owned_by=get_owned_by_for_provider(provider),
                        )
                    )
                return {
                    "object": "list",
                    "data": model_data,
                }

            effective_provider = runtime_providers[0]
            model_ids = get_model_ids_for_provider(
                effective_provider,
                cfg,
                real_model_labels=self._get_api_real_model_labels(effective_provider),
            )

            return {
                "object": "list",
                "data": build_openai_model_list(
                    model_ids,
                    owned_by=get_owned_by_for_provider(effective_provider),
                ),
            }

        @self.app.post("/v1/chat/completions")
        async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
            # Optional API key authentication (Bearer token)
            api_key_name = self._authorize_request(raw_request)

            if self._dry_run_enabled:
                await self._capture_dry_run_request(
                    request,
                    raw_request,
                    request_type="chat",
                    api_key_name=api_key_name,
                )

            if not self.driver.is_running:
                raise HTTPException(status_code=503, detail="Driver is not running")

            target_slot = self._resolve_request_slot(request.model)
            target_provider = target_slot.provider
            self._ensure_explicit_request_model_available(request.model, target_slot)
            self._ensure_supported_model_id(request.model, target_provider)
            driver_model = self._resolve_driver_model_for_request(request, target_provider)
            self._ensure_request_model_available(driver_model, target_slot)

            # Log incoming request
            msg_count = len(request.messages)
            stream_mode = "streaming" if request.stream else "non-streaming"
            loadout_name = self._get_active_loadout_name_for_provider(target_provider)
            Logger.info(
                f"Received chat completion request for {target_provider.value} "
                f"({msg_count} messages, {stream_mode})"
                + (f" using loadout '{loadout_name}'" if loadout_name else "")
            )

            # Create a queue for the response chunks
            response_queue = asyncio.Queue()
            
            # Create an abort event for this request
            abort_event = asyncio.Event()
            
            # Put the request, response queue, and abort event into the main request queue
            entry = QueueEntry(
                id=secrets.token_hex(4),
                queued_at=time.time(),
                request=request,
                request_type="chat",
                target_provider=target_provider,
                response_queue=response_queue,
                abort_event=abort_event,
                target_slot_id=target_slot.id,
                target_slot_label=target_slot.label,
                api_key_name=api_key_name,
                driver_model=driver_model,
            )
            try:
                await self._get_request_queue_for_slot(target_slot.id).put(entry)
            except RequestQueueFullError as exc:
                Logger.warning(
                    "Rejecting chat completion request because the request queue is full "
                    f"({exc.current_size}/{exc.max_size} pending requests)."
                )
                raise HTTPException(
                    status_code=429,
                    detail="Request queue is full. Please retry shortly.",
                    headers={"Retry-After": "1"},
                ) from exc
            
            if request.stream:
                return StreamingResponse(
                    self.stream_generator(response_queue, abort_event, raw_request),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                # Accumulate response for non-streaming
                content_parts: list[str] = []
                finish_reason = None
                error_message: str | None = None
                usage: dict | None = None
                
                while True:
                    chunk_str = await response_queue.get()
                    if chunk_str is None:
                        break

                    parsed = _parse_sse_json(chunk_str)
                    if not parsed:
                        continue

                    if "error" in parsed:
                        error_message = (_extract_error_message(parsed) or "request failed").strip()
                        if not error_message:
                            error_message = "request failed"
                        break

                    if isinstance(parsed.get("usage"), dict):
                        usage = parsed.get("usage")

                    choices = parsed.get("choices")
                    if isinstance(choices, list) and choices:
                        choice0 = choices[0] if isinstance(choices[0], dict) else {}
                        delta = choice0.get("delta") if isinstance(choice0, dict) else {}
                        if isinstance(delta, dict):
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                content_parts.append(content)
                        if isinstance(choice0, dict):
                            finish_reason = choice0.get("finish_reason")

                full_content = "".join(content_parts)
                
                if error_message:
                    abort_event.set()
                    self._request_abort_for_abort_event(abort_event)

                    # Prefer returning partial content for non-streaming clients if we already have
                    # meaningful output (i.e., avoid losing all progress)
                    if content_parts:
                        if _is_terminal_partial_error(error_message):
                            raise HTTPException(status_code=500, detail=error_message)
                        Logger.warning(
                            "Non-streaming request returned partial content due to error: "
                            + str(error_message)
                        )
                    else:
                        raise HTTPException(status_code=500, detail=error_message)

                return {
                    "id": "chatcmpl-custom",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": full_content
                            },
                            "finish_reason": finish_reason or "stop"
                        }
                    ],
                    "usage": _normalize_usage_object(usage),
                }

        @self.app.post("/v1/completions")
        async def text_completions(request: TextCompletionRequest, raw_request: Request):
            api_key_name = self._authorize_request(raw_request)

            if self._dry_run_enabled:
                normalized_prompt = _normalize_text_completion_prompt(request.prompt)
                normalized_request = request.model_copy(update={"prompt": normalized_prompt})
                await self._capture_dry_run_request(
                    normalized_request,
                    raw_request,
                    request_type="text",
                    api_key_name=api_key_name,
                )

            if not self.driver.is_running:
                raise HTTPException(status_code=503, detail="Driver is not running")

            target_slot = self._resolve_request_slot(request.model)
            target_provider = target_slot.provider
            self._ensure_explicit_request_model_available(request.model, target_slot)
            self._ensure_supported_model_id(request.model, target_provider)

            normalized_prompt = _normalize_text_completion_prompt(request.prompt)
            normalized_request = request.model_copy(update={"prompt": normalized_prompt})
            driver_model = self._resolve_driver_model_for_request(normalized_request, target_provider)
            self._ensure_request_model_available(driver_model, target_slot)

            prompt_length = len(normalized_prompt)
            stream_mode = "streaming" if normalized_request.stream else "non-streaming"
            loadout_name = self._get_active_loadout_name_for_provider(target_provider)
            Logger.info(
                f"Received text completion request for {target_provider.value} "
                f"({prompt_length} chars, {stream_mode})"
                + (f" using loadout '{loadout_name}'" if loadout_name else "")
            )

            response_queue = asyncio.Queue()
            abort_event = asyncio.Event()

            entry = QueueEntry(
                id=secrets.token_hex(4),
                queued_at=time.time(),
                request=normalized_request,
                request_type="text",
                target_provider=target_provider,
                response_queue=response_queue,
                abort_event=abort_event,
                target_slot_id=target_slot.id,
                target_slot_label=target_slot.label,
                api_key_name=api_key_name,
                driver_model=driver_model,
            )
            try:
                await self._get_request_queue_for_slot(target_slot.id).put(entry)
            except RequestQueueFullError as exc:
                Logger.warning(
                    "Rejecting text completion request because the request queue is full "
                    f"({exc.current_size}/{exc.max_size} pending requests)."
                )
                raise HTTPException(
                    status_code=429,
                    detail="Request queue is full. Please retry shortly.",
                    headers={"Retry-After": "1"},
                ) from exc

            if normalized_request.stream:
                return StreamingResponse(
                    self.stream_generator(
                        response_queue,
                        abort_event,
                        raw_request,
                        chunk_transform=lambda chunk: _make_text_completion_sse_chunk(
                            chunk,
                            default_model=normalized_request.model,
                        ),
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            content_parts: list[str] = []
            finish_reason = None
            error_message: str | None = None
            usage: dict | None = None

            while True:
                chunk_str = await response_queue.get()
                if chunk_str is None:
                    break

                parsed = _parse_sse_json(chunk_str)
                if not parsed:
                    continue

                if "error" in parsed:
                    error_message = (_extract_error_message(parsed) or "request failed").strip()
                    if not error_message:
                        error_message = "request failed"
                    break

                if isinstance(parsed.get("usage"), dict):
                    usage = parsed.get("usage")

                choices = parsed.get("choices")
                if isinstance(choices, list) and choices:
                    choice0 = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice0.get("delta") if isinstance(choice0, dict) else {}
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            content_parts.append(content)
                    if isinstance(choice0, dict):
                        finish_reason = choice0.get("finish_reason")

            full_content = "".join(content_parts)

            if error_message:
                abort_event.set()
                self._request_abort_for_abort_event(abort_event)

                if content_parts:
                    if _is_terminal_partial_error(error_message):
                        raise HTTPException(status_code=500, detail=error_message)
                    Logger.warning(
                        "Non-streaming text completion returned partial content due to error: "
                        + str(error_message)
                    )
                else:
                    raise HTTPException(status_code=500, detail=error_message)

            return {
                "id": "cmpl-custom",
                "object": "text_completion",
                "created": 0,
                "model": normalized_request.model,
                "choices": [
                    {
                        "text": full_content,
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": finish_reason or "stop",
                    }
                ],
                "usage": _normalize_usage_object(usage),
            }

    async def stream_generator(
        self,
        response_queue: asyncio.Queue,
        abort_event: asyncio.Event,
        raw_request: Request,
        chunk_transform: Callable[[dict], str | None] | None = None,
    ):
        try:
            while True:
                # Check if client disconnected
                if await raw_request.is_disconnected():
                    Logger.warning("Client disconnected, aborting request...")
                    abort_event.set()
                    self._notify_queue_state_changed()
                    removed = await self._remove_queued_request_by_abort_event(abort_event)
                    if not removed:
                        self._request_abort_for_abort_event(abort_event)
                    break
                
                try:
                    # Use a timeout so we can periodically check for disconnection
                    chunk = await asyncio.wait_for(response_queue.get(), timeout=0.5)
                    if chunk is None:
                        yield "data: [DONE]\n\n"
                        break
                    if chunk_transform is not None:
                        parsed = _parse_sse_json(chunk)
                        if parsed is None:
                            continue
                        chunk = chunk_transform(parsed)
                        if chunk is None:
                            continue
                    yield chunk
                except asyncio.TimeoutError:
                    # No chunk available, continue to check for disconnection
                    continue
        except asyncio.CancelledError:
            Logger.warning("Stream generator cancelled, aborting...")
            abort_event.set()
            self._notify_queue_state_changed()
            asyncio.create_task(self._remove_queued_request_by_abort_event(abort_event))
            self._request_abort_for_abort_event(abort_event)
        except GeneratorExit:
            # Client disconnected abruptly
            Logger.warning("Generator exit, aborting...")
            abort_event.set()
            self._notify_queue_state_changed()
            asyncio.create_task(self._remove_queued_request_by_abort_event(abort_event))
            self._request_abort_for_abort_event(abort_event)

    def start_worker(self):
        self.worker_tasks = {
            slot.id: asyncio.create_task(self.worker(slot.id))
            for slot in self._execution_slots
        }

    def _sync_current_entry_aliases(self) -> None:
        active_entries = list(self.current_entries_by_slot_id.values())
        active_abort_events = list(self.current_abort_events_by_slot_id.values())
        self.current_entry = active_entries[0] if len(active_entries) == 1 else None
        self.current_abort_event = active_abort_events[0] if len(active_abort_events) == 1 else None

    async def stop(self):
        Logger.info("Stopping API worker...")
        for queue in self._request_queues_by_slot_id.values():
            try:
                queue.remove_listener(self._request_queue_listener)
            except Exception:
                pass

        remote_control = getattr(self, "remote_control", None)
        if remote_control is not None:
            try:
                remote_control.stop()
            except Exception as exc:
                Logger.warning(f"Failed to stop Remote Control worker cleanly: {exc}")
            finally:
                self.remote_control = None

        worker_tasks = dict(getattr(self, "worker_tasks", {}) or {})
        for task in worker_tasks.values():
            task.cancel()

        for slot_id, task in worker_tasks.items():
            try:
                await task
            except asyncio.CancelledError:
                try:
                    slot_label = self._get_execution_slot(slot_id).label
                except Exception:
                    slot_label = slot_id
                Logger.debug(f"API Worker cancelled for {slot_label}")

        self.worker_tasks = {}
        Logger.info("API worker stopped.")

    async def _process_worker_entry(self, slot: RuntimeExecutionSlot, entry: QueueEntry) -> None:
        slot_id = slot.id
        driver = slot.driver
        provider = slot.provider
        request = entry.request
        request_type = getattr(entry, "request_type", "chat")
        driver_model = str(getattr(entry, "driver_model", None) or getattr(request, "model", "") or "")
        request_model = str(getattr(request, "model", "") or "").strip()
        should_rewrite_chunk_model = bool(request_model and request_model != driver_model.strip())
        response_queue = entry.response_queue
        abort_event = entry.abort_event
        loadout_name = self._get_active_loadout_name_for_provider(provider)
        Logger.info(
            f"Processing queued {request_type} request for {slot.label}..."
            + (f" (loadout: {loadout_name})" if loadout_name else "")
        )
        try:
            if not bool(getattr(driver, "is_running", False)):
                await response_queue.put(
                    _make_openai_error_sse_chunk(
                        f"{slot.label} is not running. Restart services to relaunch this instance."
                    )
                )
                return

            if abort_event.is_set():
                Logger.info("Queued request was already aborted. Skipping.")
                return

            self.current_entries_by_slot_id[slot_id] = entry
            self.current_abort_events_by_slot_id[slot_id] = abort_event
            self._sync_current_entry_aliases()
            self._notify_queue_state_changed()

            try:
                ece_reauth_enabled = bool(driver.ece_reauth_enabled())
            except Exception:
                ece_reauth_enabled = False

            max_attempts = 2 if ece_reauth_enabled else 1
            attempt = 0
            forwarded_any = False

            while attempt < max_attempts and (not abort_event.is_set()):
                attempt += 1

                # Track usage for Select Least Used
                try:
                    pair_getter = getattr(driver, "ece_active_pair", None)
                    mark_used = getattr(driver, "ece_mark_used", None)
                    if callable(pair_getter) and callable(mark_used):
                        pair = pair_getter()
                        email = getattr(pair, "email", None) if pair else None
                        if isinstance(email, str) and email.strip():
                            mark_used(email)
                except Exception:
                    pass

                # Optional provider hook: pass request-only controls that are not encoded
                # in the OpenAI-compatible model ID.
                try:
                    apply_request_overrides = getattr(driver, "set_request_overrides", None)
                    if callable(apply_request_overrides):
                        apply_request_overrides(
                            self._extract_provider_request_overrides(request, provider)
                        )
                except Exception as e:
                    provider_label = getattr(driver, "provider_label", None) or provider.value
                    Logger.debug(f"{provider_label}: failed to apply request overrides: {e}")

                # Optional provider hook: apply configured *real* model selection (UI model picker),
                # if the active provider supports it
                try:
                    should_apply_model = True
                    should_apply_model_before_request = getattr(
                        driver,
                        "should_apply_configured_model_before_request",
                        None,
                    )
                    if callable(should_apply_model_before_request):
                        should_apply_model = bool(should_apply_model_before_request())
                    if should_apply_model:
                        await driver.apply_configured_model(model=driver_model)
                except Exception as e:
                    provider_label = getattr(driver, "provider_label", None) or provider.value
                    Logger.warning(f"{provider_label}: Failed to apply configured model selection: {e}")

                meaningful_seen = False
                buffered: list[str] = []
                early_error_message: str | None = None

                try:
                    driver_message = (
                        request.messages
                        if request_type == "chat"
                        else _normalize_text_completion_prompt(request.prompt)
                    )
                    driver_gen = driver.generate_response(
                        message=driver_message,
                        model=driver_model,
                        stream=request.stream,
                        temperature=request.temperature,
                        top_p=request.top_p,
                        max_tokens=request.max_tokens,
                        abort_event=abort_event,
                    )
                    async for chunk in driver_gen:
                        client_chunk = (
                            _set_openai_sse_chunk_model(chunk, request_model)
                            if should_rewrite_chunk_model
                            else chunk
                        )
                        if abort_event.is_set():
                            Logger.debug("Request aborted, stopping chunk forwarding...")
                            break

                        if not meaningful_seen:
                            parsed = _parse_sse_json(client_chunk)
                            if parsed and ("error" in parsed):
                                early_error_message = _extract_error_message(parsed)
                                # Don't forward the error yet; we may retry after rotating
                                break

                            if parsed and _payload_has_meaningful_content(parsed):
                                meaningful_seen = True
                                for buffered_chunk in buffered:
                                    await response_queue.put(buffered_chunk)
                                    forwarded_any = True
                                buffered.clear()

                        if meaningful_seen:
                            await response_queue.put(client_chunk)
                            forwarded_any = True
                        else:
                            buffered.append(client_chunk)
                except Exception as e:
                    early_error_message = str(e)
                finally:
                    # Close the driver's async generator deterministically so its
                    # cleanup runs here instead of at GC time (avoids
                    # "async generator ignored GeneratorExit" noise on retries).
                    try:
                        await driver_gen.aclose()
                    except Exception:
                        pass

                if abort_event.is_set():
                    break

                if early_error_message and meaningful_seen:
                    await response_queue.put(_make_openai_error_sse_chunk(early_error_message))
                    forwarded_any = True
                    break

                # If we saw meaningful content, keep the existing behavior
                if meaningful_seen:
                    break

                # No meaningful chunks were produced
                is_final_attempt = attempt >= max_attempts
                if (
                    (not is_final_attempt)
                    and (not forwarded_any)
                    and (not _is_non_retryable_provider_error(early_error_message))
                ):
                    reason = early_error_message or "no meaningful output"
                    if early_error_message and _is_rate_limit_like_error(early_error_message):
                        reason = f"rate-limit-like failure: {early_error_message}"

                    restarted = False
                    if not abort_event.is_set():
                        restart = getattr(driver, "ece_restart_with_rotation", None)
                        if callable(restart):
                            try:
                                restarted = bool(
                                    await restart(
                                        reason,
                                        status_callback=None,
                                        die_on_no_rotation=bool(
                                            getattr(driver, "_ece_die_on_failed_rotation", False)
                                        ),
                                    )
                                )
                            except Exception:
                                restarted = False
                    if restarted:
                        continue

                # Can't / won't retry. Prefer surfacing the early error (if any)
                if early_error_message:
                    await response_queue.put(_make_openai_error_sse_chunk(early_error_message))
                    forwarded_any = True
                else:
                    for buffered_chunk in buffered:
                        await response_queue.put(buffered_chunk)
                        forwarded_any = True

                break

        except Exception as e:
            Logger.error(f"Error in {slot.label} worker: {e}")
            await response_queue.put(_make_openai_error_sse_chunk(str(e)))
        finally:
            self.current_entries_by_slot_id.pop(slot_id, None)
            self.current_abort_events_by_slot_id.pop(slot_id, None)
            self._sync_current_entry_aliases()
            self._notify_queue_state_changed()
            await response_queue.put(None)
            Logger.success(f"Request completed for {slot.label}.")

    async def worker(self, slot_id: str):
        slot = self._get_execution_slot(slot_id)
        request_queue = self._get_request_queue_for_slot(slot_id)
        Logger.info(f"API Worker started for {slot.label}")
        try:
            while True:
                if self._parallel_request_queue_enabled:
                    entry = await request_queue.get()
                    await self._process_worker_entry(slot, entry)
                    continue

                await request_queue.wait_for_item()
                async with self._global_processing_lock:
                    entry = await request_queue.get_nowait()
                    if entry is None:
                        continue
                    await self._process_worker_entry(slot, entry)
        except asyncio.CancelledError:
            Logger.info(f"API Worker cancelled for {slot.label}")
            raise
