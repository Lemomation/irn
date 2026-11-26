"""Shared helpers for driver-side formatting, macros, and clean regeneration state."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Union

from utils.logger import Logger


COMMON_REQUEST_MACRO_ACTIONS: Dict[str, tuple[str, Any]] = {
    # Thinking
    "think": ("deepthink_enabled", True),
    "r1": ("deepthink_enabled", True),
    "nothink": ("deepthink_enabled", False),
    "no_think": ("deepthink_enabled", False),
    "r0": ("deepthink_enabled", False),
    # Search
    "search": ("search_enabled", True),
    "nosearch": ("search_enabled", False),
    "no_search": ("search_enabled", False),
    "no-search": ("search_enabled", False),
    # Send as text file
    "file": ("send_as_text_file", True),
    "sendfile": ("send_as_text_file", True),
    "nofile": ("send_as_text_file", False),
    "no_file": ("send_as_text_file", False),
}

_CLEAN_REGEN_STATE_KEYS = (
    "deepthink_enabled",
    "search_enabled",
    "tools_enabled",
    "send_as_text_file",
    "ui_model",
)
_CLEAN_REGEN_MULTI_SLOT_VERSION = 1
_CLEAN_REGEN_MULTI_SLOT_MAX_ITEMS = 7
_MACRO_PATTERN = re.compile(r"\[\[\s*([a-zA-Z0-9_-]+)\s*\]\]")
_IR2_PATTERN = re.compile(r"\[\[IR2u\]\](.*?)\[\[/IR2u\]\]-\[\[IR2a\]\](.*?)\[\[/IR2a\]\]")
_CLASSIC_PATTERN = re.compile(r'DATA1: "(.*?)"\s*DATA2: "(.*?)"')


def make_openai_delta_sse(
    model_name: str,
    content: str = "",
    *,
    reasoning_content: str = "",
    finish_reason: str | None = None,
    chunk_id: str = "chatcmpl-custom",
) -> str:
    """Build an OpenAI-compatible chat completion delta SSE chunk."""
    delta: dict[str, str] = {}
    if reasoning_content:
        delta["reasoning_content"] = reasoning_content
    if content:
        delta["content"] = content

    openai_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(openai_chunk)}\n\n"


def make_openai_usage_sse(
    model_name: str,
    usage: dict[str, Any],
    *,
    chunk_id: str = "chatcmpl-custom",
) -> str:
    """Build an OpenAI-compatible chat completion usage SSE chunk."""
    openai_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name,
        "choices": [],
        "usage": usage,
    }
    return f"data: {json.dumps(openai_chunk)}\n\n"


def compute_missing_suffix(emitted: str, candidate: str) -> str:
    """Return the suffix in candidate that has not already been emitted."""
    if not candidate:
        return ""
    if not emitted:
        return candidate
    if candidate.startswith(emitted):
        return candidate[len(emitted) :]

    idx = candidate.rfind(emitted)
    if idx != -1:
        return candidate[idx + len(emitted) :]

    anchor_len = min(200, len(emitted))
    if anchor_len > 0:
        anchor = emitted[-anchor_len:]
        idx = candidate.rfind(anchor)
        if idx != -1:
            return candidate[idx + anchor_len :]

    max_check = min(500, len(emitted), len(candidate))
    for size in range(max_check, 0, -1):
        if emitted.endswith(candidate[:size]):
            return candidate[size:]

    if len(candidate) <= 800:
        return candidate
    return ""


class IncrementalTextAccumulator:
    """Track emitted text while avoiding full-string copies on every append."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._text_cache: str | None = ""

    @property
    def has_text(self) -> bool:
        return self._length > 0

    @property
    def text(self) -> str:
        if self._text_cache is None:
            self._text_cache = "".join(self._parts)
        return self._text_cache

    def append(self, text: str) -> None:
        piece = str(text or "")
        if not piece:
            return
        self._parts.append(piece)
        self._length += len(piece)
        self._text_cache = None

    def missing_suffix(self, candidate: str) -> str:
        candidate = str(candidate or "")
        if not candidate:
            return ""
        if self._length <= 0:
            return candidate
        if len(candidate) >= self._length and self._candidate_starts_with_parts(candidate):
            return candidate[self._length :]
        return compute_missing_suffix(self.text, candidate)

    def _candidate_starts_with_parts(self, candidate: str) -> bool:
        offset = 0
        for part in self._parts:
            if not candidate.startswith(part, offset):
                return False
            offset += len(part)
        return True


def build_prompt_text_file_payload(
    formatted_message: str,
    *,
    filename: str = "prompt.txt",
) -> dict[str, Any]:
    """Return the shared text-file upload payload used by provider drivers."""
    return {
        "name": filename,
        "mimeType": "text/plain",
        "buffer": str(formatted_message or "").encode("utf-8"),
    }


def clear_clean_regeneration_cache(
    cache_manager: Any,
    message_cache_key: str,
    state_cache_key: str,
) -> None:
    """Clear cached prompt and state entries used by clean-regeneration flows."""
    cache_manager.clear_cache(message_cache_key)
    cache_manager.clear_cache(state_cache_key)


def read_multi_slot_cache_payload(
    cache_manager: Any,
    cache_key: str,
    *,
    log_label: str = "Multi-Slot Cache",
) -> Dict[str, Any]:
    """Load and validate the persisted multi-slot clean-regeneration cache."""
    raw = cache_manager.read_cache(cache_key)
    if raw is None:
        return {"version": _CLEAN_REGEN_MULTI_SLOT_VERSION, "accounts": {}}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        Logger.warning(f"{log_label}: Cached multi-slot payload is invalid JSON, ignoring.")
        return {"version": _CLEAN_REGEN_MULTI_SLOT_VERSION, "accounts": {}}

    if not isinstance(data, dict):
        return {"version": _CLEAN_REGEN_MULTI_SLOT_VERSION, "accounts": {}}

    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, dict):
        raw_accounts = {}

    accounts: Dict[str, List[Dict[str, Any]]] = {}
    for raw_account_key, raw_entries in raw_accounts.items():
        account_key = str(raw_account_key or "").strip()
        if not account_key or not isinstance(raw_entries, list):
            continue

        cleaned_entries: List[Dict[str, Any]] = []
        for raw_entry in raw_entries:
            normalized = _normalize_multi_slot_entry(raw_entry)
            if normalized is not None:
                cleaned_entries.append(normalized)

        if cleaned_entries:
            accounts[account_key] = cleaned_entries[-_CLEAN_REGEN_MULTI_SLOT_MAX_ITEMS :]

    return {
        "version": _CLEAN_REGEN_MULTI_SLOT_VERSION,
        "accounts": accounts,
    }


def write_multi_slot_cache_payload(
    cache_manager: Any,
    cache_key: str,
    payload: Dict[str, Any],
) -> None:
    """Persist the normalized multi-slot clean-regeneration cache."""
    accounts = payload.get("accounts") if isinstance(payload, dict) else {}
    if not isinstance(accounts, dict):
        accounts = {}

    normalized_payload = {
        "version": _CLEAN_REGEN_MULTI_SLOT_VERSION,
        "accounts": accounts,
    }
    cache_manager.write_cache(
        cache_key,
        json.dumps(normalized_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


def find_multi_slot_cache_entry(
    payload: Dict[str, Any],
    account_key: str,
    prompt: str,
    state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return the newest cached entry that matches the prompt and state."""
    if not isinstance(payload, dict):
        return None

    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, dict):
        return None

    entries = raw_accounts.get(str(account_key or "").strip())
    if not isinstance(entries, list):
        return None

    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("prompt") != prompt:
            continue
        if entry.get("state") != state:
            continue
        normalized = _normalize_multi_slot_entry(entry)
        if normalized is not None:
            return normalized

    return None


def upsert_multi_slot_cache_entry(
    cache_manager: Any,
    cache_key: str,
    account_key: str,
    entry: Dict[str, Any],
    *,
    log_label: str = "Multi-Slot Cache",
) -> None:
    """Insert or replace a cached conversation entry, keeping only the newest slots."""
    normalized_entry = _normalize_multi_slot_entry(entry)
    cache_payload = read_multi_slot_cache_payload(
        cache_manager,
        cache_key,
        log_label=log_label,
    )
    if normalized_entry is None:
        return

    normalized_account_key = str(account_key or "").strip()
    if not normalized_account_key:
        return

    accounts = cache_payload.setdefault("accounts", {})
    raw_entries = accounts.get(normalized_account_key, [])
    if not isinstance(raw_entries, list):
        raw_entries = []

    cleaned_entries: List[Dict[str, Any]] = []
    for raw_entry in raw_entries:
        existing = _normalize_multi_slot_entry(raw_entry)
        if existing is None:
            continue
        if existing["conversation_id"] == normalized_entry["conversation_id"]:
            continue
        cleaned_entries.append(existing)

    cleaned_entries.append(normalized_entry)
    accounts[normalized_account_key] = cleaned_entries[-_CLEAN_REGEN_MULTI_SLOT_MAX_ITEMS :]
    write_multi_slot_cache_payload(cache_manager, cache_key, cache_payload)


def remove_multi_slot_cache_entry(
    cache_manager: Any,
    cache_key: str,
    account_key: str,
    conversation_id: str,
    *,
    log_label: str = "Multi-Slot Cache",
) -> bool:
    """Remove a cached conversation entry and return whether anything changed."""
    normalized_account_key = str(account_key or "").strip()
    normalized_conversation_id = str(conversation_id or "").strip()
    if not normalized_account_key or not normalized_conversation_id:
        return False

    cache_payload = read_multi_slot_cache_payload(
        cache_manager,
        cache_key,
        log_label=log_label,
    )
    accounts = cache_payload.get("accounts")
    if not isinstance(accounts, dict):
        return False

    raw_entries = accounts.get(normalized_account_key)
    if not isinstance(raw_entries, list):
        return False

    kept_entries: List[Dict[str, Any]] = []
    removed = False
    for raw_entry in raw_entries:
        existing = _normalize_multi_slot_entry(raw_entry)
        if existing is None:
            continue
        if existing["conversation_id"] == normalized_conversation_id:
            removed = True
            continue
        kept_entries.append(existing)

    if not removed:
        return False

    if kept_entries:
        accounts[normalized_account_key] = kept_entries
    else:
        accounts.pop(normalized_account_key, None)

    write_multi_slot_cache_payload(cache_manager, cache_key, cache_payload)
    return True


def extract_macro_overrides(
    text: str,
    macro_actions: Dict[str, tuple[str, Any]] | None = None,
) -> tuple[str, Dict[str, Any]]:
    """Strip recognized request macros from text and return the derived overrides.

    Args:
        text: Raw prompt text that may contain ``[[macro]]`` markers.
        macro_actions: Optional provider-specific macro map merged over the shared defaults.

    Returns:
        A tuple of ``(cleaned_text, overrides)`` where ``overrides`` contains the
        request settings implied by any recognized macros.
    """
    if not text:
        return text, {}

    actions = dict(COMMON_REQUEST_MACRO_ACTIONS)
    if macro_actions:
        actions.update(macro_actions)

    overrides: Dict[str, Any] = {}

    def _replace_macro(match: re.Match[str]) -> str:
        macro = (match.group(1) or "").strip().lower()
        action = actions.get(macro)
        if not action:
            return match.group(0)

        key, value = action
        overrides[key] = value
        return ""

    cleaned = _MACRO_PATTERN.sub(_replace_macro, text)
    return cleaned, overrides


def strip_macros_from_messages(
    messages: List[Any],
    macro_actions: Dict[str, tuple[str, Any]] | None = None,
) -> tuple[List[Any], Dict[str, Any]]:
    """Remove request macros from the last user message in a transcript.

    Args:
        messages: Chat messages represented as dict-like or object-like items.
        macro_actions: Optional provider-specific macro map merged over the shared defaults.

    Returns:
        A tuple of ``(messages, overrides)``. The returned message list is copied only
        when a recognized macro is removed from the final user message.
    """
    last_user_index = None
    for idx in range(len(messages) - 1, -1, -1):
        role = _coerce_role(messages[idx])
        if role.strip().lower() == "user":
            last_user_index = idx
            break

    if last_user_index is None:
        return messages, {}

    last_msg = messages[last_user_index]
    content = _get_message_field(last_msg, "content", "")
    if not isinstance(content, str):
        return messages, {}

    cleaned_content, overrides = extract_macro_overrides(content, macro_actions=macro_actions)
    if not overrides:
        return messages, {}

    cleaned_messages = list(messages)
    if isinstance(last_msg, dict):
        updated = dict(last_msg)
        updated["content"] = cleaned_content
        cleaned_messages[last_user_index] = updated
        return cleaned_messages, overrides

    role_value = _get_message_field(last_msg, "role", "")
    name_value = _get_message_field(last_msg, "name")
    updated = {"role": role_value, "content": cleaned_content}
    if name_value is not None:
        updated["name"] = name_value
    cleaned_messages[last_user_index] = updated
    return cleaned_messages, overrides


def split_leading_system_messages(messages: List[Any]) -> tuple[List[Any], List[Any]]:
    """Split a transcript into leading system messages and the remaining messages."""
    split_index = 0
    for msg in messages:
        role = _coerce_role(msg).strip().lower()
        if role != "system":
            break
        split_index += 1

    if split_index <= 0:
        return [], messages

    return list(messages[:split_index]), list(messages[split_index:])


def read_clean_regeneration_state(
    cache_manager: Any,
    state_cache_key: str,
    *,
    log_label: str = "Clean Regeneration",
) -> Optional[Dict[str, Any]]:
    """Load and validate cached clean-regeneration state from the cache manager.

    Args:
        cache_manager: Cache abstraction used by the drivers.
        state_cache_key: Cache key containing the serialized state payload.
        log_label: Label used for warning messages when cached JSON is invalid.

    Returns:
        The normalized state dict when the cache entry is present and valid, otherwise
        ``None``.
    """
    raw = cache_manager.read_cache(state_cache_key)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        Logger.warning(f"{log_label}: Cached state is invalid JSON, ignoring.")
        return None

    if not isinstance(data, dict):
        return None

    if not all(key in data for key in _CLEAN_REGEN_STATE_KEYS):
        return None

    return {
        "deepthink_enabled": bool(data.get("deepthink_enabled")),
        "search_enabled": bool(data.get("search_enabled")),
        "tools_enabled": bool(data.get("tools_enabled")),
        "send_as_text_file": bool(data.get("send_as_text_file")),
        "ui_model": str(data.get("ui_model") or "").strip(),
    }


def write_clean_regeneration_state(
    cache_manager: Any,
    state_cache_key: str,
    state: Dict[str, Any],
) -> None:
    """Persist the clean-regeneration state subset used for cache comparisons."""
    payload = {
        "deepthink_enabled": bool(state.get("deepthink_enabled")),
        "search_enabled": bool(state.get("search_enabled")),
        "tools_enabled": bool(state.get("tools_enabled")),
        "send_as_text_file": bool(state.get("send_as_text_file")),
        "ui_model": str(state.get("ui_model") or "").strip(),
    }
    cache_manager.write_cache(
        state_cache_key,
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


def format_messages(config_manager: Any, messages: Union[str, List[Any]]) -> str:
    """Render messages through the configurable formatting pipeline.

    Args:
        config_manager: Configuration source for formatting flags, templates, and
            injection settings.
        messages: Either a raw prompt string or a chat transcript.

    Returns:
        The formatted prompt text that should be sent to the provider UI.
    """
    apply_formatting = config_manager.get_setting("formatting", "apply_formatting")

    if not apply_formatting:
        if isinstance(messages, list):
            formatted_parts = []
            for msg in messages:
                role = _get_message_field(msg, "role", "")
                content = _get_message_field(msg, "content", "")
                formatted_parts.append(f"{role}: {content}")
            return "\n".join(formatted_parts)
        return messages

    user_name, char_name = _resolve_display_names(config_manager, messages)

    template = config_manager.get_setting("formatting", "formatting_template") or ""
    divider = config_manager.get_setting("formatting", "formatting_divider") or ""
    template = str(template).replace("\\n", "\n")
    divider = str(divider).replace("\\n", "\n")

    formatted_parts = []
    if isinstance(messages, list):
        use_msg_objects = bool(config_manager.get_setting("formatting", "enable_msg_objects"))
        for msg in messages:
            role_raw = _get_message_field(msg, "role", "")
            content = _get_message_field(msg, "content", "")
            msg_name = _get_message_name(msg) if use_msg_objects else None

            display_role = "System"
            display_name = "System"
            if role_raw == "user":
                display_role = "User"
                display_name = msg_name if msg_name else user_name
            elif role_raw == "assistant":
                display_role = "Character"
                display_name = msg_name if msg_name else char_name

            part = (
                str(template)
                .replace("{{name}}", str(display_name))
                .replace("{{role}}", str(display_role))
                .replace("{{content}}", str(content))
            )
            formatted_parts.append(part)
    else:
        part = (
            str(template)
            .replace("{{name}}", user_name)
            .replace("{{role}}", "User")
            .replace("{{content}}", str(messages))
        )
        formatted_parts.append(part)

    final_message = divider.join(formatted_parts)
    injection_pos = config_manager.get_setting("formatting", "injection_position")
    injection_content = config_manager.get_setting("formatting", "injection_content")
    if injection_content:
        rendered_injection = _render_injection(str(injection_content), user_name, char_name)
        if injection_pos == "Before":
            final_message = rendered_injection + "\n" + final_message
        else:
            final_message = final_message + "\n" + rendered_injection

    return final_message


def format_request_messages(config_manager: Any, messages: Union[str, List[Any]]) -> str:
    """Render a provider request payload with text-completion passthrough.

    Chat transcripts still use the normal formatting pipeline. Raw strings are treated
    as `/v1/completions` prompts and are sent unchanged so chat-only formatting layers
    do not rewrite them.
    """
    if isinstance(messages, str):
        return messages
    return format_messages(config_manager, messages)


def resolve_rendered_injection(
    config_manager: Any,
    messages: Union[str, List[Any]],
) -> tuple[str, str]:
    """Return the configured injection position and its rendered text."""
    if not config_manager.get_setting("formatting", "apply_formatting"):
        return "", ""

    injection_pos = str(config_manager.get_setting("formatting", "injection_position") or "")
    injection_content = config_manager.get_setting("formatting", "injection_content")
    if not injection_content:
        return injection_pos, ""

    user_name, char_name = _resolve_display_names(config_manager, messages)
    rendered_injection = _render_injection(str(injection_content), user_name, char_name)
    return injection_pos, rendered_injection


def _coerce_role(message: Any) -> str:
    """Return a message role as a string regardless of message object shape."""
    role = _get_message_field(message, "role", "")
    return str(role or "")


def _get_message_field(message: Any, key: str, default: Any = None) -> Any:
    """Read a field from either an object-like or dict-like message payload."""
    try:
        value = getattr(message, key)
    except Exception:
        value = None

    if value is not None:
        return value
    if isinstance(message, dict):
        return message.get(key, default)
    return default


def _get_message_name(message: Any) -> Any:
    """Return the preferred display name stored on a message, if any."""
    name_value = _get_message_field(message, "name")
    if name_value:
        return name_value
    return _get_message_field(message, "irp-next")


def _resolve_display_names(config_manager: Any, messages: Union[str, List[Any]]) -> tuple[str, str]:
    """Resolve user and assistant display names for formatting and injections."""
    user_name = "User"
    char_name = "Character"
    msgs_to_scan = messages if isinstance(messages, list) else []

    if config_manager.get_setting("formatting", "enable_msg_objects"):
        for msg in msgs_to_scan:
            role = _get_message_field(msg, "role", "")
            name = _get_message_name(msg)
            if name:
                if role == "user":
                    user_name = name
                elif role == "assistant":
                    char_name = name

    enable_ir2 = config_manager.get_setting("formatting", "enable_ir2")
    enable_classic = config_manager.get_setting("formatting", "enable_classic_irp")
    if enable_ir2 or enable_classic:
        for msg in msgs_to_scan:
            role = _get_message_field(msg, "role", "")
            content = _get_message_field(msg, "content", "")
            if role != "system":
                continue

            if enable_ir2:
                ir2_match = _IR2_PATTERN.search(str(content))
                if ir2_match:
                    user_name = ir2_match.group(1)
                    char_name = ir2_match.group(2)

            if enable_classic:
                classic_match = _CLASSIC_PATTERN.search(str(content))
                if classic_match:
                    char_name = classic_match.group(1)
                    user_name = classic_match.group(2)

    return user_name, char_name


def _render_injection(text: str, user_name: str, char_name: str) -> str:
    """Expand supported user and assistant placeholders in an injection snippet."""
    rendered = "" if text is None else str(text)
    rendered = rendered.replace("{{user}}", user_name)
    rendered = rendered.replace("{{char}}", char_name)
    rendered = rendered.replace("{username}", user_name)
    rendered = rendered.replace("{asstname}", char_name)
    rendered = rendered.replace("{{username}}", user_name)
    rendered = rendered.replace("{{asstname}}", char_name)
    return rendered


def _normalize_multi_slot_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None

    conversation_id = str(entry.get("conversation_id") or "").strip()
    conversation_url = str(entry.get("conversation_url") or "").strip()
    prompt = entry.get("prompt")
    state = entry.get("state")

    if not conversation_id or not conversation_url:
        return None
    if not isinstance(prompt, str) or not isinstance(state, dict):
        return None

    return {
        "conversation_id": conversation_id,
        "conversation_url": conversation_url,
        "prompt": prompt,
        "state": dict(state),
    }
