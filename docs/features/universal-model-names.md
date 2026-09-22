---
icon: material/tag-text-outline
---

# :material-tag-text-outline: Universal Model Names

This setting gives IntenseRP the same 3 API model IDs in normal single-provider mode, no matter which provider is active. It's **off** by default, since it does get a bit confusing at first, but it can be a nice quality-of-life improvement if you switch providers often.

!!! note "Providers in Parallel is stricter"
    If you have **Providers in Parallel** enabled, `intenserp-*` stays invalid because the router still needs to know which provider should handle the request.

    Real-model IDs can still appear there when this setting is enabled. If an exact real-model ID would belong to more than 1 active provider, IntenseRP prefixes that conflicting ID with the provider name, like `aistudio-gemini-3-1-pro-reasoner`.

:material-arrow-right: **Settings** -> **API Server** -> **Model IDs** -> **Use Universal Model Names**

In normal single-provider mode, `GET /v1/models` returns:

- `intenserp-auto`
- `intenserp-reasoner`
- `intenserp-chat`

So if you switch or hotswap providers, you don't also have to go change the model name in SillyTavern or another client. Nice and boring, in a good way.

For providers with a real model picker, IntenseRP also lists the available real models as request-level overrides. This applies to GLM Chat, Google AI Studio, QwenLM, Perplexity, HuggingChat, and Xiaomi MiMo.

Those IDs are lowercase, with spaces and dots converted to `-`, and they keep the normal suffixes. For example, a GLM model named **GLM-5.3** appears as `glm-5-3-auto`, `glm-5-3-reasoner`, and `glm-5-3-chat`. MiMo's **MiMo-V2.6-Pro** appears as `mimo-v2-6-pro-auto`, `mimo-v2-6-pro-reasoner`, and `mimo-v2-6-pro-chat`.

In **Providers in Parallel**, only conflicting real-model IDs get a provider prefix. So a unique Perplexity or HuggingChat model can stay short, while an overlap like **Gemini 3.1 Pro** under both Google AI Studio and Perplexity becomes provider-prefixed for those providers.

`intenserp-*` still uses the model selected in Settings. A real-model ID switches the provider UI to that model for the request, then applies the suffix behavior on top.

Before you use it, though, there are some important things to know:

- Provider-specific IDs like `deepseek-auto` or `glm-chat` still work even when this is enabled.
- `intenserp-reasoner` is the universal version of the normal `*-reasoner` behavior preset.
- Real-model IDs are only visible and accepted while **Use Universal Model Names** is enabled.

!!! note "This replaces Better Model Names"
    The old experimental **Better Model Names** setting was removed in `v2.6.3`. Universal Model Names now covers that job too, without making it a separate experimental switch.

---

[:material-arrow-left: Network & API](network-api.md)
