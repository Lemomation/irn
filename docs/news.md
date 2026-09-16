---
icon: material/newspaper-variant-outline
---

<!-- NOTE FOR SELF: Increment .github/state/latestnews.txt after you're done writing here or else nobody will get the bell indicator -->

# :material-newspaper-variant-outline: News

This page is the changelog for the latest news and updates about IntenseRP Next.

## September 17, 2026 - Update 2.9.3

A provider-fix follow-up with **Kimi K2.8** as its own picker model, **DeepSeek's new radio picker**, and a **GLM Chat** modal-overlay fix.

**Kimi (Moonshot):** Kimi introduced a new **K2.8** model that is a separate entry in the web picker (a tier-gated preview on some accounts) - not a rename of the classic Instant model. IntenseRP now treats it as its own model: `Instant` always selects the **Instant** entry and K2.8 always selects the **K2.8** entry (also matched via `K2.8 Preview` / `Kimi K2.8` labels), and K2.8 appears in the Model dropdown. With Universal Model Names enabled it exposes `k2-8-auto` / `k2-8-chat` / `k2-8-reasoner` API model IDs just like the other Kimi families.

**DeepSeek:** DeepSeek is rolling out a new radio-style model type picker, and its `data-model-type` value for the fast/default ("Instant") option varies between rollouts. IntenseRP now matches the Instant option across those values (`default`, `instant`, `instant-v3`) on both the new radio picker and the legacy inline picker.

**GLM Chat:** z.ai's app can now render a dialog modal overlay (`data-dialog-overlay`) on top of the sidebar after startup; that overlay was intercepting clicks on the **New Chat** button. IntenseRP dismisses dialog-style overlays (Escape-key and close-button cleanup) before clicking New Chat, and **GLM-5.3-Flash** joins the GLM model list.

[GLM Behavior](providers/glm-behavior.md){ .md-button }
[GLM Quirks](advanced/glm-quirks.md){ .md-button }
[Moonshot Behavior](providers/moonshot-behavior.md){ .md-button }

---

## August 22, 2026 - Update 2.9.2

This release is a big catch-up for two providers whose web apps moved under our feet: **GLM Chat (z.ai)** and **Moonshot Kimi (kimi.ai)**.

**GLM Chat works again** after z.ai moved its completion endpoint - IntenseRP now accepts both the old and new paths - and **GLM-5.3** is supported and selected by default, including its Deep Think effort menu (`glm-5-3-auto` / `glm-5-3-reasoner` / `glm-5-3-chat` API model IDs). GLM failures are honest now: structured refusals (`SENSITIVE`, `RISK_CONTROL_BLOCKED`, CAPTCHA codes, generic errors) are logged with their codes and forwarded to your client as proper error events, with a desktop notification when a FeiLin CAPTCHA needs solving in the browser window. Metadata-only "silent refusals" are reported as probable shadow censorship instead of masquerading as success, and CDP Teeing recovers fast-failing response bodies automatically instead of losing them.

**Kimi moved international accounts to kimi.ai**, so IntenseRP follows along: new sessions start on kimi.ai, a **Kimi Site Region** switch covers accounts on either domain, and the model picker gained real support for the K3 generation with label-tolerant matching. With Universal Model Names enabled, Kimi now exposes split API model IDs just like GLM: `kimi-k3-auto`, `kimi-k3-chat`, `kimi-k3-reasoner`, plus `instant-*` and `kimi-k3-swarm-*` variants, alongside the classic `moonshot-auto/chat/reasoner`. The `-reasoner` suffix forwards reasoning where the model produces it; `-chat` strips it.

Kimi's aggressive rate limiting ("Too many people are chatting with Kimi right now") is detected from stream content and HTTP 429/503 responses and surfaced as an at-capacity error to your client instead of silence. The removed Search toolkit menu is handled through Kimi's new Offline Mode switch where present, and skipped cleanly on models that don't render it.

Smaller goodies across both providers: FeiLin's console-wiping anti-debug trick is neutralized so DevTools logs survive challenges, CAPTCHA resource loading is observed and logged, UI reads use bounded timeouts so provider stalls can't freeze requests, and the API worker closes driver generators deterministically (no more `GeneratorExit` noise on retries).

[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[GLM Behavior](providers/glm-behavior.md){ .md-button }
[GLM Quirks](advanced/glm-quirks.md){ .md-button }
[Moonshot Behavior](providers/moonshot-behavior.md){ .md-button }

---

## July 4, 2026 - Update 2.9.0

IntenseRP Next v2.9.0 is here, and this one is a little special: **IntenseRP Next is turning 1 year old this July**!!! 🎆🎆🎆✨✨✨

Thank you so much to everyone who has used it, reported weird provider breakages, suggested ideas, joined the Discord, shared feedback, or just quietly kept the thing running in your own setup. This project would be a much lonelier pile of browser automation without you.

The headline feature is **Xiaomi MiMo support**. MiMo joins the provider list with model selection, thinking output filtering, token usage, prompt-file uploads, chat reuse, Xiaomi account login, and provider-specific proxy settings for regions where MiMo refuses to load. It's still region-dependent and a bit beta-ish, but it is now usable through IntenseRP's normal OpenAI-compatible API.

This update also adds **Dry Run Mode**, a new API debugging workflow that starts the server without launching a provider browser and shows exactly what your client sent plus what IntenseRP's formatting pipeline produced. On the runtime side, **CDP Teeing** is now available across more drivers, browser proxy/viewport controls are better, backups/imports are safer, and the updater has received more polish.

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.9.0-update){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[Xiaomi MiMo Behavior](providers/mimo-behavior.md){ .md-button }
[Dry Run Mode](features/network-api.md#dry-run-mode){ .md-button }

---

## June 12, 2026 - Update 2.8.8

IntenseRP Next v2.8.8 is a smaller release with two very practical wins: **Google AI Studio is back**, and **Providers in Parallel is no longer tucked Experimental**.

AI Studio is selectable again thanks to the new default **Humanize Mouse Movements** reliability mode. It is slower on purpose, because AI Studio really did not enjoy the instant-move / instant-click treatment, but it makes browser sends much more reliable.

Providers in Parallel now lives under **Browser & Runtime**, and it can do more than keep extra provider lanes warm. Depending on the mode you choose, different provider lanes can answer queued API requests at the same time, and startup can launch active lanes concurrently. If that startup burst is too heavy, batching is there too (still speeds things up, but with more breathing room).

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.8.8-patch){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[Google AI Studio Behavior](providers/aistudio-behavior.md){ .md-button }
[Providers in Parallel](runtime/providers-in-parallel.md){ .md-button }

---

## June 1, 2026 - Update 2.8.6

IntenseRP Next v2.8.6 is mostly a follow-up to the 2.8.5 provider work, but the headline is simple: **HuggingChat support is finally here**.

If DeepSeek has been giving you trouble lately, HuggingChat gives you another account-backed provider lane and broader access to some models exposed through `huggingface.co/chat`. It includes real model selection, inference provider hints, Thinking Effort, Exa search, prompt uploads, chat reuse, and account rotation.

That said, it's still not a magic solution. HuggingChat is still an early integration that depends on the web UI, and its monthly credits are a little tiny. The [HuggingChat Behavior docs](providers/huggingchat-behavior.md) cover the main things, credit limits, and account rotation setup.

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.8.6-patch){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[HuggingChat Feedback](https://forms.gle/J7MVdcnorEPE249v8){ .md-button }

---

## April 29, 2026 - Update 2.8.0

IntenseRP Next v2.8.0 is here, and the headline is **Perplexity support**. It works with both free and paid accounts, can stream answer text through the normal OpenAI-compatible API, and includes Perplexity-specific settings for model selection, Thinking, Search, and text-file prompt uploads where the account allows them.

This integration exists thanks to [Yurushia](https://github.com/twgok123), who helped with Perplexity testing during development. Huge thank you to them for making this one possible!!!

This release also includes some recent power-user and maintenance work: Full Parallelization for provider lanes, selective provider restarts when switching loadouts, AI Studio CAARS options, backup/import polish, and a more maintainable Remote Control frontend.

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.8.0-update){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[Suggest New Features](./vote-for-new-stuff.md){ .md-button }
[Perplexity Behavior](providers/perplexity-behavior.md){ .md-button }

---

## March 28, 2026 - Update 2.6.3

IntenseRP Next v2.6.3 is mostly a quality-of-life update, and the biggest visible change is the **Settings redesign**. The whole thing is meant to feel cleaner, easier to read, and a lot more intuitive to move around in.

This update also brings **Providers in Parallel**, which can keep multiple provider browsers alive at once so different providers can work side by side, plus **Loadouts**, a validated `loadouts.json` system for people who would rather manage formatting and provider behavior through a file and quickly jump between presets instead of clicking through the UI every time.

GLM also gets **GLM-5-Turbo** as a selectable real model option under **Provider Behavior -> GLM Chat -> Model**, alongside the existing GLM model choices.

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.6.3-patch){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[Suggest New Features](./vote-for-new-stuff.md){ .md-button }

---

## March 20, 2026 - Project Direction Survey

IntenseRP Next opened a community survey to collect feedback about future direction and maintenance preferences.



## March 19, 2026 - Update 2.6.1

IntenseRP Next v2.6.1 is here with two small but useful upgrades.

Moonshot now supports an experimental **Auto Login** flow for its Google popup, so IntenseRP can try to fill the sign-in steps for you before falling back to manual completion if Google decides to be Google.

Multi-Slot Cache is also now available on every current driver except **Google AI Studio**, which means **DeepSeek**, **GLM Chat**, **Moonshot**, and **QwenLM** can now look through older cached chats instead of only remembering the latest one. This is especially useful if you swipe a lot. More about that in the [Multi-Slot Cache docs page](features/multi-slot-cache.md).

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.6.1-patch){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[Suggest New Features](https://forms.gle/cRGEoTNKxUrjKRJ2A){ .md-button }

---

## 🎉 March 14, 2026 - Update 2.6.0

IntenseRP Next v2.6.0 is out, and the big headline is **Google AI Studio support**. It already includes streaming, Google login handling, model switching, and sampling controls, but it is still a bit beta-ish for now, so please poke it gently and let me know if something feels off.

This update also brings some nice extra polish: Moonshot now gets RP-friendly auto-adjustments just like Qwen, the Settings window loads much faster, mini help buttons were added around settings, the console got its own search, and there is a new v1-style multiline XML-like formatting preset too.

[Full Release Notes](https://github.com/LyubomirT/intense-rp-next/releases/tag/v2.6.0-update){ .md-button .md-button--primary }
[Join our Discord](https://discord.gg/4Gvjk2RdsK){ .md-button }
[Suggest New Features](https://forms.gle/JCe8FQ27mPgdEGes7){ .md-button }

---

## March 11, 2026 - Official Discord Server

The official Discord server is finally here! :tada: 

Based on the latest survey results, a majority vote favored creating a community space, so I've gone ahead and set it up. It's a little quiet for now since it's just starting out, but I'm hoping to expand it and see the community grow soon. If you want a place to chat, get support, or follow the latest updates more closely, come hang out!

[Join the Discord Server](https://discord.gg/4Gvjk2RdsK){ .md-button .md-button--primary }

---

## March 10, 2026 - News / Changelog is open

Welcome to the new News and Changelog page! :material-newspaper:

I've set up this place to keep everyone updated on the latest developments, features, and improvements in IntenseRP Next. Whenever there's a new update or important news, you'll find it here first. Make sure to check back regularly to stay in the loop with all the changes coming your way!
