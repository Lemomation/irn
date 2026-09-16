---
icon: providers/moonshot
---

# :providers-moonshot: Moonshot Behavior

This page covers the toggles and options that control how IntenseRP interacts with **Moonshot** (`kimi.ai`).

!!! note "Kimi domains"
    Kimi runs the same app on two regional domains: **kimi.ai** (overseas, the default) and **kimi.com** (China). IntenseRP starts new sessions on `kimi.ai` and accepts both domains for existing sessions, so accounts living on either domain keep working.

!!! note "Model IDs"
    Moonshot exposes three API behavior presets:
    `moonshot-auto`, `moonshot-chat`, and `moonshot-reasoner`.
    These are behavior modes, not separate backend model selection.

---

## :material-earth: Kimi Site Region

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Kimi Site Region**

Chooses which Kimi domain new sessions start on:

| Option | Domain | For |
|--------|--------|-----|
| **International (kimi.ai)** | `kimi.ai` | The default. Use this unless your account lives on the Chinese site. |
| **China (kimi.com)** | `kimi.com` | Accounts registered on the Chinese Kimi site. |

The switch only decides the *starting* domain. Once a session is live, IntenseRP always talks to whichever host the page actually uses, so a saved session from the other region keeps working even if the switch is set differently.

## :material-call-split: Request Capture Mode

Controls how IntenseRP captures Moonshot's streaming response.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Request Capture Mode**

**Replay** is the default. IntenseRP intercepts the Kimi request, replays it internally, streams that replay to the API client, and then gives the captured response back to the page. It's the older, known-good path.

**CDP Teeing** is the newer alternative. IntenseRP leaves Kimi's real browser request alone, tees the real response through Chrome DevTools Protocol, and feeds those bytes through the same Moonshot stream parser. This lets the page JavaScript receive and process its own response normally while IntenseRP observes the stream.

!!! note "Default stays Replay"
    CDP Teeing is off by default for Moonshot. It's available if you want the browser-native request path, but Replay remains the safer default while this newer path gets more real-world mileage.

---

## :material-login: Authentication

Kimi uses a Google sign-in popup rather than a normal in-page email/password form.

IntenseRP can now try a best-effort **Auto Login** flow there by pasting your saved Google email/password into the popup and pressing `Enter` between steps.

That said, Google is still Google. It may decide to ask for extra confirmation, show an account chooser, demand 2FA, or just leave the popup hanging there. When that happens, IntenseRP falls back to manual completion and waits for you to finish the flow.

**Persistent Sessions are still strongly recommended.** If your Google session stays alive, Kimi becomes much less annoying to use.

!!! info "Multiple profiles"
    If you legitimately use more than one Kimi account, you can keep separate account/profile rows.

    Open **Settings -> Provider and Login -> Sign-In and Accounts -> Saved Accounts** and add multiple accounts under Moonshot.

    IntenseRP uses those values for Moonshot's Google popup Auto Login when **Auto Login** is enabled, and each row also gets its own browser profile/session.

    Keep each row tied to an account you are authorized to use, and expect each provider account to keep its own limits and checks.

## :material-head-cog: Thinking

Kimi exposes reasoning through model mode selection in the web UI.

### Model

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Model**

Selects a specific entry in Kimi's model picker:

| Option | Picker entry |
|--------|--------------|
| **Auto (Thinking toggle)** | No fixed selection - the Enable Thinking toggle decides |
| **Instant** | Kimi's fast model (currently labeled `Instant`) |
| **K2.8** | Kimi's K2.8 entry (separate from Instant; tier-gated preview on some accounts) |
| **Kimi K3** | The K3 flagship |
| **Kimi K3 Swarm** | The K3 Swarm variant |

An explicit model always wins over the Enable Thinking toggle: when one is selected, IntenseRP makes sure that exact picker entry is active and leaves the Thinking toggle alone. Matching is label-tolerant, so older rollouts that still show `K2.6 Instant` / `K2.6 Thinking` keep working. `Instant` and `K2.8` are always matched to their own picker entries - K2.8 is never used as a stand-in for Instant.

#### Split API model IDs

With Universal Model Names enabled (Providers in Parallel), Moonshot exposes per-model API IDs like GLM does:

| API model ID | Picker target | Reasoning |
|--------------|---------------|-----------|
| `instant-auto` / `instant-chat` / `instant-reasoner` | Instant | `-reasoner` switches to the Thinking variant |
| `k2-8-auto` / `k2-8-chat` / `k2-8-reasoner` | K2.8 | `-reasoner` switches to the Thinking variant, K2.8 entry |
| `kimi-k3-auto` / `kimi-k3-chat` / `kimi-k3-reasoner` | Kimi K3 | `-reasoner` forwards reasoning, `-chat` strips it |
| `kimi-k3-swarm-auto` / `kimi-k3-swarm-chat` / `kimi-k3-swarm-reasoner` | Kimi K3 Swarm | same as K3 |
| `moonshot-auto` / `moonshot-chat` / `moonshot-reasoner` | legacy behavior modes | unchanged |

The suffix always decides how reasoning content is handled; the name decides which picker entry is active.

!!! note "Rate limits"
    Kimi rate limits aggressively under load (especially K3). When the server answers with its "Too many people are chatting with Kimi" message or an HTTP 429, IntenseRP reports an at-capacity error to your client instead of returning silence - retry shortly, switch models, or subscribe to Kimi for the priority queue.

### Enable Thinking

Switches Kimi to its Thinking model before sending a request.
When disabled, IntenseRP switches to **Instant**.
Only applies when **Model** is set to **Auto (Thinking toggle)**.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Enable Thinking**

### Send Thinking

When enabled, reasoning content is included in API output, wrapped in `<think>` tags.
When disabled, only final answer text is forwarded.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Send Thinking**

!!! warning "Model availability"
    Kimi may downgrade to K2.6 Instant when Thinking mode is unavailable for the active account or current provider conditions. This is a provider-side change and not something IntenseRP can control.

---

## :material-magnify: Search

Toggles Kimi search tooling in the web UI.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Enable Search**

!!! warning "Search + Thinking"
    Kimi can emit multi-stage reasoning when Search and Thinking are both enabled.
    Some clients (including SillyTavern) may not parse this perfectly.

!!! danger "On a personal note"
    Do not enable Search and Thinking at the same time. Just don't. It's a mess and IntenseRP sometimes breaks trying to handle it.

---

## :material-shield-check: Provider guardrails (automatic)

Kimi's Memory feature can inject account-level context into chats.

To keep chats predictable, IntenseRP watches Kimi's startup settings response and auto-disables Memory if it sees it enabled.

This is done from the browser context, so it uses your active Kimi session and cookies rather than a separate HTTP client.

---

## :material-file-upload: File Upload Mode

Instead of typing your message into Kimi's editor, IntenseRP can upload it as a text file attachment.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Send As Text File**

### File Upload Timeout

Controls how long IntenseRP waits (in seconds) for the send button to become enabled after upload.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **File Upload Timeout**

Default is 15 seconds.

### Text File Filler

Kimi won't let you send a file with an empty textbox as it needs *some* text alongside it. By default IntenseRP pastes a single `.` (dot) as filler, but you can change this to whatever you want.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Text File Filler**

This setting only appears when **Send As Text File** is enabled.

---

## :material-shield-off: Blocked-response handling

When **Blocked-Response Handling** is enabled, IntenseRP suppresses detected refusal-like stream events and closes the response cleanly.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Blocked-Response Handling**

!!! warning "What It Doesn't Do"
    This does not override provider policy or unlock blocked content. It only changes how detected refusal-style output is forwarded to the client.

---

## :material-refresh: Reuse Matching Chat

When enabled, IntenseRP tries to click Kimi's regenerate action if:

1. The new prompt is identical to the cached last prompt
2. Effective behavior settings are also identical

Otherwise it starts a fresh chat.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Reuse Matching Chat**

!!! tip "Search Older Matching Chats"
    If you enable **Provider Behavior** -> **Moonshot** -> **Search Older Matching Chats**, IntenseRP keeps up to 7 older cached Kimi chats per account instead of only remembering the latest one.

    When one of those older chats matches the current prompt/settings, IntenseRP can reopen it and use Kimi's regenerate action there.

---

## :material-delete-clock-outline: Delete Chat After Reply

If you want Kimi's chat list to stay tidier, IntenseRP can delete the completed chat after a successful reply finishes.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Moonshot** -> **Delete Chat After Reply**

!!! warning "Slower requests"
    This adds extra cleanup work after each request, so it can slow requests down quite a bit.

!!! note "No chat reuse here"
    This does **not** work together with **Reuse Matching Chat** or **Search Older Matching Chats**.

See also: [:material-delete-clock-outline: Chat Auto-Deletion](../features/chat-auto-deletion.md)

---

## :material-code-tags: Per-Message Macros

You can add `[[...]]` macros to the latest user message to override behavior for that request only.
All macros are stripped before sending.

| Macro | Effect |
|------|--------|
| `[[think]]`, `[[r1]]` | Force Thinking on |
| `[[nothink]]`, `[[r0]]` | Force Thinking off |
| `[[search]]` | Force Search on |
| `[[nosearch]]`, `[[no_search]]` | Force Search off |
| `[[file]]` | Force Send As Text File on |
| `[[nofile]]` | Force Send As Text File off |

---

## :material-format-list-checks: Quick Reference

| Setting | What It Does | Default |
|---------|--------------|---------|
| **Kimi Site Region** | Picks the starting domain (kimi.ai or kimi.com) | International (kimi.ai) |
| **Model** | Picks a specific model (Instant, K2.8, Kimi K3, Kimi K3 Swarm) | Auto (Thinking toggle) |
| **Request Capture Mode** | Captures responses with Replay or CDP Teeing | Replay |
| **Enable Thinking** | Switches Kimi mode between Instant and Thinking | Off |
| **Send Thinking** | Includes reasoning in response | Off |
| **Enable Search** | Toggles Kimi search | Off |
| **Send As Text File** | Uploads prompt as .txt | Off |
| **File Upload Timeout** | Seconds to wait for upload | 15 |
| **Text File Filler** | Text pasted alongside the uploaded file | `.` |
| **Blocked-Response Handling** | Handles detected refusal-like outputs | Off |
| **Reuse Matching Chat** | Regenerates on duplicate prompts | Off |
| **Delete Chat After Reply** | Deletes the completed Kimi chat after a successful reply | Off |

---

## :material-arrow-left: Back to Providers

[:material-arrow-left: Providers Overview](../providers.md)
