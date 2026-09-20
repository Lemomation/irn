---
icon: providers/aistudio
---

# :providers-aistudio: Google AI Studio Behavior

This page covers the toggles and options that control how IntenseRP interacts with **Google AI Studio** (`aistudio.google.com`).

---

## :material-call-split: Request Capture Mode

Controls how IntenseRP captures Google AI Studio's streaming response.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Request Capture Mode**

**Replay** is the default. IntenseRP intercepts the AI Studio request, replays it internally, streams that replay to the API client, and then gives the captured response back to the page. It's the older, known-good path.

**CDP Teeing** is the newer alternative. IntenseRP leaves AI Studio's real browser request alone, tees the real response through Chrome DevTools Protocol, and feeds those bytes through the same AI Studio stream parser. This lets AI Studio's page JavaScript receive and process its own response normally while IntenseRP observes the stream.

!!! note "Default stays Replay"
    CDP Teeing is off by default for Google AI Studio. It's available if you want the browser-native request path, but Replay remains the safer default while this newer path gets more real-world mileage.

---

## :material-cursor-default-click: Recommended Reliability Mode

Google AI Studio is available again. The important part is that **Humanize Mouse Movements** is now enabled by default for AI Studio.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Humanize Mouse Movements**

!!! warning "Leave this on for Google AI Studio"
    This is the practical fix we found for reliable Google AI Studio sends in the browser driver. It slows AI Studio down because IntenseRP uses visible pointer movement, varied click points, and small pauses between UI actions, but it avoids the too-fast/teleporty interaction pattern that was breaking sends.

If you turn it off and AI Studio starts failing again, turn **Humanize Mouse Movements** back on first. It's slower, yes, but "slower and working" is a pretty good trade when the alternative is it not working at all.

---

## :material-tune: Modes (model IDs)

In IntenseRP Next v2, the `model` you select in SillyTavern is still mostly a **behavior preset**, not a true AI Studio model picker.

For Google AI Studio, these model IDs map to the following behavior:

| Model ID | Behavior |
|---|---|
| `aistudio-auto` | Uses your IntenseRP settings |
| `aistudio-chat` | Suppresses `<think>` output and lowers Thinking Level on supported AI Studio models |
| `aistudio-reasoner` | Uses your configured Thinking Level and Send Thinking setting |

!!! note "Why chat mode works differently here"
    Google AI Studio's supported thinking models do not expose a true "thinking off" switch the way some other providers do.

    So IntenseRP approximates a chat-like mode by:

    - lowering Thinking Level to the minimum available (when the model supports it), and
    - suppressing `<think>` output to the client

---

## :material-chip: Real AI Studio model selection (web UI)

Google AI Studio has a real model picker in the web UI:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Model**

Use this setting to pick from the AI Studio model entries exposed in IntenseRP Settings. The dropdown is the source of truth for exact labels, while the docs only call out the important behavior differences.

This is separate from the API `aistudio-*` behavior presets. Gemini 2.5 entries are the special case since they stay visible for compatibility, but runtime requests require the paid-access override below.

!!! warning "Gemini 2.5 models need paid account access"
    Gemini 2.5 Pro, Flash, and Flash Lite have become paid in Google AI Studio. IntenseRP keeps their model entries and old thinking-budget mappings so existing settings don't disappear, but runtime requests that resolve to a Gemini 2.5 model stop unless **Assume Paid Model Access** is enabled for the active account.

    `Gemini 2.5 models have become paid in Google AI Studio, and IRP can't serve them.`

    This is an account-access override, not a way to gain access. If a paid AI Studio API key is available, the actual AI Studio API is usually the cleaner path.

---

## :material-login: Authentication

Google AI Studio uses a Google login flow. IntenseRP supports both:

- **Manual login**
- **Auto Login** (best-effort Google form autofill)

!!! warning "Persistent Sessions strongly recommended"
    Google can still ask for manual confirmation, CAPTCHAs, or extra account checks even when Auto Login is enabled.

    In practice, **Persistent Sessions** are the main quality-of-life feature here. If your Google session stays alive, AI Studio becomes much smoother to use.

!!! note "First-use legal acknowledgement"
    On accounts that have never used AI Studio before, Google may show a legal / terms acknowledgement modal after login.

    IntenseRP now detects that modal, notifies you, and waits for **you** to accept it manually before continuing with setup.

### Auto Login Redirect Timeout

If Auto Login fills your credentials but Google does not return to AI Studio quickly enough, IntenseRP falls back to manual completion.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Auto Login Redirect Timeout (s)**

Default is 15 seconds.

### Humanize Mouse Movements

This is enabled by default and recommended for Google AI Studio.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Humanize Mouse Movements**

When enabled, IntenseRP uses slower Playwright-native pointer movement for AI Studio clicks, adds slight variation to click points, and inserts tiny pauses around UI actions.

This makes AI Studio noticeably slower, especially around clicks, file uploads, and large prompt entry. In return, it avoids the inhuman instant-move/instant-click pattern that was making Google AI Studio unreliable.

!!! note "Keep the boring default"
    You can turn this off for testing, but for normal Google AI Studio use, leave it enabled.

### Assume Paid Model Access

If your Google AI Studio account can use the paid Gemini 2.5 models in the web UI, you can allow IntenseRP to use those model entries again:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Assume Paid Model Access**

When enabled, **Paid Model Access Emails** appears below it. Leave that list empty to assume every AI Studio account has access. Add one or more emails to restrict Gemini 2.5 requests to only those accounts.

!!! warning "Be picky here"
    Only enable this when you're sure the account really has Gemini 2.5 access in AI Studio. With **Providers in Parallel**, IntenseRP uses the email list to route Gemini 2.5 requests to matching AI Studio lanes when it can. Accounts not on the list are treated as not having access.

---

## :material-head-cog: Thinking

Google AI Studio exposes **Thinking Level** instead of a simple on/off reasoning toggle.

### Enable Thinking

When enabled, IntenseRP uses your configured **Thinking Level** on supported AI Studio models.

When disabled, IntenseRP falls back to the lowest available level instead.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Enable Thinking**

### Thinking Level

Controls the level IntenseRP selects on supported AI Studio models:

- `Minimal`
- `Low`
- `Medium`
- `High`

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Thinking Level**

!!! note "Gemini 2.5 Flash, Flash Lite, and Pro"
    Gemini 2.5 Flash, Flash Lite, and Pro use a manual thinking budget instead of the newer Thinking Level dropdown.

    For **Gemini 2.5 Flash** and **Gemini 2.5 Flash Lite**, `Minimal` turns Thinking Mode off. `Low`, `Medium`, and `High` turn Thinking Mode on and set the manual budget to `8192`, `16384`, and `24576`. Flash Lite's manual budget range starts at `512`.

    For **Gemini 2.5 Pro**, Thinking Mode is always on, so `Minimal`, `Low`, `Medium`, and `High` map to `128`, `8192`, `16384`, and `32768`.

    These mappings are kept for compatibility. Gemini 2.5 requests are allowed only when **Assume Paid Model Access** says the active account can use them.

!!! note "Gemma 4 models"
    Gemma 4 models currently expose `Minimal` and `High`. When your configured level lands between those two, IntenseRP picks the nearest available level.

### Per-request reasoning effort

For API clients, the recommended per-request way to control AI Studio thinking is `reasoning_effort`:

```json
{
  "model": "aistudio-auto",
  "reasoning_effort": "medium"
}
```

This works when **Settings** -> **API Server** -> **Request Controls** -> **Accept API Reasoning Effort** is enabled and Google AI Studio is selected under **Reasoning Effort Providers**.

AI Studio maps effort values to Thinking Level:

| API effort | AI Studio Thinking Level |
|------------|--------------------------|
| Not sent, `auto` | Chat/off mode |
| `minimum`, `minimal` | `Minimal` |
| `low` | `Low` |
| `medium` | `Medium` |
| `high`, `max`, `xhigh` | `High` |

This is usually nicer than changing the global IRP setting or adding macros to prompts, especially when the client already has a reasoning-effort control.

!!! note "Model-specific rounding"
    Some AI Studio models expose fewer levels, so IntenseRP picks the closest supported one. The old Gemini 2.5 budget mappings are still kept for compatibility, but those models are rejected at runtime because they are paid in AI Studio.

### Send Thinking

When enabled, AI Studio thinking summaries are included in the API response, wrapped in `<think>` tags.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Send Thinking**

---

## :material-magnify: Search and URL Context

Google AI Studio exposes two browsing-style tools in the web UI:

### Enable Search

Toggles **Grounding with Google Search**.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Enable Search**

### Enable URL Context

Toggles **URL Context** browsing.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Enable URL Context**

!!! note "Gemma 4 models"
    Gemma 4 models force Google Search grounding on and do not expose URL Context in AI Studio, so IntenseRP treats Search as enabled and skips the URL Context toggle for them.

!!! note "Grounding payloads"
    AI Studio can emit extra grounding/search payloads into the same response stream. IntenseRP strips those provider-specific payloads and forwards only the assistant text.

---

## :material-text-box-edit: System Prompt Field

AI Studio has its own **System Instructions** box, and IntenseRP can optionally use it for the leading `system` messages in your chat.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Use System Prompt Field**

When this is enabled, IntenseRP:

- pulls consecutive `system` messages from the start of the request
- pastes them into AI Studio's native System Instructions UI
- removes those leading `system` messages from the normal chat prompt before sending
- also moves your configured prompt injection there when **Injection Position** is set to `Before`

Anything after the first non-`system` message stays in the normal prompt on purpose. Mid-chat `system` messages are left alone, because AI Studio's separate system field is global to the whole chat and mixing both behaviors would get weird fast.

!!! note "Startup cleanup"
    AI Studio stores system instructions in browser local storage. When this option is enabled, IntenseRP clears that local cache once on page load and refreshes the tab so old instructions do not pile up.

---

## :material-file-upload: File Upload Mode

Google AI Studio can now upload the prompt as a text file through its native media picker flow.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Send As Text File**

This is a best-effort implementation because AI Studio does not expose a clean direct file input - it opens a native picker from the media menu instead.

!!! note "First upload on a fresh AI Studio account"
    On accounts that have not uploaded media before, AI Studio may show a copyright acknowledgement dialog after you pick the file.

    IntenseRP now waits briefly for that dialog and clicks **Agree to the copyright acknowledgement** automatically when it appears.

### Text File Message

Optional text to send alongside the uploaded prompt file.

Leave it empty to try file-only requests.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Text File Message**

### File Upload Timeout

Controls how long IntenseRP waits (in seconds) for the send button to become available after the file is selected.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **File Upload Timeout**

---

## :material-tune: Sampling and output controls

Google AI Studio is currently the only provider in IntenseRP that applies these OpenAI-style request controls in the web UI when the selected model exposes the matching fields:

- `temperature`
- `top_p`
- `max_tokens`

You can set defaults in Settings, and request-level API values still win when provided.

Some models don't expose every sampling field. For example, `Gemini 3.8 Flash`, `Gemini 3.7 Flash`, `Gemini 3.6 Flash`, `Gemini 3.5 Flash`, and `Gemini 3.1 Flash Lite` currently have no Temperature or Top P inputs, so IntenseRP skips those controls for those models and still applies Max Output Tokens.

### Temperature

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Temperature**

Default is `1.0`.

### Top P

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Top P**

Default is `0.95`.

### Max Output Tokens

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Max Output Tokens**

Default is `65536`.

Gemma 4 models are capped at `32768`, so IntenseRP clamps larger request or settings values before applying the web UI control.

---

## :material-shield-off: Safety Filters

On first AI Studio startup, IntenseRP automatically moves AI Studio's safety sliders to their lowest position once for that browser session.

!!! warning "What it does"
    This only adjusts the safety controls that AI Studio exposes in the UI. It does not override provider policy or guarantee any specific output.

---

## :material-shield-off: Blocked-response handling

Google AI Studio can block a reply on the backend. When that happens, you usually get either:

- no usable assistant text in the stream, or
- a turn in the UI that shows **Content blocked** in the **Safety Ratings** button area

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Blocked-Response Handling**

When this setting is enabled, IntenseRP runs a bounded recovery flow:

1. It watches the intercepted AI Studio response and the latest assistant turn for a hard-block signal
2. If the turn is blocked, IntenseRP edits that assistant message in-place
3. It replaces the blocked turn with your configured **Replacement Message**
4. It sends your configured **Continue Nudge** as a normal text message
5. It retries this up to 3 follow-up nudges

Blocked attempts are not forwarded to the API stream. If a recovery attempt produces usable assistant text, that retry streams normally.

!!! warning "Limits"
    This does not override AI Studio policy or guarantee recovery. It only describes how IntenseRP handles blocked turns when the setting is enabled.

!!! warning "Reuse Matching Chat gets disabled for that chat"
    If IntenseRP detects a blocked turn in the current AI Studio chat, it clears that chat out of the clean-regeneration cache.

    In plain English: the next identical request will start fresh instead of trying to regenerate inside that blocked chat.

### CAARS prelude

> "We've been trying to reach you about your caar's extended warranty."

CAARS is an optional prelude for AI Studio blocked-response handling. It only appears when **Blocked-Response Handling** is enabled.

When enabled, IntenseRP sends the original prompt to a secondary **Savior Model** first, with that model's thinking level set as low as AI Studio allows. The default is `Gemini 3.1 Flash Lite`. IntenseRP waits until either the secondary request finishes on its own or the assistant turn has produced 5 meaningful visible text updates. If the 5-update threshold wins while the secondary request is still running, IntenseRP clicks AI Studio's **Stop** button. Then it edits that assistant turn and replaces it with your configured **Replacement Message**, even if the turn was not blocked.

After that, IntenseRP switches back to your normal AI Studio **Model** and sends your configured **Continue Nudge**. The main model's answer is the one that streams back to the API. If the main model is blocked too, the normal edit + continue retry flow still runs.

!!! note "Reuse Matching Chat"
    CAARS starts from a fresh AI Studio chat for the prelude and skips **Reuse Matching Chat** for that request. It needs the secondary turn and the main continuation to live in the same fresh chat.

### Savior Model

The AI Studio model used for the CAARS prelude.

Default is `Gemini 3.1 Flash Lite`.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Savior Model**

### Replacement Message

Text used to replace the blocked assistant turn before IntenseRP sends the continue nudge.

Default is `.`.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Replacement Message**

### Continue Nudge

Text IntenseRP sends as the next user message after a blocked assistant turn is replaced.

Default is `Continue.`.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Continue Nudge**

### Edit Save Timeout

How long IntenseRP waits for AI Studio to finish saving the edited assistant turn before it sends the continue nudge.

Default is `10` seconds.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Edit Save Timeout**

If the continue nudge appears before the blocked assistant message has actually changed to your **Replacement Message**, increase this first.

### Edit Save Retries

How many extra times IntenseRP tries to save the edited assistant turn if AI Studio is slow to expose or accept the save action.

Default is `2`.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Edit Save Retries**

!!! note "Why this exists"
    AI Studio sometimes renders the assistant edit controls before the save action is really ready. This setting gives the workaround a couple more chances before it gives up and sends an error instead of accidentally nudging from an unsaved blocked turn.

!!! tip "Sent as plain text"
    The continue nudge is always typed into the composer normally.

    Even if **Send As Text File** is enabled for the original prompt, IntenseRP does **not** upload the nudge as a file.

---

## :material-refresh: Reuse Matching Chat

When enabled, IntenseRP tries to click AI Studio's regenerate action if:

1. The new prompt matches the cached last prompt
2. Effective AI Studio settings also match

Otherwise it opens a fresh chat.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Reuse Matching Chat**

!!! note "No Search Older Matching Chats here (yet)"
    Google AI Studio does **not** support **Search Older Matching Chats** right now.

    So for AI Studio, **Reuse Matching Chat** only checks the currently remembered latest chat instead of trying older cached conversations.

!!! note "Blocked chats are skipped"
    If AI Studio blocks a turn and **Blocked-Response Handling** kicks in, IntenseRP clears that chat from the clean-regeneration cache instead of reusing it later.

!!! note "Mutually exclusive with Preflight Next Chat"
    **Reuse Matching Chat** needs to keep the completed chat around so it can press Regenerate later.

    **Preflight Next Chat** immediately moves on to a fresh blank chat after a successful response. Since those two ideas want opposite things from the browser tab, IntenseRP only lets you keep one enabled.

---

## :material-flight-takeoff: Preflight Next Chat

When enabled, IntenseRP starts preparing the next Google AI Studio chat as soon as a successful response finishes.

It opens a fresh temporary chat, reapplies the previous request's AI Studio controls, and leaves the composer empty. The next request can then skip some setup and go straight to filling in the prompt.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Preflight Next Chat**

If the next request changes model, thinking, search, URL Context, sampling, or system prompt settings, IntenseRP adjusts the preflighted blank chat before sending the prompt. So the feature is best-effort speed, not a promise to ignore request-level overrides.

!!! note "CAARS preflights the savior turn"
    When CAARS is enabled, Preflight Next Chat prepares the blank chat for the **Savior Model** prelude, because that is the first AI Studio request CAARS sends.

    After the savior turn is replaced, IntenseRP still switches back to your main model in that same chat and sends the configured continue nudge.

---

## :material-translate: UI language requirement

The Google AI Studio driver currently expects the AI Studio UI language to be English (`en` / `en-US`).

If you see a warning about the language:

1. Switch your Google account language to **English**
2. Reload the AI Studio tab
3. Retry / restart the browser from IntenseRP if needed

### Assume English UI

If AI Studio reports the wrong language value (and complains that the UI must be in English) while the visible page is actually English, you can skip that detection:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Google AI Studio** -> **Assume English UI**

!!! warning "Last resort"
    Leave this off unless you're absolutely sure the AI Studio UI is English. It does not translate the page or make non-English buttons searchable; it only tells IntenseRP to skip the language safety check.

---

## :material-code-tags: Per-message macros

You can add `[[...]]` macros to the latest user message to override behavior for that request only.
All macros are stripped before sending.

| Macro | Effect |
|------|--------|
| `[[think]]` | Use the configured Thinking Level |
| `[[nothink]]`, `[[r0]]` | Force the lowest available Thinking Level and suppress `<think>` output |
| `[[r1]]`, `[[r2]]`, `[[r3]]`, `[[r4]]` | Pick a Thinking Level tier (mapped per model) |
| `[[search]]` | Force Search on |
| `[[nosearch]]`, `[[no_search]]` | Force Search off |
| `[[url]]`, `[[urlcontext]]` | Force URL Context on |
| `[[nourl]]`, `[[no_url]]` | Force URL Context off |
| `[[nocaars]]`, `[[nocars]]` | Disable CAARS for this request |

`[[nocaars]]` / `[[nocars]]` only matters when AI Studio blocked-response handling and CAARS are both enabled. Otherwise it has nothing to turn off.

!!! tip "Model-name suffixes"
    `reasoning_effort` is the recommended API-side method now, but you can still append a Thinking Level suffix directly to the API model string, for example:

    - `aistudio-auto-high`
    - `aistudio-auto-low`
    - `aistudio-auto-r4`

    IntenseRP strips that suffix, applies the Thinking Level override, then processes the remaining `aistudio-*` behavior preset as normal.

---

## :material-format-list-checks: Quick Reference

| Setting | What It Does | Default |
|---------|--------------|---------|
| **Request Capture Mode** | Captures responses with Replay or CDP Teeing | Replay |
| **Model** | Selects AI Studio's real model picker | Gemini 3.1 Pro |
| **Enable Thinking** | Uses a higher Thinking Level on supported AI Studio models | Off |
| **Thinking Level** | Picks the Thinking Level when Thinking is enabled | Medium |
| **Send Thinking** | Includes AI Studio thinking summaries in response | Off |
| **Enable Search** | Toggles Google Search grounding | Off |
| **Enable URL Context** | Toggles URL Context browsing | Off |
| **Use System Prompt Field** | Moves leading system messages into AI Studio's System Instructions UI | Off |
| **Send As Text File** | Uploads the prompt through AI Studio's media picker | Off |
| **Text File Message** | Optional text sent alongside the uploaded file | (empty) |
| **File Upload Timeout** | Seconds to wait for the send button after file selection | `20` |
| **Blocked-Response Handling** | Attempts a bounded blocked-response recovery flow | Off |
| **CAARS** | Runs a secondary-model prelude before the main AI Studio model | Off |
| **Savior Model** | Model used for the CAARS prelude | `Gemini 3.1 Flash Lite` |
| **Replacement Message** | Text used to replace a blocked assistant turn before retrying | `.` |
| **Continue Nudge** | Follow-up user message sent after a blocked turn | `Continue.` |
| **Edit Save Timeout** | Seconds to wait for the blocked-turn edit to save | `10` |
| **Edit Save Retries** | Extra save attempts before giving up | `2` |
| **Temperature** | Default temperature | `1.0` |
| **Top P** | Default top-p value | `0.95` |
| **Max Output Tokens** | Default output token budget | `65536` |
| **Auto Login Redirect Timeout (s)** | Wait before falling back to manual Google completion | `15` |
| **Humanize Mouse Movements** | Slows AI Studio UI actions with pointer movement and tiny pauses | On |
| **Assume English UI** | Skips AI Studio's `<html lang>` safety check when the visible UI is definitely English | Off |
| **Assume Paid Model Access** | Allows Gemini 2.5 requests for accounts that have paid AI Studio access | Off |
| **Paid Model Access Emails** | Optional allowlist for paid-access Google accounts | (empty) |
| **Reuse Matching Chat** | Regenerates on duplicate prompts | Off |
| **Preflight Next Chat** | Prepares a blank chat after successful responses | Off |

---

## :material-arrow-left: Back to Providers

[:material-arrow-left: Providers Overview](../providers.md)
