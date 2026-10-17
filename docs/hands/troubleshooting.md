---
icon: material/bug
---

# :material-bug: Troubleshooting

This page is a practical checklist for diagnosing problems with IntenseRP Next v2 - from basic "is it running?" checks, through common fixes, to filing a useful bug report.

---

## :material-clipboard-check-outline: Quick checks (do these first)

1. **Is IntenseRP running?**
    - The main window should say **Running (Port XXXX)**.
    - If it says Ready/Stopped, click :material-play: **Start**.

2. **Is the browser open and logged in?**
    - A Chromium window should be open.
    - If you're on a provider login page, log in manually.
    - All providers also support Auto Login.
    - For GLM Chat, login requires solving a CAPTCHA (Persistent Sessions are strongly recommended).
    - For Moonshot, Google login may require manual confirmation/challenge steps, 2FA, or manually closing the popup after login.
    - For Perplexity, Auto Login starts the email-code flow, but you still need to type the 6-digit code.
    - For HuggingChat, use your Hugging Face username/email and password. If the account hit its monthly credits, disable that row in Saved Accounts and switch accounts.
    - For Google AI Studio, Auto Login can try to fill Google sign-in, but Google may still require manual confirmation.

3. **Is the provider UI language set to English?**
    - IntenseRP currently expects the provider UI language to be English.
    - DeepSeek: English (en / en-US)
    - GLM Chat: English (en-US)
    - Moonshot: English (en-US)
    - QwenLM: English (en / en-US)
    - Perplexity: English (en / en-US)
    - HuggingChat: English (en)
    - Google AI Studio: English (en / en-US)
    - If you see a warning/popup about UI language, change it in the provider browser window and **reload the page**, then retry.
    - If that doesn't fix it after changing the language, close IntenseRP and start it again.
    - Persistent Sessions help a lot with managing languages.

4. **Is your client pointing at the right endpoint?**
    - Default endpoint: `http://127.0.0.1:7777/v1`
    - If you changed the port, your client must match.

5. **If using API keys, is your client sending one?**
    - `Authorization: Bearer YOUR_KEY`

---

## :material-connection: Client cannot connect

Symptoms:

- "Connection refused"
- "Cannot reach server"
- SillyTavern Connect button stays red

Checklist:

- Confirm the server is running (status label shows Running).
- Confirm the port is correct (default `7777`).
- Confirm you are using `http://`, not `https://`.
- If connecting from another device:
    - Enable **Allow Local Network Access**
    - Use your PC's LAN IP (for example `http://192.168.1.100:7777/v1`)

See: [:material-lan: Network & API](../features/network-api.md)

### Verify the port is listening

=== ":material-microsoft-windows: Windows"

    In Command Prompt:

    ```bat
    netstat -ano | findstr :7777
    ```

=== ":material-linux: Linux"

    In a terminal:

    ```bash
    ss -ltnp | grep ':7777'
    ```

If nothing is listening, IntenseRP is not started, or it failed to start.

---

## :material-key-variant: 401 Unauthorized (API keys)

If **Require API Keys** is enabled and your client does not send a key, IntenseRP will reject requests.

Fix:

1. Go to :material-arrow-right: **Settings** → **API Server** → **Security**
2. Either disable **Require API Keys**, or add a key and configure your client to use it

See: [:material-lan: Network & API](../features/network-api.md)

---

## :material-window-close: Browser won't open / crashes immediately

Common causes:

- First-run browser install blocked (firewall, captive portal, restricted network)
- Antivirus interference
- Missing GUI dependencies (Linux)

What to try:

1. Restart IntenseRP and try again.
2. Check logs for "Verifying/Installing Chromium browser..." or install errors.
3. If running from source, try installing the browser once manually:

    ```bash
    playwright install chromium
    ```

See: [:material-console: Console & Logging](../features/console-logging.md)

---

## :material-folder-alert: Install folder / config directory issues

Symptoms:

- Settings "reset" between restarts
- Persistent Sessions never stick
- Logs are missing / not written
- The app works only when run as admin
- You moved `intenserp-next-v2.exe` out of its folder (release build)

Checklist:

- If you downloaded a release `.zip`, **extract it fully** before running. Do not run from inside the zip viewer.
- Keep `intenserp-next-v2.exe` next to the `_internal/` folder (release build). Do not rename or separate them.
- Avoid running from protected folders (like `C:\\Program Files`) if you use **Relative** config storage. Use a normal writable folder instead.
- If you use cloud-synced folders (OneDrive/Dropbox/network drives), try moving the app/config to a local folder - Persistent Sessions can get weird when files are locked or delayed.
- Check the active config directory:
    1. Open the folder where the app executable is
    2. Open `config_dir.txt`
    3. Confirm the folder exists and is writable
- Try switching config storage:
    - :material-arrow-right: **Settings** -> **Advanced** -> **Config Storage** -> **Config Storage Location**

See: [:material-cog: System](../features/system.md)

---

## :material-account-alert: Login problems (stuck on sign-in, loops, expired sessions)

Fixes, in order:

1. If you use **Sign In Automatically**, double-check **Current Provider** and the credentials saved under **Provider and Login** -> **Saved Accounts**.
2. GLM Chat requires a CAPTCHA during login. Auto Login can fill credentials, but you still need to solve the CAPTCHA in the browser window.
3. Try toggling **Keep Provider Sessions Signed In** off and back on (then Start again).
4. If **Keep Provider Sessions Signed In** is enabled but things feel "stuck", use **Delete Profile** (or **Clear All Profiles**) to reset the saved browser profile.
5. For DeepSeek / GLM Chat / Moonshot / QwenLM / Perplexity / HuggingChat / Google AI Studio, try manual login once (disable **Sign In Automatically** temporarily) to confirm the provider isn't blocking automated sign-in.

See:

- [:material-key: Login & Sessions](../features/login-sessions.md)
- [:material-cog: System](../features/system.md)

---

## :material-translate: Provider UI language is not English

IntenseRP currently relies on a few English UI elements (button labels/placeholders). If a provider is set to a non-English language, automation can fail in confusing ways.

Symptoms:

- You see a popup/warning about UI language not being English
- Requests fail immediately with an error mentioning UI language
- Buttons/toggles are "not found" even though the provider loaded

Fix:

1. In the provider browser window, change the language to English.
2. **Reload the provider page** after changing the language (F5 / Ctrl+R).
3. In IntenseRP, click **Retry** on the language prompt (or click **Stop** then **Start**).

If this keeps happening on fresh launches, check **Settings** -> **Browser & Runtime** -> **Browser Environment**. The browser locale now defaults to **English (en-US)**, which can help, but saved provider/account language preferences can still override it.

---

## :material-timer-sand: Requests are slow / appear to hang

Reality check:

- IntenseRP processes **one request at a time** by default. If multiple clients/requests are active, later requests will wait in a queue unless you are intentionally using the experimental parallel modes.
- First request after launch can be slower (browser warmup, login, UI settling).

What to check:

- Look for "Processing queued request..." in logs.
- If you run many parallel chats, reduce concurrency on the client side.

See: [:material-api: API Behavior](../advanced/api-behavior.md)

---

## :material-bug-check: Bug reports

If something is genuinely broken (crash, login loop, API failures), a good report makes it much easier to fix quickly.

<div class="grid cards" markdown>

-   :material-github: **Search existing issues**

    Check if someone already reported it (and maybe a workaround exists).

    [:arrow_right: Open issues](https://github.com/Lemomation/irn/issues)

-   :material-bug: **Open a new issue**

    If it's new, file a bug report with the template below.

    [:arrow_right: New issue](https://github.com/Lemomation/irn/issues/new/choose)

-   :material-console: **Collect logs**

    Logs are usually the difference between "can't reproduce" and "fixed".

    [:arrow_right: Console & Logging](../features/console-logging.md)

</div>

### :material-clipboard-check-outline: Before you file

- [ ] Reproduce once (so you can describe exact steps).
- [ ] If possible, reproduce with logs enabled (console and/or logfiles).
- [ ] If you want the easy route, enable **Bug Reports** in **Settings** -> **Logs and Troubleshooting** before reproducing.
- [ ] Note whether it happens on `deepseek-*`, `glm-*`, `moonshot-*`, `qwen-*`, `perplexity-*`, `huggingchat-*`, or `aistudio-*` (depending on your provider).
- [ ] If you are using LAN, try locally too (to rule out firewall/network issues).

### :material-clipboard-text-outline: What to include

- **Version** (app title bar)
- **OS** (Windows/Linux + version)
- **Install method** (release zip vs from source)
- **Provider** (DeepSeek, GLM Chat, Moonshot, QwenLM, Perplexity, HuggingChat, or Google AI Studio)
- **Client** (SillyTavern or other client + version)
- **Endpoint** (example: `http://127.0.0.1:7777/v1`)
- **Model** (example: `deepseek-auto` / `glm-auto` / `moonshot-auto` / `qwen-auto` / `perplexity-auto` / `huggingchat-auto` / `aistudio-auto`)
- **Streaming** (`stream: true` or `stream: false`)
- **Expected vs actual**
- **Logs** (`Bug Report` zip from **Tools**, console dump, or `logs/log_*.txt`)

### :material-archive: Easiest way to collect useful data

If you have already enabled the Bug Report settings, use the built-in helper instead of manually hunting for files:

1. Go to **Settings** → **Logs and Troubleshooting** -> **Diagnostics**
    - Enable **Keep an Internal Log** to capture detailed logs
    - Optionally enable **Also Save the Last Prompt** to include the latest prompt snapshot in the bundle
2. Reproduce the issue once with that enabled
3. Click **Tools**
4. Click **Bug Report**
5. Save the generated `.zip`

That bundle can include a private redacted diagnostics log, the latest saved prompt snapshots, timestamps, and a `latest-prompt.txt` file for the newest captured prompt.

!!! danger "Redact secrets"
    Logs and prompt bundles can contain provider emails, file paths, message content, and API keys. Always redact personal data before posting publicly.

### :material-text-box-outline: Copy/paste template

```text
Title:

Version:
OS:
Install method:
Provider:
Client:
Endpoint:
Model:
Streaming:

Steps to reproduce:
1)
2)
3)

Expected:
Actual:

Logs:
- (attach `logs/log_*.txt` or a console dump, redacted)
```

---

## :material-arrow-right: Related pages

<div class="grid cards" markdown>

-   :material-rocket-launch: **Getting Started**

    Install, launch, and connect SillyTavern.

    [:arrow_right: Getting Started](../getting-started.md)

-   :material-lan: **Network & API**

    Ports, LAN access, and API key auth.

    [:arrow_right: Network & API](../features/network-api.md)

-   :material-console: **Console & Logging**

    Capture logs and share them safely.

    [:arrow_right: Console & Logging](../features/console-logging.md)

-   :material-api: **API Behavior**

    Request flow, streaming, cancellation, and queueing.

    [:arrow_right: API Behavior](../advanced/api-behavior.md)

</div>

---

## :material-arrow-left: Back to Home

[:material-arrow-left: Home](../index.md)
