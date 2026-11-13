---
icon: providers/qwen
---

# :providers-qwen: QwenLM Behavior

This page covers the toggles and options that control how IntenseRP interacts with **QwenLM** (`chat.qwen.ai`).

---

## :material-call-split: Request Capture Mode

Controls how IntenseRP captures QwenLM's streaming response.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Request Capture Mode**

**Replay** is the default. IntenseRP intercepts the Qwen request, replays it internally, streams that replay to the API client, and then gives the captured response back to the page. It's the older, known-good path.

**CDP Teeing** is the newer alternative. IntenseRP leaves Qwen's real browser request alone, tees the real response through Chrome DevTools Protocol, and feeds those bytes through the same Qwen stream parser. This lets Qwen's page JavaScript receive and process its own response normally while IntenseRP observes the stream.

!!! note "Default stays Replay"
    CDP Teeing is off by default for QwenLM. It's available if you want the browser-native request path, but Replay remains the safer default while this newer path gets more real-world mileage.

---

## :material-tune: Modes (model IDs)

In IntenseRP Next v2, the `model` you select in SillyTavern is mostly a **mode selector**, not a true model picker.

For QwenLM, these model IDs map to simple behavior presets:

| Model ID | Behavior |
|---|---|
| `qwen-auto` | Uses your IntenseRP settings |
| `qwen-chat` | Forces Thinking **off** and never emits `<think>` |
| `qwen-reasoner` | Forces Thinking **on** (Send Thinking follows your setting) |

---

## :material-chip: Real Qwen model selection (web UI)

IntenseRP can also switch QwenLM's *real* model picker in the web UI:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Model**

The list is intentionally "what Qwen shows in the dropdown". If your selected model is missing (UI rollout / region / UI change), IntenseRP logs a warning and keeps going.

---

## :material-head-cog: Thinking

QwenLM exposes three thinking modes in the web UI: **Auto**, **Thinking**, and **Fast**.

IntenseRP keeps it simple:

- **Enable Thinking** -> selects **Thinking**
- Thinking disabled -> selects **Fast**

### Enable Thinking

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Enable Thinking**

### Send Thinking

When enabled, IntenseRP includes QwenLM's thinking *summaries* in the response, wrapped in `<think>` tags.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Send Thinking**

!!! note "Thinking content"
    QwenLM sends a short thinking summary stream. This is what IntenseRP forwards (not hidden internal chain-of-thought).

---

## :material-magnify: Search

QwenLM Web search can be toggled via IntenseRP.

Qwen streams tool/search payloads into the same response stream. For stability, IntenseRP strips these tool payloads, so search results are **not sent** to the client.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Enable Search**

---

## :material-tools: Tools

QwenLM's **Tools** switch lives in the same `+` menu as Search and Upload.

IntenseRP keeps Tools **off by default**. If **Enable Tools** is turned on, the driver will try to flip Qwen's Tools switch on before sending. If Qwen shows the switch as disabled (which occurs on some models such as `Qwen4-Max-Preview`), IntenseRP stops trying for that request and leaves it off.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Enable Tools**

!!! warning "Experimental"
    Leave **Enable Tools** disabled unless you are intentionally testing it. Qwen's tool payloads are not a polished IntenseRP workflow yet, and anything tool/search-shaped in the stream is still treated defensively.

!!! note "Separate from Search"
    **Enable Tools** is its own toggle. It doesn't replace **Enable Search**, and enabling one does not automatically enable the other.

---

## :material-shield-alert-outline: Provider-side filtering

QwenLM can abort a request with a `data_inspection_failed` stream event, sometimes even after part of the assistant response has already arrived. When that happens, IntenseRP returns a clear terminal error instead of waiting for the response timeout.

There isn't a Qwen recovery flow for this event yet. It happens on Qwen's side, so IntenseRP reports it and stops the request.

---

## :material-calculator: Count Tokens

QwenLM reports token usage during the response stream. When enabled, IntenseRP captures those values and returns them in the OpenAI-style `usage` fields (`prompt_tokens`, `completion_tokens`, `total_tokens`). This is enabled by default.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Count Tokens**

---

## :material-file-upload: File Upload Mode

Instead of typing your message into QwenLM's chat box, IntenseRP can upload it as a text file attachment. This is useful for very long prompts that might hit input limits.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Send As Text File**

!!! warning "QwenLM file uploads are flaky"
    QwenLM does not handle files very reliably right now. If you are sending important context (system prompts, lore, character sheets), you will usually get better results by keeping File Upload Mode **off** and sending plain text instead.

### Text File Message

Optional text that IntenseRP pastes into QwenLM *alongside* the uploaded file.

Leave it empty to send a file-only message.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Text File Message**

!!! info "Helps a lot"
    QwenLM treats files as attachments rather than part of the message content, so it can be helpful to include a short message like "Context attached, please read it before answering" to make sure the model knows to look at the file.

### File Upload Timeout

After uploading, the send button can take a moment to become available. This setting controls how long IntenseRP waits (in seconds) before giving up.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **File Upload Timeout**

### Message Send Timeout

QwenLM doesn't render the send button until there's something to send (typed text or an uploaded file). This timeout controls how long IntenseRP waits for the send button to appear in normal (non-file) mode.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Message Send Timeout (s)**

---

## :material-wrench-clock: Qwen Quirks & Timing

QwenLM's web UI can sometimes show that it's sending before the actual completion request appears on the network. When that happens too slowly, IntenseRP may think the click was swallowed even though Qwen is still waking up.

### Completion Request Timeout

How long (in seconds) IntenseRP waits after clicking **Send** or **Regenerate** for QwenLM's completion request to appear.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Quirks** -> **Completion Request Timeout (s)**

| | |
|---|---|
| **Default** | 150 seconds |
| **Minimum** | 5 seconds |

If you're seeing `QwenLM: completion request not observed`, try raising this. This is different from the stream timeout: it only covers the gap before Qwen's backend request starts.

### First Chunk Timeout

How long (in seconds) IntenseRP waits for QwenLM's response stream to produce its first chunk after the completion request has started.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Quirks** -> **First Chunk Timeout (s)**

| | |
|---|---|
| **Default** | 150 seconds |
| **Minimum** | 5 seconds |

If you're seeing `timed out waiting for intercepted first chunk` errors on QwenLM, this is the setting to raise.

---

## :material-shield-check: Provider guardrails (recommended)

QwenLM has a couple of web settings that can silently change how your prompt gets sent (for example, turning big messages into file attachments).

To keep things predictable, IntenseRP tries to auto-disable these when the driver starts:

- Large Text as File
- Split Large Chunks
- Memory
- History Memory

If any of those get changed, you might see the Qwen tab reload once. That is normal.

---

## :material-refresh: Reuse Matching Chat

Reuse Matching Chat tries to keep chats tidy: when you send the exact same prompt twice in a row, IntenseRP clicks Qwen's "Regenerate" instead of creating a brand new chat. This is especially useful if you swipe a lot in SillyTavern and want fewer duplicate chats.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Reuse Matching Chat**

!!! tip "Search Older Matching Chats"
    QwenLM also supports **Provider Behavior** -> **QwenLM** -> **Search Older Matching Chats**.

    That keeps up to 7 older cached Qwen chats per account, so IntenseRP can reopen a matching older conversation and regenerate there instead of only checking the most recent one.

---

## :material-delete-clock-outline: Delete Chat After Reply

If you want Qwen's chat list cleaned up automatically, IntenseRP can delete the completed Qwen chat after a successful reply finishes.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **QwenLM** -> **Delete Chat After Reply**

!!! warning "Slower requests"
    This adds extra cleanup work after each request, so it can slow requests down quite a bit.

!!! note "No chat reuse here"
    This does **not** work together with **Reuse Matching Chat** or **Search Older Matching Chats**.

See also: [:material-delete-clock-outline: Chat Auto-Deletion](../features/chat-auto-deletion.md)

---

## :material-key: Login notes

QwenLM supports Auto Login (email + password). Persistent Sessions are still recommended, since they reduce how often you need to log in.

See: [:material-key: Login & Sessions](../features/login-sessions.md)

---

## :material-translate: UI language requirement

The QwenLM driver currently expects the Qwen web UI language to be English (`en` / `en-US`). If QwenLM is set to another language, IntenseRP may fail to find buttons/toggles reliably.

If you see a warning about QwenLM UI language:

1. Change QwenLM language to **English**
2. Reload the page (F5 / Ctrl+R)
3. Retry / restart the browser from IntenseRP if needed

---

## :material-code-tags: Per-message macros

You can add simple `[[...]]` macros to the *latest* user message in SillyTavern to override certain QwenLM Behavior settings for that request only.

All macros are stripped from the message before sending it to QwenLM.

| Macro | Effect |
|------|--------|
| `[[think]]`, `[[r1]]` | Force Thinking on |
| `[[nothink]]`, `[[r0]]` | Force Thinking off |
| `[[search]]` | Force Search on |
| `[[nosearch]]`, `[[no_search]]` | Force Search off |
| `[[tool]]`, `[[tools]]` | Force Tools on |
| `[[notool]]`, `[[notools]]`, `[[no_tool]]`, `[[no_tools]]` | Force Tools off |
| `[[file]]` | Force Send As Text File on |
| `[[nofile]]` | Force Send As Text File off |

!!! note "Scope"
    Only macros from the latest user message apply. They do not persist across requests.

---

## :material-format-list-checks: Quick Reference

| Setting | What It Does | Default |
|---------|--------------|---------|
| **Request Capture Mode** | Captures responses with Replay or CDP Teeing | Replay |
| **Model** | Selects QwenLM's real model picker (UI) | Qwen3.5-Plus |
| **Enable Thinking** | Switches QwenLM into Thinking (vs Fast) | Off |
| **Send Thinking** | Includes thinking summaries in response | Off |
| **Count Tokens** | Returns token usage in API responses | On |
| **Enable Search** | Enables QwenLM Web search | Off |
| **Enable Tools** | Enables QwenLM Tools when available | Off |
| **Send As Text File** | Uploads prompt as .txt | Off |
| **Text File Message** | Text pasted alongside uploaded file | (empty) |
| **File Upload Timeout** | Seconds to wait after upload | 20 |
| **Message Send Timeout (s)** | Seconds to wait for send button | 8 |
| **Completion Request Timeout (s)** | Seconds to wait for Qwen's backend request | 150 |
| **First Chunk Timeout (s)** | Seconds to wait for Qwen's stream to start | 150 |
| **Reuse Matching Chat** | Regenerates on duplicate prompts | Off |
| **Delete Chat After Reply** | Deletes the completed Qwen chat after a successful reply | Off |

---

## :material-arrow-left: Back to Providers

[:material-arrow-left: Providers Overview](../providers.md)
