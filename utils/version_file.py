from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

DEFAULT_POST_UPDATE_ACTION = "survey"
DEFAULT_POST_UPDATE_FUNCTION_REF = "none"
DEFAULT_UPDATE_SUMMARY = ""
_POST_UPDATE_ACTIONS = {"survey", "none"}


@dataclass(frozen=True)
class VersionFileInfo:
    version: str
    auto_updateable: bool
    severity: int  # 1-4
    post_update: str  # survey | none
    post_update_function_ref: str = DEFAULT_POST_UPDATE_FUNCTION_REF
    summary: str = DEFAULT_UPDATE_SUMMARY


def parse_version_file(
    text: str,
    *,
    default_version: str = "unknown",
    default_auto_updateable: bool = True,
    default_severity: int = 2,
    default_post_update: str = DEFAULT_POST_UPDATE_ACTION,
    default_post_update_function_ref: str = DEFAULT_POST_UPDATE_FUNCTION_REF,
    default_summary: str = DEFAULT_UPDATE_SUMMARY,
    strict: bool = False,
) -> VersionFileInfo:
    """
    Parse version.json content.

    version.json is a JSON object with these keys:

    - version: SemVer-like string
    - aua: boolean (auto-updateable?)
    - severity: integer 1-4
    - pu: post-update action (survey, none)
    - pufref: post-update function reference (none, or a registered function key)
    - summary: brief post-update summary text
    """

    value = (text or "").strip()
    if not value:
        if strict:
            raise ValueError("version.json is empty.")
        return VersionFileInfo(
            version=default_version,
            auto_updateable=default_auto_updateable,
            severity=_clamp_severity(default_severity, default=default_severity),
            post_update=_coerce_post_update(default_post_update, default=default_post_update),
            post_update_function_ref=_coerce_post_update_function_ref(
                default_post_update_function_ref,
                default=default_post_update_function_ref,
            ),
            summary=_coerce_summary(default_summary, default=default_summary),
        )

    try:
        payload = json.loads(value)
    except Exception as exc:
        if strict:
            raise ValueError("version.json is not valid JSON.") from exc
        payload = {}

    if not isinstance(payload, dict):
        if strict:
            raise ValueError("version.json must be a JSON object.")
        payload = {}

    version_raw = payload.get("version", default_version)
    version_str = str(version_raw or "").strip() or default_version

    aua_raw = payload.get("aua", payload.get("auto_updateable", default_auto_updateable))
    aua = _coerce_bool(aua_raw, default_auto_updateable)

    severity_raw = payload.get("severity", default_severity)
    severity = _clamp_severity(_coerce_int(severity_raw, default_severity), default=default_severity)

    post_update_raw = payload.get("pu", payload.get("post_update", default_post_update))
    post_update = _coerce_post_update(post_update_raw, default=default_post_update)

    pufref_raw = payload.get(
        "pufref",
        payload.get("post_update_function_ref", default_post_update_function_ref),
    )
    post_update_function_ref = _coerce_post_update_function_ref(
        pufref_raw,
        default=default_post_update_function_ref,
    )

    summary_raw = payload.get("summary", payload.get("update_summary", default_summary))
    summary = _coerce_summary(summary_raw, default=default_summary)

    return VersionFileInfo(
        version=version_str,
        auto_updateable=aua,
        severity=severity,
        post_update=post_update,
        post_update_function_ref=post_update_function_ref,
        summary=summary,
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return bool(int(value))
        except Exception:
            return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on", "t"}:
            return True
        if v in {"0", "false", "no", "n", "off", "f"}:
            return False
        return default
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _clamp_severity(value: int, *, default: int) -> int:
    try:
        severity = int(value)
    except Exception:
        return default
    return severity if 1 <= severity <= 4 else default


def _coerce_post_update(value: Any, *, default: str) -> str:
    normalized_default = str(default or DEFAULT_POST_UPDATE_ACTION).strip().lower() or DEFAULT_POST_UPDATE_ACTION
    if normalized_default not in _POST_UPDATE_ACTIONS:
        normalized_default = DEFAULT_POST_UPDATE_ACTION

    normalized = str(value or "").strip().lower()
    if normalized == "discord":
        return "none"
    if normalized in _POST_UPDATE_ACTIONS:
        return normalized
    return normalized_default


def _coerce_post_update_function_ref(value: Any, *, default: str) -> str:
    normalized_default = str(default or DEFAULT_POST_UPDATE_FUNCTION_REF).strip().lower()
    if not normalized_default:
        normalized_default = DEFAULT_POST_UPDATE_FUNCTION_REF

    normalized = str(value or "").strip().lower()
    if normalized:
        return normalized
    return normalized_default


def _coerce_summary(value: Any, *, default: str) -> str:
    summary = str(value or "").strip()
    if summary:
        return summary
    return str(default or "").strip()
