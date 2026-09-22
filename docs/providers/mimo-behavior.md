---
icon: providers/xiaomi
---

# :providers-xiaomi: Xiaomi MiMo Behavior

This page covers the toggles and notes for **Xiaomi MiMo** (`aistudio.xiaomimimo.com`).

MiMo is a fairly simple chat UI once it loads, but availability is region-dependent. If the browser can't reach MiMo at all, the provider may fail with `ERR_CONNECTION_REFUSED` before the normal sign-in flow even has a chance to appear.

!!! warning "Regional availability"
    If MiMo is unavailable from your location, use a network setup that is allowed by the service and can reach the MiMo site. IntenseRP's MiMo-specific proxy setting only affects the provider browser context.

---

## :material-tune: Modes (model IDs)

In IntenseRP, the `model` you send through the OpenAI-compatible API is usually a **behavior preset**. MiMo always produces thinking-style text internally, so these modes mainly decide whether IntenseRP forwards or filters that text.

| Model ID | Behavior |
|---|---|
| `mimo-auto` | Uses your MiMo Behavior settings |
| `mimo-chat` | Filters MiMo `<think>...</think>` text from the API response |
| `mimo-reasoner` | Forwards MiMo `<think>...</think>` text to the API client |

When **Use Universal Model Names** is enabled, `intenserp-auto`, `intenserp-chat`, and `intenserp-reasoner` work the same way for MiMo when it's the active provider.

---

## :material-chip: Real MiMo model selection (web UI)

IntenseRP can switch MiMo's real model picker in the web UI:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Model**

Supported options right now:

- **MiMo-V2.6-Pro** (default)
- **MiMo-V2.6-Flash**
- **MiMo-V2.5-Pro** (retired)
- **MiMo-V2.5** (retired)

If **Use Universal Model Names** is enabled, these real model picker entries also appear as request-level model IDs:

| Real model | Example IDs |
|---|---|
| MiMo-V2.6-Pro | `mimo-v2-6-pro-auto`, `mimo-v2-6-pro-chat`, `mimo-v2-6-pro-reasoner` |
| MiMo-V2.6-Flash | `mimo-v2-6-flash-auto`, `mimo-v2-6-flash-chat`, `mimo-v2-6-flash-reasoner` |
| MiMo-V2.5-Pro *(retired)* | `mimo-v2-5-pro-auto`, `mimo-v2-5-pro-chat` *(aliased to MiMo-V2.6-Pro)* |
| MiMo-V2.5 *(retired)* | `mimo-v2-5-auto`, `mimo-v2-5-chat` *(aliased to MiMo-V2.6-Pro)* |

Those IDs switch the MiMo web UI model for that request, then apply the `auto` / `chat` / `reasoner` behavior on top.

---

## :material-brain: Thinking

MiMo does *not* expose a Thinking toggle in the web UI. The provider stream can include `<think>...</think>` text, and IntenseRP can either forward it or strip it before sending the response to your API client.

### Send Thinking

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Send Thinking**

When enabled, IntenseRP forwards the streamed `<think>` text. When disabled, IntenseRP filters it out and only sends the answer text.

!!! note "Mode IDs can override this"
    `mimo-chat` forces thinking output off for that request. `mimo-reasoner` forces it on. `mimo-auto` follows the setting above.

---

## :material-calculator: Count Tokens

MiMo's response stream can include token usage metadata. When **Count Tokens** is enabled, IntenseRP forwards that as OpenAI-style `usage` fields (`prompt_tokens`, `completion_tokens`, `total_tokens`) when the stream provides it.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Count Tokens**

This is enabled by default.

---

## :material-magnify-remove-outline: Search

MiMo does not currently expose a Search toggle in the web UI, so IntenseRP does not provide one either.

If a client sends search-style macros, they will not make MiMo browse the web. Tiny mercy: at least there is no extra search button to chase around the page.

---

## :material-earth: Proxy

MiMo can be unavailable from some regions even when the rest of IntenseRP works normally. If that happens, the browser may show or log `ERR_CONNECTION_REFUSED`.

If your setup is permitted to use a proxy, you can route only MiMo's provider browser through it:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Proxy** -> **Use Proxy**

### Proxy URL

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Proxy** -> **Proxy URL**

Supported URL schemes:

```text
http://127.0.0.1:8080
https://proxy.example:8443
socks4://127.0.0.1:1080
socks5://user:pass@127.0.0.1:1080
```

The scheme is the proxy type. If your proxy needs credentials, put them in the URL as `user:pass@host`.

!!! note "Restart required"
    Proxy settings apply when the MiMo browser context is created. After changing them, stop and start the provider browser.

!!! tip "Global browser proxy"
    The global **Browser Proxy URL** under **Browser & Runtime -> Browser Environment** still exists. MiMo's provider-specific proxy overrides it only when **Use Proxy** is enabled for MiMo.

---

## :material-cookie: Popups and consent

MiMo can show a few blocking popups before the chat is usable. IntenseRP handles these automatically where possible:

- announcement popup close button
- cookie consent popup
- policy agreement checkbox + confirm button

### Decline Cookies Automatically

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Decline Cookies Automatically**

When enabled, IntenseRP clicks MiMo's **Decline All** cookie button if it appears. This is enabled by default.

---

## :material-file-upload: File Upload Mode

Instead of typing the whole prompt into MiMo's textarea, IntenseRP can upload the prompt as a `.txt` file. This is useful for longer prompts.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Send As Text File**

### Text File Message

MiMo requires text alongside an uploaded file. If the textarea is empty, the send button stays disabled.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Text File Message**

Default:

```text
Please read the attached file and respond to it.
```

### File Upload Timeout

How long IntenseRP waits for MiMo to finish parsing the uploaded text file.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **File Upload Timeout**

| | |
|---|---|
| **Default** | 30 seconds |

### Message Send Timeout

How long IntenseRP waits for MiMo's send button to become available after entering text.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Message Send Timeout (s)**

| | |
|---|---|
| **Default** | 8 seconds |

---

## :material-refresh: Reuse Matching Chat

Reuse Matching Chat regenerates the last matching MiMo chat instead of creating a new chat when the prompt and relevant settings match.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Reuse Matching Chat**

MiMo also supports **Search Older Matching Chats**:

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Search Older Matching Chats**

That keeps up to 7 older cached MiMo chats per account/profile so duplicate prompts can reuse more than just the latest chat.

See also: [:material-layers-triple-outline: Search Older Matching Chats](../features/multi-slot-cache.md)

---

## :material-wrench-clock: MiMo quirks and timing

MiMo streams through an EventStream request. IntenseRP waits for that request to appear, then tees the response through Chrome DevTools Protocol and converts it into OpenAI-style SSE chunks.

### Completion Request Timeout

How long IntenseRP waits after clicking **Send** or **Regenerate** for MiMo's chat request to appear.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Quirks** -> **Completion Request Timeout (s)**

| | |
|---|---|
| **Default** | 150 seconds |
| **Minimum** | 5 seconds |

### First Chunk Timeout

How long IntenseRP waits for MiMo's response stream to produce its first chunk after the request starts.

:material-arrow-right: **Settings** -> **Provider Behavior** -> **Xiaomi MiMo** -> **Quirks** -> **First Chunk Timeout (s)**

| | |
|---|---|
| **Default** | 150 seconds |
| **Minimum** | 5 seconds |

---

## :material-shield-alert-outline: Provider-side filtering

MiMo can stop some requests with a provider-side `sensitive_query` event. When that happens, IntenseRP returns a clear error instead of forwarding MiMo's canned blocked-response text.

There is no MiMo blocked-response recovery flow.

The sensitive-query event appears to happen mid-stream, and IntenseRP cannot recover the missing content.

In practice, this means some requests may stop abruptly based on MiMo's provider-side policy checks. IntenseRP reports the failure instead of trying to work around it.

---

## :material-key: Login notes

MiMo uses a Xiaomi account login page. Auto Login can fill the account and password fields, accept Xiaomi's login agreement checkbox, submit the form, and then wait for the redirect back to MiMo.

If the flow stalls or starts requiring 2FA, CAPTCHAs, etc., you'll be asked to finish the login manually in the browser. Persistent Sessions are **heavily** recommended so you do not need to repeat that dance every time.

See: [:material-key: Login & Sessions](../features/login-sessions.md)

---

## :material-translate: UI language requirement

The MiMo driver expects the web UI to be English (`en-*`). If the page language is not English, IntenseRP may not be able to find the right controls reliably (just like with all other providers) as it relies on the text of some buttons, labels, divs, etc.

If you see a MiMo UI language warning:

1. Change MiMo/Xiaomi account language to English
2. `Try again` in IRP
3. Restart the provider browser if needed

---

## :material-code-tags: Per-message macros

You can add simple `[[...]]` macros to the latest user message to override a few MiMo settings for that request only. IntenseRP strips the macros before sending the message to MiMo.

| Macro | Effect |
|------|--------|
| `[[think]]`, `[[r1]]` | Force thinking output on |
| `[[nothink]]`, `[[no_think]]`, `[[r0]]` | Force thinking output off |
| `[[file]]`, `[[sendfile]]` | Force Send As Text File on |
| `[[nofile]]`, `[[no_file]]` | Force Send As Text File off |

!!! note "Scope"
    Only macros from the latest user message apply. They do not persist across requests.

---

## :material-format-list-checks: Quick Reference

| Setting | What It Does | Default |
|---------|--------------|---------|
| **Model** | Selects MiMo's real model picker | MiMo-V2.6-Pro |
| **Show Retired Models** | Displays retired MiMo models in the dropdown | Off |
| **Send Thinking** | Forwards `<think>` text to the API client | Off |
| **Count Tokens** | Returns token usage when MiMo provides it | On |
| **Send As Text File** | Uploads the prompt as `.txt` | Off |
| **Text File Message** | Text pasted alongside uploaded file | `Please read the attached file and respond to it.` |
| **File Upload Timeout** | Seconds to wait after upload | 30 |
| **Message Send Timeout (s)** | Seconds to wait for send button | 8 |
| **Decline Cookies Automatically** | Clicks MiMo's Decline All cookie button | On |
| **Use Proxy** | Routes only MiMo's browser through a proxy | Off |
| **Proxy URL** | HTTP/HTTPS/SOCKS proxy URL for MiMo | (empty) |
| **Reuse Matching Chat** | Regenerates on duplicate prompts | Off |
| **Search Older Matching Chats** | Reuses up to 7 older matching MiMo chats | Off |
| **Completion Request Timeout (s)** | Seconds to wait for MiMo's backend request | 150 |
| **First Chunk Timeout (s)** | Seconds to wait for MiMo's stream to start | 150 |

---

## :material-arrow-left: Back to Providers

[:material-arrow-left: Providers Overview](../providers.md)
