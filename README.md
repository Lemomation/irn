This is just a continuation of original IntenseRP Next, Credits to everyone who worked on it!
<p align="center">
  <img src=".github/images/logo-strip.png" alt="IntenseRP Next" />
</p>

<h1 align="center">IntenseRP Next v2</h1>

<p align="center">
  It's a local OpenAI-compatible API + desktop app that drives various web LLM chat UIs (via Playwright),
  so you can use those providers from SillyTavern and other clients without wiring up the official provider APIs. <i>Slightly cursed yet surprisingly effective.</i>
</p>

<p align="center">
  <a href="https://github.com/Phobeuscz/irn/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/Phobeuscz/irn?style=flat-square" /></a>
  <a href="https://github.com/Phobeuscz/irn/issues"><img alt="Issues" src="https://img.shields.io/github/issues/Phobeuscz/irn?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/Phobeuscz/irn?style=flat-square" /></a>
  <a href="https://intense-rp-next.readthedocs.io/en/latest/"><img alt="Docs" src="https://img.shields.io/website?url=https%3A%2F%2Fintense-rp-next.readthedocs.io%2Fen%2Flatest%2F&label=docs&style=flat-square" /></a>
</p>

<p align="center">
  <a href="#what-is-this">What is this?</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#connect-sillytavern-or-any-openai-compatible-client">Client setup</a> ·
  <a href="#provider-support">Providers</a> ·
  <a href="#documentation">Documentation</a> ·
</p>

<h1 align="center">🎬 Preview Video</h1>



https://github.com/user-attachments/assets/ebf1bfcd-3b23-4614-b584-174791bcb004


<p align="center">
  <a href="https://github.com/Phobeuscz/irn"><img src=".github/svgs/star2.svg" alt="Leave a Star" height="42"></a>
</p>

## Welcome 👋

If you're here because you need a local bridge between SillyTavern and a browser-based provider chat, this repository may still be useful as reference.
IntenseRP Next v2 drives supported provider web apps (like DeepSeek, Z.AI, Kimi, QwenLM, Perplexity, HuggingChat, and Google AI Studio) in a real browser, and re-exposes the browser session as an OpenAI-compatible endpoint.

It can mirror provider web UI controls such as reasoning toggles, search, file uploads, and model pickers where those controls are available to your account. The tradeoff is that it depends on third-party web pages, so it is inherently fragile and should be treated as a local personal-use tool.
  
## Start here! 🎁

1. Install From source

Requirements: Python 3.12+ (3.13 recommended)

```bash
git clone https://github.com/Phobeuscz/irn.git   # Or download and extract in zip
cd intense-rp-next

python -m venv venv

source venv/bin/activate  # Linux/Mac
# or: venv\\Scripts\\activate  # Windows

pip install -r requirements.txt
python main.py
```

2. Click **Start** and log in when the browser opens
3. Point your SillyTavern client at `http://127.0.0.1:7777/v1` (default) and pick the matching provider model IDs (`deepseek-*`, `glm-*`, `moonshot-*`, `qwen-*`, etc.)

And it's done! It should Just Work™️.

## What is this?

IntenseRP Next v2 (sometimes shortened to "IRP Next v2") is a local bridge between:

- an OpenAI-style client (like SillyTavern), and
- a supported provider web app (DeepSeek, GLM Chat, Kimi / Moonshot, QwenLM, Perplexity, HuggingChat, or Google AI Studio)

Under the hood it:

1. Starts a local FastAPI server (OpenAI-compatible routes under `/v1`)
2. Launches a real Chromium session (Patchright/Playwright)
3. Logs in (manual or auto-login)
4. Captures the provider's streaming responses in the browser session
5. Re-emits them as OpenAI-style SSE deltas for your client

In normal human terms, it is a local compatibility layer between a desktop browser session and OpenAI-style clients.

## Should you use it? 🎯

If you read this far, you probably have a use case in mind! But here's the objective truth:

It would work well for you if you:

- want a local bridge for provider web models
- prefer a clicky desktop app over a pile of scripts
- are OK with the occasional wait or hiccup (web apps change)

Not the best fit if you:

- need high throughput / parallel requests without using the experimental parallel modes
- want to run headless on a server
- need an actively maintained project with support guarantees
- want something that never breaks (that's perhaps the biggest caveat)

> [!NOTE]
> 1. Provider web apps change. When they do, a driver can break until it's updated.
> 2. By default, IntenseRP processes **one request at a time** (requests are queued). This is on purpose for a single live browser session.
> 3. This project is not affiliated with DeepSeek, ZhipuAI, SillyTavern, or any provider.

## Why v2?

v2 is a full rewrite based on lessons learned from the original **IntenseRP API** (by Omega-Slender) and my own **IntenseRP Next v1**.
The focus is less on a pile of features and more on making it sane to maintain and hard to break.

It's a more modular codebase with a Playwright-first browser-control approach, a better UI (PySide6), and a cleaner settings model, plus built-in update and migration flows.

If you want to compare, have a look:

| Area | IntenseRP API / Next v1 | IntenseRP Next v2 |
|---|---|---|
| Backend | Python (Flask) | Python (FastAPI) |
| UI | customtkinter | PySide6 (Qt) |
| Automation | Selenium-based | Playwright (Patchright) |
| Response capture | HTML parsing | Structured browser response handling |



## Connect SillyTavern (or any OpenAI-compatible client)

Once the app says **Running (Port 7777)**:

| Setting | Value |
|---|---|
| Endpoint | `http://127.0.0.1:7777/v1` |
| API | OpenAI-compatible chat or text completions |
| API key | Leave blank (unless you enabled API keys) |
| Model | Provider model IDs like `deepseek-*`, `glm-*`, `moonshot-*`, `qwen-*`, `perplexity-*`, `huggingchat-*`, or `aistudio-*` |

Available model IDs (depends on provider):

- DeepSeek:
  - `deepseek-auto` (uses your IntenseRP settings)
  - `deepseek-chat` (forces DeepThink off)
  - `deepseek-reasoner` (forces DeepThink on, Send DeepThink follows your setting)
  - `deepseek-expert-auto` (uses your IntenseRP settings, but with Expert Mode enabled in the web UI)
  - `deepseek-expert-chat` (forces DeepThink off, with Expert Mode enabled in the web UI)
  - `deepseek-expert-reasoner` (forces DeepThink on, Send DeepThink follows your setting, with Expert Mode enabled in the web UI)
- GLM Chat:
  - `glm-auto` (uses your IntenseRP settings)
  - `glm-chat` (forces Deep Think off)
  - `glm-reasoner` (forces Deep Think on, Send Deep Think follows your setting)
- Kimi / Moonshot:
  - `moonshot-auto` (uses your IntenseRP settings)
  - `moonshot-chat` (forces Thinking off, Send Thinking off)
  - `moonshot-reasoner` (forces Thinking on, Send Thinking follows your setting)
- QwenLM:
  - `qwen-auto` (uses your IntenseRP settings)
  - `qwen-chat` (forces Thinking off, Send Thinking off)
  - `qwen-reasoner` (forces Thinking on, Send Thinking follows your setting)
- Perplexity:
  - `perplexity-auto` (uses your IntenseRP settings)
  - `perplexity-chat` (forces Reasoning off)
  - `perplexity-reasoner` (forces Reasoning on)
- HuggingChat:
  - `huggingchat-auto` (uses your IntenseRP settings)
  - `huggingchat-chat` (uses HuggingChat's default/off behavior)
  - `huggingchat-reasoner` (enables HuggingChat Thinking where available)
- Google AI Studio:
  - `aistudio-auto` (uses your IntenseRP settings)
  - `aistudio-chat` (forces Thinking off, Send Thinking off)
  - `aistudio-reasoner` (forces Thinking on, Send Thinking follows your setting)

Note: these IDs are behavior presets (modes). For providers that support actual model selection in their web UI (like GLM, QwenLM, Perplexity, HuggingChat, and Google AI Studio), the mode will still apply its behavior changes, but the real web UI model will be whatever you have selected in settings within the app.

If you change the port in Settings, update the endpoint to match (example: `http://127.0.0.1:YOUR_PORT/v1`).

> [!TIP]
> If you don't want to use different provider names for model selection, enable **Universal Model Names** in Settings. This replaces all provider names with `intenserp`, so the above would become `intenserp-auto`, `intenserp-chat`, and `intenserp-reasoner`. With the exception of DeepSeek's Expert Mode presets, which would become `intenserp-expert-auto`, `intenserp-expert-chat`, and `intenserp-expert-reasoner`.


## Quick troubleshooting 🧯

- **Browser takes forever on first run**: it may be downloading/verifying Chromium. Let it cook, then try again.
- **Client cannot connect**: confirm the app says **Running**, and the endpoint matches your port (`http://127.0.0.1:7777/v1` by default).
- **401 Unauthorized**: you probably enabled API keys in Settings. Either disable them or add a key in your client.
- **Login loops / stuck sign-in**: try disabling Persistent Sessions, or clear the profile in Settings (it wipes saved cookies).
- **Slow responses**: requests are queued by default, and reasoning/thinking modes can add extra time. Experimental parallel modes can help, but they are much heavier.

Tip: enable the console and/or logfiles before reporting issues. Logs help a lot when diagnosing!

## What you get ✨

There are a few highlights I think are worth calling out. Most have been in v1 as well, but v2 has them all better and in a cleaner way.

- 🖥️ A desktop UI that starts/stops everything for you (and doesn't require terminal work)
- 🔌 An OpenAI-compatible API under `/v1` for SillyTavern and other OpenAI-compatibles
- 🧩 A formatting pipeline: templates, divider, injection, name detection
- 🧠 Provider behavior toggles: Thinking/Reasoning, Search, uploads, model pickers, and provider-specific knobs
- 👥 Saved Accounts, account rotation, pinning, disabling, and persistent browser sessions
- 🔐 Optional LAN mode and API keys
- 🪵 Built-in extensive logging: console window, log files, console dump
- 📱 Experimental Remote Control for managing the app from another device on your network
- ♻️ Built-in v1 migrator + built-in update flow (when running packaged builds)

## Provider support

Current supported providers:

| Provider | Status | Notes |
|---|---|---|
| <img src=".github/svgs/providers/deepseek.svg" width="18" alt=""> **DeepSeek** | Stable | DeepThink, Search, Expert Mode, uploads, and chat reuse |
| <img src=".github/svgs/providers/zai.svg" width="18" alt=""> **GLM Chat (Z.ai)** | Stable-ish | Deep Think, Search/Advanced Search/Tools, model selection, and CAPTCHA login |
| <img src=".github/svgs/providers/moonshot.svg" width="18" alt=""> **Kimi / Moonshot** | Stable-ish | Thinking, Search, uploads, Google login, and Kimi-specific caveats |
| <img src=".github/svgs/providers/qwen.svg" width="18" alt=""> **QwenLM** | Stable | Thinking, Web Search, model selection, token counting, and uploads |
| <img src=".github/svgs/providers/perplexity.svg" width="18" alt=""> **Perplexity** | Verification | Model selection, Thinking/Search controls, uploads, and email-code login |
| <img src=".github/svgs/providers/huggingface.svg" width="18" alt=""> **HuggingChat** | Early integration | Model/provider selection, Thinking Effort, Exa search, uploads, and tight monthly credits |
| <img src=".github/svgs/providers/aistudio.svg" width="18" alt=""> **Google AI Studio** | Usable | Gemini model selection, Thinking Level, Search/URL Context, and so on |

More detail lives in `docs/` (best viewed as the docs site - see below).

## Documentation

There is a full docs site with screenshots and details if you want to dig a bit deeper:

Check out [the docs site here](https://intense-rp-next.readthedocs.io/en/latest/).

Local preview (Zensical):

```bash
python -m pip install -r docs/requirements.txt
zensical serve
```

## Security and privacy notes

- IntenseRP is designed for local or LAN use. Do not expose it to the public internet unless you know what you're doing.
- If you enable **Available on LAN**, consider enabling **API Keys** too.
- Your config directory contains sensitive data (credentials, API keys, session cookies). Treat it like a password vault.

## Contributors of the original version ❤️

| <a href="https://github.com/LyubomirT"><img src="https://avatars.githubusercontent.com/u/127299159?s=500&v=4" width="80" height="80" alt="LyubomirT" /></a> | <a href="https://github.com/Omega-Slender"><img src="https://avatars.githubusercontent.com/u/134849645?s=500&v=4" width="80" height="80" alt="Omega-Slender" /></a> | <a href="https://github.com/Deaquay"><img src="https://avatars.githubusercontent.com/u/103206423?s=500&v=4" width="80" height="80" alt="Deaquay" /></a> | <a href="https://github.com/Targren"><img src="https://avatars.githubusercontent.com/u/11566412?s=500&v=4" width="80" height="80" alt="Targren" /></a> | <a href="https://github.com/fushigipururin"><img src="https://avatars.githubusercontent.com/u/96440827?s=500&v=4" width="80" height="80" alt="fushigipururin" /></a> | <a href="https://github.com/Vova12344weq"><img src="https://avatars.githubusercontent.com/u/131772052?s=500&v=4" width="80" height="80" alt="Vova12344weq" /></a> |
|:---:|:---:|:---:|:---:|:---:|:---:|
| <a href="https://github.com/LyubomirT">LyubomirT</a> | <a href="https://github.com/Omega-Slender">Omega-Slender</a> | <a href="https://github.com/Deaquay">Deaquay</a> | <a href="https://github.com/Targren">Targren</a> | <a href="https://github.com/fushigipururin">fushigipururin</a> | <a href="https://github.com/Vova12344weq">Vova12344weq</a> |
| Project Maintainer | Original Creator | Contributor to OG | Feedback & Proposals, Code | Code and Concept Contributor | Early Testing, Bug Reports, Suggestions |

Full list: https://github.com/LyubomirT/intense-rp-next/graphs/contributors

## License

IntenseRP Next v2 is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

> [!NOTE]
> Original IntenseRP API by Omega-Slender is also MIT-licensed, but previously was a [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) project. This v2 rewrite is a new codebase and is not a derivative work, so the license has been switched to MIT for simplicity. I'm not affiliated with [Omega-Slender](https://github.com/Omega-Slender), even if I'm the official successor to their project (starting from v1).

## Credits

- Me ( Fixing shit, GLM5.3, Fixed Moonshot API, added Kimi K3 support )
- FastAPI, Pydantic, Uvicorn
- PySide6 (Qt)
- Playwright + Patchright
- Feather Icons / Lucide Icons
- SillyTavern (client ecosystem)
- IntenseRP API (Omega-Slender) - original inspiration
- (LyubomirT) - for doing all the work :D
- RossAscends (for STMP)
- Developers of Zensical (docs generator)
