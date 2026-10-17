---
icon: material/frequently-asked-questions
---

# :material-frequently-asked-questions: FAQ

Quick answers to the questions that come up the most. If you need a step-by-step checklist, start with :material-bug: [Troubleshooting](troubleshooting.md).

---

## :material-help-circle: General

??? question "What is IntenseRP Next v2?"
    A local desktop app + OpenAI-compatible API that lets clients like SillyTavern talk to provider web chats (DeepSeek / GLM / Moonshot / QwenLM / Perplexity / HuggingChat / Google AI Studio) by driving them in a real browser.

??? question "Is this an official DeepSeek or SillyTavern project?"
    Nope. This is a third-party tool and is not affiliated with DeepSeek, SillyTavern, or any provider.

??? question "Does this use provider API keys?"
    No. IntenseRP uses provider web apps you log into in the browser (DeepSeek / GLM / Moonshot / QwenLM / Perplexity / HuggingChat / Google AI Studio). If you need a production-grade or officially supported integration, use the provider's official API.

??? question "What platforms are supported?"
    Windows and Linux are the supported targets right now. A graphical desktop is required because it launches a real browser.

??? question "Why does it open a browser window?"
    Because that's how it works: IntenseRP actually drives the provider UI (and intercepts the underlying network stream). It is not a headless, server-only API.

---

## :material-power-plug: Setup & usage

??? question "What endpoint do I paste into SillyTavern?"
    By default:

    `http://127.0.0.1:7777/v1`

    If you change the port in Settings, update the endpoint to match.

??? question "Do I need to keep the browser open?"
    Yes - keep the provider browser window open while you use IntenseRP. You can minimize it, but if you close it you'll usually need to press Start again.

??? question "Which model should I pick?"
    Use `<provider>-auto` for your active provider. The current prefixes are `deepseek`, `glm`, `moonshot`, `qwen`, `perplexity`, `huggingchat`, `aistudio`, and `mimo`.

    If you want to force the behavior, use the same prefix with `-chat` or `-reasoner` instead.

??? question "Why are responses slow sometimes?"
    Two common reasons:

    - IntenseRP processes one request at a time by default (requests are queued)
    - DeepThink can add extra time

    For details, see :material-api: [API Behavior](../advanced/api-behavior.md).

??? question "My character names look wrong (or always say User/Character)."
    This is usually just name passing/detection. Start here: :material-format-text: [Formatting](../features/formatting.md).

---

## :material-lan: Networking & security

??? question "Can I connect from my phone or another PC?"
    Yes - enable **Allow Local Network Access**, then use your computer's LAN IP in the endpoint (example: `http://192.168.1.100:7777/v1`). See :material-lan: [Network & API](../features/network-api.md).

??? question "Can I expose IntenseRP to the internet?"
    Not recommended. IntenseRP is designed for local/LAN use. If you need remote access, use a VPN and consider enabling API keys.

??? question "Why am I getting 401 Unauthorized?"
    You most likely enabled **Require API Keys** in Settings. Either disable it, or put the same key into your client. See :material-key-variant: [API Keys](../features/network-api.md#require-api-keys).

---

## :material-lock: Privacy & data

??? question "Does IntenseRP store my provider password?"
    Only if you enable **Sign In Automatically**. Your config directory contains sensitive data (credentials, API keys, cookies), so treat it like a password vault. If you prefer, leave it off and just log in manually.

??? question "Where are my settings and sessions saved?"
    In your config directory (`[config_dir]`). If you enabled Persistent Sessions, it can also contain saved browser profiles for your providers. See :material-cog: [System](../features/system.md).

---

## :material-bug: Troubleshooting & support

??? question "The first launch takes forever - is it frozen?"
    Usually not. On first run it may download/verify browser components. Let it finish once - subsequent launches are much faster.

??? question "What's the best way to report a bug?"
    GitHub issues are best. Include your version, OS, exact steps to reproduce, and logs (redacted). Start with :material-bug: [Troubleshooting](troubleshooting.md) for a checklist and a copy/paste template.

??? question "I still need help - where do I reach you?"
    For help, bug reports, and discussions, open an issue on the [GitHub repository](https://github.com/Lemomation/irn/issues).

    For more details, see :material-email: [Means of Contact](contact.md).

---

## :material-arrow-left: Back to Home

[:material-arrow-left: Home](../index.md)
