<p align="center">
  <img src=".github/images/logo-strip.png" alt="IntenseRP Next" />
</p>

<h1 align="center">IntenseRP Next v2 (Maintained)</h1>

<p align="center">
  <b>A local OpenAI-compatible bridge between SillyTavern and browser-based AI chat providers.</b><br>
  <i>Drives provider web UIs directly in Chromium via Patchright/Playwright — no official API keys required.</i>
</p>

<p align="center">
  <a href="https://github.com/Lemomation/irn/releases"><img alt="Latest Release" src="https://img.shields.io/github/v/release/Lemomation/irn?style=flat-square&color=success" /></a>
  <a href="https://github.com/Lemomation/irn/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/Lemomation/irn?style=flat-square" /></a>
  <a href="https://github.com/Lemomation/irn/issues"><img alt="Issues" src="https://img.shields.io/github/issues/Lemomation/irn?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/Lemomation/irn?style=flat-square" /></a>
</p>

<p align="center">
  <a href="#-quick-download--releases"><b>Download</b></a> ·
  <a href="#welcome-">Welcome</a> ·
  <a href="#what-is-this">What is this?</a> ·
  <a href="#connect-sillytavern-or-any-openai-compatible-client">Client setup</a> ·
  <a href="#provider-support">Providers</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

> [!IMPORTANT]
> ### 📢 Actively Maintained Continuation
> The original repository (`LyubomirT/intense-rp-next`) was archived, and subsequent upstream branches (`Phobeuscz/irn`) are currently inactive with issues disabled.
> 
> Because provider web interfaces change constantly, **this repository (`Lemomation/irn`) is the actively maintained continuation.**
> 
> **Recent additions in this fork:**
> - ⚡ **Google AI Studio**: Gemini 3.8 Flash, 3.7 Flash, and 3.6 Flash support.
> - 🤖 **Xiaomi MiMo**: Full driver support for `MiMo-V2.6-Pro` and `MiMo-V2.6-Flash` with automatic fallback/migration for retired 2.5 checkpoints.
> - 🧠 **Zhipu AI GLM Chat**: `GLM-5.3` and `GLM-5.3-Flash` support, plus thinking effort levels (`Low`, `High`, `Max`).
> - 🔮 **Alibaba Qwen**: `Qwen3.8-Max` and `Qwen3.8-Omni-Flash` support.
> - 🧹 **Retired Model Filter**: Toggle (`show_deprecated_models`) to keep dropdown lists clean while preserving existing configs.
> - 🔄 **Built-in Auto-Updater**: Pointed to this repo so you receive new fixes and models automatically.
> 
> 📅 **Release Cadence**: When new models drop or website selectors break, fixes are added over the **weekend**. Please be patient!

---

## 📥 Quick Download & Releases

Pre-compiled desktop releases with the built-in auto-updater:

👉 **[Download the Latest Windows / Linux Build on GitHub Releases](https://github.com/Lemomation/irn/releases)**

---

## 🎬 Preview Video

https://github.com/user-attachments/assets/ebf1bfcd-3b23-4614-b584-174791bcb004

<p align="center">
  <a href="https://github.com/Lemomation/irn"><img src=".github/svgs/star2.svg" alt="Leave a Star" height="42"></a>
</p>

## Welcome 👋

If you need a local bridge between SillyTavern and browser-based provider chats, IntenseRP Next v2 drives supported web apps (DeepSeek, Xiaomi MiMo, GLM Chat, Kimi / Moonshot, QwenLM, Perplexity, HuggingChat, and Google AI Studio) in a real browser session, and re-exposes that session as a standard OpenAI-compatible `/v1` endpoint.

It mirrors provider web UI controls such as reasoning/thinking toggles, web search, file uploads, and model selection.

## Running From Source 🎁

Requirements: Python 3.12+ (3.12 or 3.13 recommended)

```bash
git clone https://github.com/Lemomation/irn.git
cd irn

python -m venv venv

# Linux / Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

1. Click **Start** in the desktop app and log in when the browser window opens.
2. Point your SillyTavern client at `http://127.0.0.1:7777/v1` (default).
3. Select your model and chat away!

## What is this?

IntenseRP Next v2 is a local desktop bridge between:
- an OpenAI-style client (like SillyTavern), and
- a supported provider web app (DeepSeek, MiMo, GLM Chat, Kimi, QwenLM, Perplexity, HuggingChat, Google AI Studio).

Under the hood it:
1. Starts a local FastAPI server (OpenAI-compatible routes under `/v1`)
2. Launches an automated Chromium session (Patchright/Playwright)
3. Preserves your login session via local cookies/profiles
4. Captures the provider's streaming responses from the web UI
5. Re-emits them as OpenAI-style SSE deltas for your frontend client

## Connect SillyTavern (or any OpenAI-compatible client)

Once the app says **Running (Port 7777)**:

| Setting | Value |
|---|---|
| **Endpoint** | `http://127.0.0.1:7777/v1` |
| **API** | OpenAI-compatible chat or text completions |
| **API Key** | Leave blank (unless you explicitly enabled API keys in Settings) |
| **Model** | Provider model IDs (`mimo-*`, `deepseek-*`, `glm-*`, `moonshot-*`, `qwen-*`, `perplexity-*`, `huggingchat-*`, `aistudio-*`) |

### Available Model ID Presets

- **Xiaomi MiMo**:
  - `mimo-auto` (uses your IntenseRP settings)
  - `mimo-chat` (standard chat)
  - `mimo-reasoner` (thinking mode)
- **DeepSeek**:
  - `deepseek-auto`, `deepseek-chat`, `deepseek-reasoner`
  - `deepseek-expert-auto`, `deepseek-expert-chat`, `deepseek-expert-reasoner`
- **Google AI Studio**:
  - `aistudio-auto`, `aistudio-chat`, `aistudio-reasoner` (Gemini 3.8 / 3.7 / 3.6 / 2.5)
- **GLM Chat (Z.ai)**:
  - `glm-auto`, `glm-chat`, `glm-reasoner` (supports Low / High / Max thinking levels)
- **Alibaba Qwen**:
  - `qwen-auto`, `qwen-chat`, `qwen-reasoner` (Qwen 3.8-Max, 3.8-Omni-Flash, etc.)
- **Kimi / Moonshot**:
  - `moonshot-auto`, `moonshot-chat`, `moonshot-reasoner`
- **Perplexity**:
  - `perplexity-auto`, `perplexity-chat`, `perplexity-reasoner`
- **HuggingChat**:
  - `huggingchat-auto`, `huggingchat-chat`, `huggingchat-reasoner`

> [!TIP]
> Enable **Universal Model Names** in Settings to use generic IDs (`intenserp-auto`, `intenserp-chat`, `intenserp-reasoner`) across all providers.

## Provider Support

| Provider | Status | Models & Features |
|---|---|---|
| <img src=".github/svgs/providers/aistudio.svg" width="18" alt=""> **Google AI Studio** | 🟢 Active | Gemini 3.8 Flash, 3.7 Flash, 3.6 Flash, Thinking Levels, Search context |
| <img src=".github/svgs/providers/zai.svg" width="18" alt=""> **Xiaomi MiMo** | 🟢 Active | MiMo-V2.6-Pro, MiMo-V2.6-Flash, auto-fallback for retired 2.5 |
| <img src=".github/svgs/providers/deepseek.svg" width="18" alt=""> **DeepSeek** | 🟢 Active | DeepThink, Search, Expert Mode, uploads, chat reuse |
| <img src=".github/svgs/providers/zai.svg" width="18" alt=""> **GLM Chat (Z.ai)** | 🟢 Active | GLM-5.3, GLM-5.3-Flash, GLM-5.2, Thinking levels (Low/High/Max), Search |
| <img src=".github/svgs/providers/qwen.svg" width="18" alt=""> **Alibaba Qwen** | 🟢 Active | Qwen3.8-Max, Qwen3.8-Omni-Flash, Qwen3.7 series, Web Search, Thinking |
| <img src=".github/svgs/providers/moonshot.svg" width="18" alt=""> **Kimi / Moonshot** | 🟡 Usable | Thinking, Search, file uploads, Google login |
| <img src=".github/svgs/providers/perplexity.svg" width="18" alt=""> **Perplexity** | 🟡 Usable | Model selection, Thinking/Search controls, uploads |
| <img src=".github/svgs/providers/huggingface.svg" width="18" alt=""> **HuggingChat** | 🟡 Usable | Model/provider selection, Thinking Effort, Exa search |

## Quick Troubleshooting 🧯

- **Browser takes forever on first run**: Patchright may be downloading/verifying Chromium. Let it finish.
- **Client cannot connect**: Check that the app status is **Running** and your endpoint matches the port (`http://127.0.0.1:7777/v1`).
- **401 Unauthorized**: You enabled API Keys in Settings. Either enter a key in your client or disable the setting.
- **Login loop / stuck sign-in**: Try toggling Persistent Sessions off or clear your browser profile in Settings.
- **Bug Reports**: Open an issue at **[GitHub Issues](https://github.com/Lemomation/irn/issues)**.

## Contributing 🤝

Contributions are encouraged! See **[CONTRIBUTING.md](CONTRIBUTING.md)** for details.
- Basic understanding of Playwright / browser automation is helpful.
- Vibe coding is welcome as long as your changes work cleanly and without bugs!

## Maintainers & Contributors ❤️

| <a href="https://github.com/Lemomation"><img src="https://avatars.githubusercontent.com/u/204118677?s=500&v=4" width="80" height="80" alt="Lemomation" /></a> | <a href="https://github.com/LyubomirT"><img src="https://avatars.githubusercontent.com/u/127299159?s=500&v=4" width="80" height="80" alt="LyubomirT" /></a> | <a href="https://github.com/Omega-Slender"><img src="https://avatars.githubusercontent.com/u/134849645?s=500&v=4" width="80" height="80" alt="Omega-Slender" /></a> | <a href="https://github.com/Deaquay"><img src="https://avatars.githubusercontent.com/u/103206423?s=500&v=4" width="80" height="80" alt="Deaquay" /></a> | <a href="https://github.com/Targren"><img src="https://avatars.githubusercontent.com/u/11566412?s=500&v=4" width="80" height="80" alt="Targren" /></a> | <a href="https://github.com/fushigipururin"><img src="https://avatars.githubusercontent.com/u/96440827?s=500&v=4" width="80" height="80" alt="fushigipururin" /></a> | <a href="https://github.com/Vova12344weq"><img src="https://avatars.githubusercontent.com/u/131772052?s=500&v=4" width="80" height="80" alt="Vova12344weq" /></a> |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| <a href="https://github.com/Lemomation">Lemomation</a> | <a href="https://github.com/LyubomirT">LyubomirT</a> | <a href="https://github.com/Omega-Slender">Omega-Slender</a> | <a href="https://github.com/Deaquay">Deaquay</a> | <a href="https://github.com/Targren">Targren</a> | <a href="https://github.com/fushigipururin">fushigipururin</a> | <a href="https://github.com/Vova12344weq">Vova12344weq</a> |
| Current Maintainer | OG Maintainer | Original Creator | Contributor to OG | Feedback & Proposals, Code | Code and Concept Contributor | Early Testing, Bug Reports, Suggestions |

- Special thanks to past maintainers and contributors across earlier forks, including **[Phobeuscz](https://github.com/Phobeuscz)** and **[TalonHawke](https://github.com/TalonHawke)**.
- Full contributor history: https://github.com/LyubomirT/intense-rp-next/graphs/contributors

Special thanks to the developers and tools powering the project: **FastAPI**, **PySide6 (Qt)**, and **Playwright / Patchright**.

## License

IntenseRP Next v2 is open-source software licensed under the [MIT License](LICENSE).
