from typing import Dict, Any
from config.formatting_presets import LEGACY_V2_FORMATTING_PRESET_MAP
from drivers.providers import DriverProvider


REMOVED_FIELDS = {
    ("application_settings", "paged_settings_view"),
    ("application_settings", "show_only_active_provider_behavior"),
    ("experimental", "better_model_names"),
    ("experimental", "providers_in_parallel"),
    ("experimental", "providers_in_parallel_note"),
    ("experimental", "parallel_concurrent_launch"),
    ("experimental", "parallel_launch_in_batches"),
    ("experimental", "parallel_launch_batch_size"),
    ("experimental", "parallelize_request_queue"),
    ("experimental", "parallelize_request_queue_note"),
    ("experimental", "full_parallelization"),
    ("experimental", "full_parallelization_note"),
    ("experimental", "parallel_enable_deepseek"),
    ("experimental", "parallel_instances_deepseek"),
    ("experimental", "parallel_enable_glm"),
    ("experimental", "parallel_instances_glm"),
    ("experimental", "parallel_enable_moonshot"),
    ("experimental", "parallel_instances_moonshot"),
    ("experimental", "parallel_enable_qwen"),
    ("experimental", "parallel_instances_qwen"),
    ("experimental", "parallel_enable_perplexity"),
    ("experimental", "parallel_instances_perplexity"),
    ("experimental", "parallel_enable_huggingchat"),
    ("experimental", "parallel_instances_huggingchat"),
    ("experimental", "parallel_enable_aistudio"),
    ("experimental", "parallel_instances_aistudio"),
}

GLM_MODEL_RENAMES = {
    "GLM-4.6": "GLM-5.1",
    "GLM-5": "GLM-5.2",
}

MIMO_MODEL_RENAMES = {
    "MiMo-V2.5-Pro": "MiMo-V2.6-Pro",
    "MiMo-V2.5": "MiMo-V2.6-Pro",
}

LEGACY_PARALLEL_PROVIDER_FIELD_BY_PROVIDER = {
    DriverProvider.DEEPSEEK: "parallel_enable_deepseek",
    DriverProvider.GLM_CHAT: "parallel_enable_glm",
    DriverProvider.MOONSHOT: "parallel_enable_moonshot",
    DriverProvider.QWEN_LM: "parallel_enable_qwen",
    DriverProvider.PERPLEXITY: "parallel_enable_perplexity",
    DriverProvider.HUGGINGCHAT: "parallel_enable_huggingchat",
    DriverProvider.AI_STUDIO: "parallel_enable_aistudio",
}

LEGACY_PARALLEL_PROVIDER_INSTANCE_FIELD_BY_PROVIDER = {
    DriverProvider.DEEPSEEK: "parallel_instances_deepseek",
    DriverProvider.GLM_CHAT: "parallel_instances_glm",
    DriverProvider.MOONSHOT: "parallel_instances_moonshot",
    DriverProvider.QWEN_LM: "parallel_instances_qwen",
    DriverProvider.PERPLEXITY: "parallel_instances_perplexity",
    DriverProvider.HUGGINGCHAT: "parallel_instances_huggingchat",
    DriverProvider.AI_STUDIO: "parallel_instances_aistudio",
}

MAX_PARALLEL_PROVIDER_INSTANCES = 32


def migrate_glm_model_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return GLM_MODEL_RENAMES.get(value, value)


def migrate_glm_behavior_settings(raw_settings: Any) -> bool:
    if not isinstance(raw_settings, dict):
        return False

    raw_model = raw_settings.get("model")
    migrated_model = migrate_glm_model_value(raw_model)
    if migrated_model == raw_model:
        return False

    raw_settings["model"] = migrated_model
    return True


def migrate_mimo_model_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return MIMO_MODEL_RENAMES.get(value, value)


def migrate_mimo_behavior_settings(raw_settings: Any) -> bool:
    if not isinstance(raw_settings, dict):
        return False

    raw_model = raw_settings.get("model")
    migrated_model = migrate_mimo_model_value(raw_model)
    if migrated_model == raw_model:
        return False

    raw_settings["model"] = migrated_model
    return True


def _clamp_parallel_instance_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(MAX_PARALLEL_PROVIDER_INSTANCES, count))


def migrate_parallel_runtime_settings(settings: Dict[str, Any]) -> None:
    experimental = settings.get("experimental")
    if not isinstance(experimental, dict):
        return

    has_legacy_parallel_settings = any(
        key in experimental
        for key in (
            "providers_in_parallel",
            "parallelize_request_queue",
            "full_parallelization",
            "parallel_concurrent_launch",
            "parallel_launch_in_batches",
            "parallel_launch_batch_size",
            *LEGACY_PARALLEL_PROVIDER_FIELD_BY_PROVIDER.values(),
            *LEGACY_PARALLEL_PROVIDER_INSTANCE_FIELD_BY_PROVIDER.values(),
        )
    )
    if not has_legacy_parallel_settings:
        return

    runtime = settings.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        settings["runtime"] = runtime

    if "providers_in_parallel" not in runtime and "providers_in_parallel" in experimental:
        runtime["providers_in_parallel"] = bool(experimental.get("providers_in_parallel"))

    if "parallelization_mode" not in runtime:
        if bool(experimental.get("full_parallelization")):
            runtime["parallelization_mode"] = "full_parallel_lanes"
        elif bool(experimental.get("parallelize_request_queue")):
            runtime["parallelization_mode"] = "concurrent_provider_lanes"
        else:
            runtime["parallelization_mode"] = "provider_lanes"

    if "parallel_provider_lanes" not in runtime:
        selected_providers = []
        instance_counts = {}
        for provider, enable_key in LEGACY_PARALLEL_PROVIDER_FIELD_BY_PROVIDER.items():
            if bool(experimental.get(enable_key)):
                selected_providers.append(provider.value)

            count_key = LEGACY_PARALLEL_PROVIDER_INSTANCE_FIELD_BY_PROVIDER.get(provider)
            if count_key:
                instance_counts[provider.value] = _clamp_parallel_instance_count(
                    experimental.get(count_key)
                )

        runtime["parallel_provider_lanes"] = {
            "providers": selected_providers,
            "instances": instance_counts,
        }

    for key in (
        "parallel_concurrent_launch",
        "parallel_launch_in_batches",
        "parallel_launch_batch_size",
    ):
        if key in runtime or key not in experimental:
            continue
        if key == "parallel_launch_batch_size":
            runtime[key] = _clamp_parallel_instance_count(experimental.get(key))
        else:
            runtime[key] = bool(experimental.get(key))


class SettingsMigrator:
    @staticmethod
    def migrate(settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Migrates settings from older versions to the current version.
        Modifies the settings dictionary in-place.
        """
        
        # Migration: Preset Variants (Non-Role/Name to Role/Name)
        if "formatting" in settings:
            formatting = settings["formatting"]
            if "formatting_preset" in formatting:
                preset = formatting["formatting_preset"]
                formatting["formatting_preset"] = LEGACY_V2_FORMATTING_PRESET_MAP.get(preset, preset)

        # Migration: enable_console moved from system_settings -> console_settings
        system_settings = settings.get("system_settings")
        if isinstance(system_settings, dict) and "enable_console" in system_settings:
            enable_console = system_settings.pop("enable_console")
            console_settings = settings.setdefault("console_settings", {})
            if isinstance(console_settings, dict) and "enable_console" not in console_settings:
                console_settings["enable_console"] = enable_console

        # Migration: Base Driver provider moved to providers_credentials.provider
        base_driver = settings.get("base_driver")
        if isinstance(base_driver, dict) and "provider" in base_driver:
            provider = base_driver.get("provider")
            providers_credentials = settings.setdefault("providers_credentials", {})
            if isinstance(providers_credentials, dict) and not providers_credentials.get("provider"):
                providers_credentials["provider"] = provider

        # Migration: normalize provider value to canonical enum label
        providers_credentials = settings.get("providers_credentials")
        if isinstance(providers_credentials, dict):
            raw_provider = providers_credentials.get("provider")
            providers_credentials["provider"] = DriverProvider.from_setting(raw_provider).value

        # Migration: removed GLM / MiMo model labels -> current supported labels
        migrate_glm_behavior_settings(settings.get("glm_behavior"))
        migrate_mimo_behavior_settings(settings.get("mimo_behavior"))

        loadouts_root = settings.get("loadouts")
        if isinstance(loadouts_root, dict):
            raw_definitions = loadouts_root.get("definitions")
            if isinstance(raw_definitions, list):
                for definition in raw_definitions:
                    if not isinstance(definition, dict):
                        continue
                    raw_provider = definition.get("provider")
                    if raw_provider is None:
                        continue
                    provider = DriverProvider.from_setting(str(raw_provider))
                    if provider is DriverProvider.GLM_CHAT:
                        migrate_glm_behavior_settings(definition.get("settings"))
                    elif provider is DriverProvider.MIMO:
                        migrate_mimo_behavior_settings(definition.get("settings"))

        # Migration: account engine toggles moved from Experimental -> Providers & Credentials
        experimental = settings.get("experimental")
        if isinstance(experimental, dict):
            select_least_used = experimental.get("ece_select_least_used")
            reload_on_failure = experimental.get("ece_reauth_on_no_content")
            better_model_names = experimental.get("better_model_names")

            providers_credentials = settings.setdefault("providers_credentials", {})
            if isinstance(providers_credentials, dict):
                if "select_least_used" not in providers_credentials and select_least_used is not None:
                    providers_credentials["select_least_used"] = bool(select_least_used)
                if "reload_on_failure" not in providers_credentials and reload_on_failure is not None:
                    providers_credentials["reload_on_failure"] = bool(reload_on_failure)

            network_settings = settings.setdefault("network_settings", {})
            if isinstance(network_settings, dict):
                if "enable_umm" not in network_settings and better_model_names is not None:
                    network_settings["enable_umm"] = bool(better_model_names)

        # Migration: Hotswap no longer lives in the Stop button chevron menu.
        application_settings = settings.get("application_settings")
        if isinstance(application_settings, dict):
            hotswap_experience = str(application_settings.get("hotswap_experience") or "").strip()
            if hotswap_experience in {"Stop Menu", "Chevron Menu"}:
                application_settings["hotswap_experience"] = "Discrete"

        # Migration: Providers in Parallel moved from Experimental into Browser & Runtime.
        migrate_parallel_runtime_settings(settings)

        # Migration: remove obsolete settings that no longer exist in the UI
        for category_key, field_key in REMOVED_FIELDS:
            category = settings.get(category_key)
            if isinstance(category, dict):
                category.pop(field_key, None)

        # Future migrations will be added here
        # e.g. v1 to v2
        
        return settings
