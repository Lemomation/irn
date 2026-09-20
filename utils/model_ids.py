from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping

from drivers.providers import DriverProvider

MODE_AUTO = "auto"
MODE_CHAT = "chat"
MODE_REASONER = "reasoner"
DEEPSEEK_MODEL_TYPE_DEFAULT = "default"
DEEPSEEK_MODEL_TYPE_EXPERT = "expert"

REAL_MODEL_SUFFIX_MODE_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    ("-auto", MODE_AUTO),
    ("-reasoner", MODE_REASONER),
    ("-chat", MODE_CHAT),
)

REAL_MODEL_ID_PROVIDERS: set[DriverProvider] = {
    DriverProvider.GLM_CHAT,
    DriverProvider.QWEN_LM,
    DriverProvider.PERPLEXITY,
    DriverProvider.HUGGINGCHAT,
    DriverProvider.AI_STUDIO,
    DriverProvider.MIMO,
    DriverProvider.MOONSHOT,
}

UMM_MODEL_IDS: tuple[str, ...] = (
    "intenserp-auto",
    "intenserp-reasoner",
    "intenserp-chat",
)

UMM_MODE_BY_MODEL_ID: Dict[str, str] = {
    "intenserp-auto": MODE_AUTO,
    "intenserp-reasoner": MODE_REASONER,
    "intenserp-chat": MODE_CHAT,
}

DEEPSEEK_UMM_EXPERT_MODE_BY_MODEL_ID: Dict[str, str] = {
    "intenserp-expert-auto": MODE_AUTO,
    "intenserp-expert-reasoner": MODE_REASONER,
    "intenserp-expert-chat": MODE_CHAT,
}

DEEPSEEK_UMM_EXPERT_MODEL_IDS: tuple[str, ...] = tuple(DEEPSEEK_UMM_EXPERT_MODE_BY_MODEL_ID.keys())


LEGACY_MODE_BY_PROVIDER: Dict[DriverProvider, Dict[str, str]] = {
    DriverProvider.DEEPSEEK: {
        "deepseek-auto": MODE_AUTO,
        "deepseek-chat": MODE_CHAT,
        "deepseek-reasoner": MODE_REASONER,
        "deepseek-expert-auto": MODE_AUTO,
        "deepseek-expert-chat": MODE_CHAT,
        "deepseek-expert-reasoner": MODE_REASONER,
    },
    DriverProvider.GLM_CHAT: {
        "glm-auto": MODE_AUTO,
        "glm-chat": MODE_CHAT,
        "glm-reasoner": MODE_REASONER,
    },
    DriverProvider.MOONSHOT: {
        "moonshot-auto": MODE_AUTO,
        "moonshot-chat": MODE_CHAT,
        "moonshot-reasoner": MODE_REASONER,
    },
    DriverProvider.QWEN_LM: {
        "qwen-auto": MODE_AUTO,
        "qwen-chat": MODE_CHAT,
        "qwen-reasoner": MODE_REASONER,
    },
    DriverProvider.PERPLEXITY: {
        "perplexity-auto": MODE_AUTO,
        "perplexity-chat": MODE_CHAT,
        "perplexity-reasoner": MODE_REASONER,
    },
    DriverProvider.HUGGINGCHAT: {
        "huggingchat-auto": MODE_AUTO,
        "huggingchat-chat": MODE_CHAT,
        "huggingchat-reasoner": MODE_REASONER,
    },
    DriverProvider.AI_STUDIO: {
        "aistudio-auto": MODE_AUTO,
        "aistudio-chat": MODE_CHAT,
        "aistudio-reasoner": MODE_REASONER,
    },
    DriverProvider.MIMO: {
        "mimo-auto": MODE_AUTO,
        "mimo-chat": MODE_CHAT,
        "mimo-reasoner": MODE_REASONER,
    },
}

LEGACY_MODEL_IDS_BY_PROVIDER: Dict[DriverProvider, tuple[str, ...]] = {
    provider: tuple(model_map.keys())
    for provider, model_map in LEGACY_MODE_BY_PROVIDER.items()
}

LEGACY_MODEL_PREFIX_BY_PROVIDER: Dict[DriverProvider, str] = {
    DriverProvider.DEEPSEEK: "deepseek",
    DriverProvider.GLM_CHAT: "glm",
    DriverProvider.MOONSHOT: "moonshot",
    DriverProvider.QWEN_LM: "qwen",
    DriverProvider.PERPLEXITY: "perplexity",
    DriverProvider.HUGGINGCHAT: "huggingchat",
    DriverProvider.AI_STUDIO: "aistudio",
    DriverProvider.MIMO: "mimo",
}

OWNED_BY_PROVIDER: Dict[DriverProvider, str] = {
    DriverProvider.DEEPSEEK: "deepseek",
    DriverProvider.GLM_CHAT: "glm",
    DriverProvider.MOONSHOT: "moonshot",
    DriverProvider.QWEN_LM: "qwen",
    DriverProvider.PERPLEXITY: "perplexity",
    DriverProvider.HUGGINGCHAT: "huggingchat",
    DriverProvider.AI_STUDIO: "aistudio",
    DriverProvider.MIMO: "mimo",
}

AISTUDIO_MODEL_OVERRIDE_SUFFIX_RE = re.compile(r"-(minimal|low|medium|high|r[0-4])$")


def get_legacy_model_ids(provider: DriverProvider) -> list[str]:
    default_ids = LEGACY_MODEL_IDS_BY_PROVIDER[DriverProvider.DEEPSEEK]
    return list(LEGACY_MODEL_IDS_BY_PROVIDER.get(provider, default_ids))


def get_legacy_model_prefix(provider: DriverProvider) -> str:
    return LEGACY_MODEL_PREFIX_BY_PROVIDER.get(provider, "deepseek")


def get_owned_by_for_provider(provider: DriverProvider) -> str:
    return OWNED_BY_PROVIDER.get(provider, "deepseek")


def get_real_model_api_prefix(provider: DriverProvider) -> str:
    return get_legacy_model_prefix(provider)


def _with_real_model_api_prefix(provider: DriverProvider, base_id: str) -> str:
    safe_base = str(base_id or "").strip("-")
    provider_prefix = get_real_model_api_prefix(provider)
    if not safe_base or not provider_prefix:
        return safe_base
    if safe_base == provider_prefix or safe_base.startswith(f"{provider_prefix}-"):
        return safe_base
    return f"{provider_prefix}-{safe_base}"


def is_umm_enabled(config_manager: Any) -> bool:
    try:
        return bool(config_manager.get_setting("network_settings", "enable_umm"))
    except Exception:
        return False


def _get_universal_mode_map(provider: DriverProvider) -> Dict[str, str]:
    if provider == DriverProvider.DEEPSEEK:
        return {
            **UMM_MODE_BY_MODEL_ID,
            **DEEPSEEK_UMM_EXPERT_MODE_BY_MODEL_ID,
        }
    return UMM_MODE_BY_MODEL_ID


def normalize_real_model_api_base(label: Any) -> str:
    normalized = str(label or "").strip().lower()
    if not normalized:
        return ""

    normalized = re.sub(r"[\s.]+", "-", normalized)
    normalized = re.sub(r"[^a-z0-9-]+", "", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-")


def _real_model_id_bases_for_label(
    label: Any,
    *,
    provider: DriverProvider | None = None,
    include_base: bool = True,
    include_provider_prefix: bool = False,
) -> list[str]:
    base_id = normalize_real_model_api_base(label)
    if not base_id:
        return []

    raw_label = str(label or "").strip().lower()
    raw_dot_id = re.sub(r"\s+", "-", raw_label)
    raw_dot_id = re.sub(r"[^a-z0-9.-]+", "", raw_dot_id).strip("-")

    base_ids = []
    if include_base:
        base_ids.append(base_id)
        if raw_dot_id and raw_dot_id != base_id and raw_dot_id not in base_ids:
            base_ids.append(raw_dot_id)

    if include_provider_prefix and provider is not None:
        for bid in list(base_ids):
            prefixed_id = _with_real_model_api_prefix(provider, bid)
            if prefixed_id and prefixed_id not in base_ids:
                base_ids.append(prefixed_id)
    return base_ids


def _real_model_label_map(
    real_model_labels: Iterable[str] | None,
    *,
    provider: DriverProvider | None = None,
    include_base: bool = True,
    include_provider_prefix: bool = False,
) -> Dict[str, str]:
    label_map: Dict[str, str] = {}
    for raw_label in real_model_labels or ():
        label = str(raw_label or "").strip()
        base_ids = _real_model_id_bases_for_label(
            label,
            provider=provider,
            include_base=include_base,
            include_provider_prefix=include_provider_prefix,
        )
        if not label or not base_ids:
            continue
        for base_id in base_ids:
            label_map.setdefault(base_id, label)
            for suffix, _mode in REAL_MODEL_SUFFIX_MODE_BY_SUFFIX:
                label_map.setdefault(f"{base_id}{suffix}", label)
    return label_map


def _real_model_mode_map(
    real_model_labels: Iterable[str] | None,
    *,
    provider: DriverProvider | None = None,
    include_base: bool = True,
    include_provider_prefix: bool = False,
) -> Dict[str, str]:
    mode_map: Dict[str, str] = {}
    for raw_label in real_model_labels or ():
        label = str(raw_label or "").strip()
        base_ids = _real_model_id_bases_for_label(
            label,
            provider=provider,
            include_base=include_base,
            include_provider_prefix=include_provider_prefix,
        )
        if not label or not base_ids:
            continue
        for base_id in base_ids:
            mode_map.setdefault(base_id, MODE_AUTO)
            for suffix, mode in REAL_MODEL_SUFFIX_MODE_BY_SUFFIX:
                mode_map.setdefault(f"{base_id}{suffix}", mode)
    return mode_map


def get_real_model_ids_for_labels(real_model_labels: Iterable[str] | None) -> list[str]:
    return list(_real_model_label_map(real_model_labels).keys())


def get_real_model_ids_for_provider(
    provider: DriverProvider,
    real_model_labels: Iterable[str] | None,
    *,
    include_provider_prefix: bool = False,
    prefixed_model_ids: Iterable[str] | None = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    ids_to_prefix = set(prefixed_model_ids or ())

    for raw_label in real_model_labels or ():
        label = str(raw_label or "").strip()
        base_id = normalize_real_model_api_base(label)
        if not label or not base_id:
            continue

        for suffix, _mode in REAL_MODEL_SUFFIX_MODE_BY_SUFFIX:
            output_base_id = base_id
            unprefixed_model_id = f"{base_id}{suffix}"
            if include_provider_prefix or unprefixed_model_id in ids_to_prefix:
                output_base_id = _with_real_model_api_prefix(provider, base_id)

            model_id = f"{output_base_id}{suffix}"
            if model_id not in seen:
                seen.add(model_id)
                out.append(model_id)

    return out


def _parallel_real_model_ids_requiring_prefix(
    providers: Iterable[DriverProvider],
    real_model_labels_by_provider: Mapping[DriverProvider, Iterable[str]] | None,
) -> set[str]:
    occurrences: Dict[str, set[DriverProvider]] = {}

    for provider in providers:
        for model_id in get_legacy_model_ids(provider):
            occurrences.setdefault(model_id, set()).add(provider)

    if not real_model_labels_by_provider:
        return set()

    for provider in providers:
        if provider not in REAL_MODEL_ID_PROVIDERS:
            continue
        real_model_ids = get_real_model_ids_for_provider(
            provider,
            real_model_labels_by_provider.get(provider),
        )
        for model_id in real_model_ids:
            occurrences.setdefault(model_id, set()).add(provider)

    return {
        model_id
        for model_id, model_providers in occurrences.items()
        if len(model_providers) > 1
    }


def _iter_unique_provider_model_ids(
    items: Iterable[tuple[DriverProvider, str]],
) -> list[tuple[DriverProvider, str]]:
    out: list[tuple[DriverProvider, str]] = []
    seen: set[tuple[DriverProvider, str]] = set()

    for provider, model_id in items:
        key = (provider, model_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)

    return out


def _providers_for_model_id(
    items: Iterable[tuple[DriverProvider, str]],
    model: Any,
) -> set[DriverProvider]:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return set()

    providers: set[DriverProvider] = set()
    for provider, model_id in items:
        if str(model_id or "").strip().lower() == normalized:
            providers.add(provider)
    return providers


def _get_parallel_model_ids_for_providers_impl(
    providers: Iterable[DriverProvider],
    config_manager: Any,
    *,
    real_model_labels_by_provider: Mapping[DriverProvider, Iterable[str]] | None = None,
) -> list[tuple[DriverProvider, str]]:
    provider_list = list(providers)
    items: list[tuple[DriverProvider, str]] = []
    for provider in provider_list:
        items.extend((provider, model_id) for model_id in get_legacy_model_ids(provider))

    if not is_umm_enabled(config_manager):
        return _iter_unique_provider_model_ids(items)

    prefixed_model_ids = _parallel_real_model_ids_requiring_prefix(
        provider_list,
        real_model_labels_by_provider,
    )

    for provider in provider_list:
        if provider not in REAL_MODEL_ID_PROVIDERS:
            continue
        labels = None
        if real_model_labels_by_provider:
            labels = real_model_labels_by_provider.get(provider)
        real_model_ids = get_real_model_ids_for_provider(
            provider,
            labels,
            prefixed_model_ids=prefixed_model_ids,
        )
        items.extend((provider, model_id) for model_id in real_model_ids)

    return _iter_unique_provider_model_ids(items)


def get_parallel_model_ids_for_providers(
    providers: Iterable[DriverProvider],
    config_manager: Any,
    *,
    real_model_labels_by_provider: Mapping[DriverProvider, Iterable[str]] | None = None,
) -> list[tuple[DriverProvider, str]]:
    return _get_parallel_model_ids_for_providers_impl(
        providers,
        config_manager,
        real_model_labels_by_provider=real_model_labels_by_provider,
    )


def resolve_parallel_provider_from_model_id(
    model: Any,
    providers: Iterable[DriverProvider],
    config_manager: Any,
    *,
    real_model_labels_by_provider: Mapping[DriverProvider, Iterable[str]] | None = None,
) -> DriverProvider | None:
    matches = _providers_for_model_id(
        _get_parallel_model_ids_for_providers_impl(
            providers,
            config_manager,
            real_model_labels_by_provider=real_model_labels_by_provider,
        ),
        model,
    )
    if len(matches) == 1:
        return next(iter(matches))
    return None


def resolve_real_model_label_from_model_id(
    provider: DriverProvider,
    model: Any,
    real_model_labels: Iterable[str] | None,
    *,
    require_provider_prefix: bool = False,
) -> str | None:
    normalized = str(model or "").strip().lower()
    if not normalized or provider not in REAL_MODEL_ID_PROVIDERS:
        return None

    if provider == DriverProvider.AI_STUDIO:
        normalized = _strip_aistudio_override_suffix(normalized)

    label_map = _real_model_label_map(
        real_model_labels,
        provider=provider,
        include_base=not require_provider_prefix,
        include_provider_prefix=True,
    )
    res = label_map.get(normalized)
    if res is None and "." in normalized:
        res = label_map.get(normalized.replace(".", "-"))
    return res


def get_model_ids_for_provider(
    provider: DriverProvider,
    config_manager: Any,
    *,
    force_legacy: bool = False,
    real_model_labels: Iterable[str] | None = None,
) -> list[str]:
    if (not force_legacy) and is_umm_enabled(config_manager):
        model_ids = list(UMM_MODEL_IDS)
        if provider == DriverProvider.DEEPSEEK:
            model_ids.extend(DEEPSEEK_UMM_EXPERT_MODEL_IDS)
        if provider in REAL_MODEL_ID_PROVIDERS:
            model_ids.extend(get_real_model_ids_for_labels(real_model_labels))
        return model_ids
    return get_legacy_model_ids(provider)


def get_model_ids_for_providers(
    providers: Iterable[DriverProvider],
    config_manager: Any,
    *,
    force_legacy: bool = False,
    real_model_labels_by_provider: Mapping[DriverProvider, Iterable[str]] | None = None,
) -> list[tuple[DriverProvider, str]]:
    items: list[tuple[DriverProvider, str]] = []
    for provider in providers:
        real_model_labels = None
        if real_model_labels_by_provider:
            real_model_labels = real_model_labels_by_provider.get(provider)
        model_ids = get_model_ids_for_provider(
            provider,
            config_manager,
            force_legacy=force_legacy,
            real_model_labels=real_model_labels,
        )
        items.extend((provider, model_id) for model_id in model_ids)
    return items


def _strip_aistudio_override_suffix(model: str) -> str:
    normalized = str(model or "").strip().lower()
    return AISTUDIO_MODEL_OVERRIDE_SUFFIX_RE.sub("", normalized)


def is_supported_model_id(
    provider: DriverProvider,
    model: Any,
    config_manager: Any = None,
    *,
    real_model_labels: Iterable[str] | None = None,
) -> bool:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return False

    if is_umm_enabled(config_manager) and normalized in _get_universal_mode_map(provider):
        return True

    if is_umm_enabled(config_manager) and provider in REAL_MODEL_ID_PROVIDERS:
        real_normalized = normalized
        if provider == DriverProvider.AI_STUDIO:
            real_normalized = _strip_aistudio_override_suffix(real_normalized)
        if real_normalized in _real_model_mode_map(
            real_model_labels,
            provider=provider,
            include_provider_prefix=True,
        ):
            return True

    legacy_map = LEGACY_MODE_BY_PROVIDER.get(provider) or {}
    if normalized in legacy_map:
        return True

    if provider == DriverProvider.AI_STUDIO:
        stripped = _strip_aistudio_override_suffix(normalized)
        return stripped in legacy_map

    return False


def resolve_provider_from_model_id(
    model: Any,
    *,
    config_manager: Any = None,
    real_model_labels_by_provider: Mapping[DriverProvider, Iterable[str]] | None = None,
) -> DriverProvider | None:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None

    for provider in LEGACY_MODE_BY_PROVIDER:
        legacy_model = normalized
        if provider == DriverProvider.AI_STUDIO:
            legacy_model = _strip_aistudio_override_suffix(legacy_model)
        legacy_map = LEGACY_MODE_BY_PROVIDER.get(provider) or {}
        if legacy_model in legacy_map:
            return provider

    if not is_umm_enabled(config_manager):
        return None

    if not real_model_labels_by_provider:
        return None

    return resolve_parallel_provider_from_model_id(
        normalized,
        real_model_labels_by_provider.keys(),
        config_manager,
        real_model_labels_by_provider=real_model_labels_by_provider,
    )


def resolve_behavior_mode(
    model: Any,
    provider: DriverProvider,
    *,
    real_model_labels: Iterable[str] | None = None,
) -> str:
    """
    Resolve a requested OpenAI-style `model` string into an IntenseRP behavior mode.

    - MODE_AUTO: use the user's behavior settings
    - MODE_CHAT: force reasoning off
    - MODE_REASONER: force reasoning on

    Unknown values fall back to MODE_AUTO. Google AI Studio also accepts its
    legacy thinking-level suffixes (for example `aistudio-auto-high`).
    """

    normalized = str(model or "").strip().lower()
    if not normalized:
        return MODE_AUTO

    universal_mode = _get_universal_mode_map(provider).get(normalized)
    if universal_mode:
        return universal_mode

    real_normalized = normalized
    if provider == DriverProvider.AI_STUDIO:
        real_normalized = _strip_aistudio_override_suffix(real_normalized)
    real_model_mode = _real_model_mode_map(
        real_model_labels,
        provider=provider,
        include_provider_prefix=True,
    ).get(real_normalized)
    if real_model_mode:
        return real_model_mode

    if provider == DriverProvider.AI_STUDIO:
        normalized = _strip_aistudio_override_suffix(normalized)

    legacy_map = LEGACY_MODE_BY_PROVIDER.get(provider) or {}
    legacy_mode = legacy_map.get(normalized)
    if legacy_mode:
        return legacy_mode

    return MODE_AUTO


def resolve_deepseek_model_type(model: Any, provider: DriverProvider) -> str:
    normalized = str(model or "").strip().lower()
    if provider != DriverProvider.DEEPSEEK or not normalized:
        return DEEPSEEK_MODEL_TYPE_DEFAULT

    if (
        normalized in DEEPSEEK_UMM_EXPERT_MODE_BY_MODEL_ID
        or normalized.startswith("deepseek-expert-")
    ):
        return DEEPSEEK_MODEL_TYPE_EXPERT

    return DEEPSEEK_MODEL_TYPE_DEFAULT


def build_openai_model_list(ids: Iterable[str], owned_by: str) -> List[Dict[str, Any]]:
    safe_owned = str(owned_by or "").strip() or "unknown"
    out: List[Dict[str, Any]] = []
    for mid in ids:
        sid = str(mid or "").strip()
        if not sid:
            continue
        out.append({"id": sid, "object": "model", "created": 0, "owned_by": safe_owned})
    return out
