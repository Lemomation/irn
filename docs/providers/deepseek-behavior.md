---
icon: providers/deepseek
---

# :providers-deepseek: DeepSeek Behavior

This page covers the toggles and options that control how IntenseRP interacts with DeepSeek. Most of these are simple on/off switches, but a few have some nuance worth knowing about.

---

## :material-call-split: Request Capture Mode

Controls how IntenseRP captures DeepSeek's streaming response.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Request Capture Mode**

**Replay** is the default. IntenseRP intercepts the DeepSeek request, replays it internally, streams that replay to the API client, and then gives the captured response back to the page. It's the older and battle-tested method, and it works well in most cases.

**CDP Teeing** is the newer alternative path. IntenseRP leaves DeepSeek's browser request alone, tees the real response through Chrome DevTools Protocol, and feeds those bytes through the same DeepSeek stream parser. This lets the page JavaScript receive and process its own response normally while IntenseRP observes the stream.

!!! note "Default stays Replay"
    CDP Teeing is off by default for DeepSeek. It's there if you want the browser-native request path, but Replay remains the known-good default.

---

## :material-head-cog: DeepThink

DeepThink is DeepSeek's reasoning mode. When enabled, the model "thinks through" problems step-by-step before giving you an answer. This can make responses smarter and more thorough, but also slower and sometimes changes the tone. (for better or worse!)

### Enable DeepThink

Toggles the DeepThink button in DeepSeek's interface.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Enable DeepThink**

### Send DeepThink

When enabled, the model's thinking process is included in the response, wrapped in `<think>` tags:

```
<think>
Let me consider how to approach this...
The user seems to want a casual conversation.
I should respond in a friendly way.
</think>

Hey! What's up?
```

If disabled, you only get the final response - the thinking happens behind the scenes but isn't sent to SillyTavern.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Send DeepThink**

### Model Override

You can also control DeepThink per-request using the model name in SillyTavern:

| Model Name | Behavior |
|------------|----------|
| `deepseek-auto` | Uses your IntenseRP settings |
| `deepseek-chat` | Forces DeepThink **off** |
| `deepseek-reasoner` | Forces DeepThink **on** |

This is handy if you want to quickly switch modes without digging into settings.

---

## :material-magnify: Search

Toggles DeepSeek's web search feature. When enabled, the model can look things up online to provide more accurate or up-to-date information.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Enable Search**

!!! note
    Search results aren't directly included in the response - the model just uses them to inform its answer. You won't see citations or links unless the model decides to include them.

---

## :material-file-upload: File Upload Mode

Instead of typing your message into DeepSeek's chat box, IntenseRP can upload it as a text file attachment. This is useful for very long prompts that might hit DeepSeek's input limits.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Send As Text File**

### File Upload Timeout

When uploading files, DeepSeek takes a moment to process them before the send button becomes active. This setting controls how long IntenseRP waits (in seconds) before giving up.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **File Upload Timeout**

Default is 15 seconds, which should be plenty for most cases. Increase it if you're on a slow connection/PC or uploading very large prompts.

---

## :material-timer-sand: First Chunk Timeout

This controls how long IntenseRP waits for DeepSeek's response stream to actually start after the request has been sent.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **DeepSeek** -> **First Chunk Timeout (s)**

Default is 45 seconds.

If DeepSeek is being slow to wake up, or your machine/browser is not the fastest, raising this can help avoid false timeout errors.

!!! note
    This timeout is mainly about the start of the response stream, not the full generation time. Once the stream is active, the normal idle timeout logic takes over.

---

## :material-shield-off: Blocked-response handling

DeepSeek has a content filter that sometimes triggers with a refusal-style message. When **Blocked-Response Handling** is enabled, IntenseRP catches the filter signal and terminates the response cleanly instead of forwarding the refusal text.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Blocked-Response Handling**

!!! warning "What It Doesn't Do"
    This does not override provider policy or unlock blocked content. It only changes how detected refusal-style output is forwarded to the client.

### How It Works

When IntenseRP detects a `CONTENT_FILTER` status in the response stream:

1. It stops processing the response
2. Closes any open `<think>` tags (if DeepThink was active)
3. Signals the response as complete
4. The refusal message never reaches SillyTavern

---

## :material-refresh: Reuse Matching Chat

When you send the exact same prompt twice in a row, IntenseRP can regenerate the previous response instead of creating a brand new chat. This keeps things tidy and can sometimes give you a different (hopefully better) answer.

:material-arrow-right: **Settings** → **Provider Behavior** → **DeepSeek** → **Reuse Matching Chat**

### How It Works

1. IntenseRP caches the last prompt you sent (plus the effective DeepSeek settings used for it)
2. When a new request comes in, it compares the prompt and settings against the cache
3. If they match, it clicks the "Regenerate" button instead of starting fresh
4. If they don't match (or the button isn't available), it creates a new chat as usual

!!! note "Settings Changes"
    If you change DeepThink, Search, or Send As Text File, IntenseRP will start a new chat even if the prompt is identical. This makes sure the new settings actually apply to the request.

!!! tip "Swipe in SillyTavern"
    This is especially useful with SillyTavern's "swipe" feature. Each swipe sends the same prompt again, and **Reuse Matching Chat** makes sure you're regenerating rather than cluttering up DeepSeek with duplicate chats.

!!! note "Content-filtered chats"
    DeepSeek automatically disables the regenerate button if the last response was content-filtered. In that case, **Reuse Matching Chat** won't work, and IntenseRP will start a new chat instead.

!!! tip "Search Older Matching Chats"
    If you want **Reuse Matching Chat** to remember more than just the latest chat, enable **Provider Behavior** -> **DeepSeek** -> **Search Older Matching Chats** as well.

    It keeps up to 7 older cached DeepSeek chats per account, and if one of them matches the current prompt/settings, IntenseRP can reopen that chat and regenerate there instead.

    DeepSeek still blocks regeneration for content-filtered chats. If a chat gets content-filtered, IntenseRP skips saving it to the multi-slot cache.

---

## :material-delete-clock-outline: Delete Chat After Reply

If you want DeepSeek's own chat list to stay cleaner, IntenseRP can delete the completed DeepSeek chat after a successful reply finishes.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **DeepSeek** -> **Delete Chat After Reply**

!!! warning "Slower requests"
    This adds extra cleanup work after each request, so it can slow requests down quite a bit.

!!! note "No chat reuse here"
    This does **not** work together with **Reuse Matching Chat** or **Search Older Matching Chats**.

    If you enable auto-deletion, IntenseRP has to throw the chat away on purpose, so there is nothing left to regenerate later.

See also: [:material-delete-clock-outline: Chat Auto-Deletion](../features/chat-auto-deletion.md)

---

## :material-code-tags: Per-Message Macros

You can add simple `[[...]]` macros to the *latest* user message in SillyTavern to override certain DeepSeek Behavior settings for that request only.

All macros are stripped from the message before sending it to DeepSeek.

| Macro | Effect |
|------|--------|
| `[[think]]`, `[[r1]]` | Force DeepThink on |
| `[[nothink]]`, `[[r0]]` | Force DeepThink off |
| `[[search]]` | Force Search on |
| `[[nosearch]]` | Force Search off |
| `[[file]]` | Force Send As Text File on |
| `[[nofile]]` | Force Send As Text File off |

!!! note "Scope"
    Only macros from the latest user message apply. They do not persist across requests.

---

## :material-format-list-checks: Quick Reference

| Setting | What It Does | Default |
|---------|--------------|---------|
| **Request Capture Mode** | Captures responses with Replay or CDP Teeing | Replay |
| **Enable DeepThink** | Toggles reasoning mode | Off |
| **Send DeepThink** | Includes thinking in response | Off |
| **Enable Search** | Allows web search | Off |
| **Send As Text File** | Uploads prompt as .txt | Off |
| **File Upload Timeout** | Seconds to wait for upload | 15 |
| **First Chunk Timeout** | Seconds to wait for the response stream to start | 45 |
| **Blocked-Response Handling** | Handles detected refusal messages | Off |
| **Reuse Matching Chat** | Regenerates on duplicate prompts | Off |
| **Delete Chat After Reply** | Deletes the completed DeepSeek chat after a successful reply | Off |

---

## :material-arrow-left: Back to Providers

[:material-arrow-left: Providers Overview](../providers.md)
