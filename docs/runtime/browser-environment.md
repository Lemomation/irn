---
icon: material/earth
---

# :material-earth: Browser Environment

Browser Environment controls the launch-time hints IntenseRP passes to provider pages.

:material-arrow-right: **Settings** -> **Browser & Runtime** -> **Browser Environment**

The settings here cover a few launch-time browser hints:

- **Preferred Browser Locale** defaults to **English (en-US)**. It affects browser language signals such as `navigator.language`, request language headers, and locale-sensitive formatting.
- **Browser Timezone** defaults to **System Default**. The built-in override is **New York (`America/New_York`)**.
- **Resize Viewport With Window** is off by default. It lets the provider page viewport follow the real browser window size instead of Playwright's fixed viewport.
- **Background Mode (Hidden Browser)** is off by default. On Linux it runs the provider browser on a private, invisible display, so your screen stays free.
- **Browser Proxy URL** is optional. It routes provider browser contexts through an HTTP, HTTPS, SOCKS4, or SOCKS5 proxy.

This is best-effort, not a magic cloak. Saved provider preferences, account settings, cookies, local storage, and IP geolocation can still matter.

!!! tip "Recommended default"
    Keep the locale on **English (en-US)** unless you have a specific reason not to. IntenseRP's provider drivers expect English UI text, so this tends to be the most predictable choice.

Timezone is more situational. Leave it on **System Default** unless you specifically need a browser page to report New York time.

---

## :material-fit-to-screen-outline: Resize Viewport With Window

:material-arrow-right: **Settings** -> **Browser & Runtime** -> **Browser Environment** -> **Resize Viewport With Window**

By default, Playwright/Patchright gives provider pages a fixed viewport. That keeps existing behavior predictable, but on some systems the real browser window can resize while the page viewport stays stuck at the original size. The result is the empty-space windows or clipped content you sometimes see when maximizing or resizing the provider browser window.

Turn this on if maximizing or resizing the provider browser window doesn't resize the actual page area. IntenseRP will launch the provider browser context with Playwright's fixed viewport disabled, so the page viewport follows the browser window instead.

!!! note "Restart required"
    This setting applies when the provider browser context is created. Stop and start the provider browser after changing it.

---

## :material-eye-off-outline: Background Mode

:material-arrow-right: **Settings** -> **Browser & Runtime** -> **Browser Environment** -> **Background Mode (Hidden Browser)**

Background Mode runs the provider browser where you can't see it, while provider pages still see the same browser as in normal mode. Your screen, mouse and keyboard stay yours, and it keeps working with the monitor off or the laptop lid closed.

It does **not** use Chromium's headless mode or a software virtual screen (Xvfb), because both change what pages can measure. Instead, IntenseRP starts a private, GPU-accelerated display (a headless [sway](https://swaywm.org/) compositor) and runs the same browser, with the same flags, inside it. What a page can observe about your desktop is mirrored from your real session:

- screen size, layout, scale and exact refresh rate
- GPU (the WebGL/canvas identity is checked before any provider page loads)
- input devices: pages see a mouse and a keyboard, with your keyboard layout
- X11 desktops: the work area (panels and taskbars), fonts, theme, DPI and font rendering, and where your window manager would place the browser window
- Wayland desktops: the browser runs as a native Wayland app, like in normal mode; theme and fonts come from your session settings as usual

On X11, the exact refresh rate needs a C compiler such as `gcc` once (IntenseRP builds and caches a tiny helper for the private display); without one it is matched to within a few hundredths of a Hz.

### Calibration

The first start on a desktop (and the first one in each viewport mode) runs **visibly**. IntenseRP records what a page can see of your browser: GPU, screen, scale, pointer and touch support, theme preferences, scrollbars, fonts, and how your desktop sizes and places the window. Every later start runs hidden only if it matches that recording exactly, checked on a blank page before any provider page loads. If anything differs, for example a new monitor, a different GPU, or a desktop IntenseRP cannot reproduce, that start opens normally and tells you why. A visible start also refreshes the recording. Your keyboard layout is copied and verified every time the private display starts.

### Requirements

- Linux with an X11 or Wayland desktop session
- `sway` and `Xwayland` installed: Debian/Ubuntu/Mint `sudo apt install sway xwayland`, Fedora `sudo dnf install sway xorg-x11-server-Xwayland`, Arch `sudo pacman -S sway xorg-xwayland`
- access to the GPU render node (`/dev/dri/renderD*`). Desktop logins usually have it. To use Background Mode without an active desktop session, add yourself to the `render` group: `sudo usermod -aG render $USER`

### Known limits

- **GNOME and fixed viewport:** GNOME maximizes browser windows that are almost as large as the screen, which the private display cannot reproduce. Calibration detects this; turning on **Resize Viewport With Window** lets the browser keep its own size so Background Mode can run. That setting also changes what sites see in normal mode (your real screen size instead of the fixed 1280x720 viewport), so consider whether that matters for accounts you already use.
- **Tiling window managers** (i3, bspwm, ...) size every window themselves; calibration detects this and keeps the browser visible.
- **Touchscreens** cannot be reproduced on the private display, so Background Mode stays off on touch devices.
- On Wayland desktops, a browser window that was last used **maximized** opens visibly; un-maximize it once.
- On Wayland desktops, windows a site opens itself (for example a Google sign-in popup during Auto Login) cannot be reproduced exactly. During sign-in IntenseRP reopens the browser visibly for that session; later popups are logged as a warning.
- On Wayland desktops with several keyboard layouts, the hidden browser starts on the first layout (on X11 it uses the one that is active).
- Windows and macOS are not supported yet.

!!! note "Signing in"
    When a provider needs you in the browser (Auto Login off, no saved account, CAPTCHA, extra confirmation), IntenseRP reopens the same browser profile in a visible window for that session. Your session is saved, so the next start runs in the background again.

!!! note "Monitor off, screen lock, no desktop"
    Once your desktop has been recorded with the monitor on, later starts work with the monitor off or the lid closed. If your screen locker switches to the login screen, a provider that is already running keeps going; starting one while locked, or from SSH or a service, may need the `render` group (see Requirements). Without GPU access IntenseRP refuses to run hidden rather than change your browser fingerprint.

!!! note "Restart required"
    Stop and start the provider browser after changing this setting.

---

## :material-lan-connect: Browser Proxy URL

:material-arrow-right: **Settings** -> **Browser & Runtime** -> **Browser Environment** -> **Browser Proxy URL**

This is a global provider-browser proxy. Leave it blank to connect directly.

Supported examples:

```text
http://127.0.0.1:8080
https://proxy.example:8443
socks4://127.0.0.1:1080
socks5://user:pass@127.0.0.1:1080
```

The scheme (`http`, `https`, `socks4`, or `socks5`) tells Playwright/Patchright which proxy type to use. Username and password can be included in the URL when your proxy requires authentication.

!!! note "Restart required"
    Proxy settings apply when a provider browser context is created. Stop and start the provider browser after changing this.

!!! tip "MiMo-specific proxy"
    Xiaomi MiMo also has its own **Provider Behavior -> Xiaomi MiMo -> Proxy** settings. Use that when only MiMo needs a supported-region proxy and you want other providers to connect normally.

---

## :material-arrow-left: Back to Browser & Runtime

[:material-arrow-left: Browser & Runtime Overview](../runtime.md)
