---
icon: material/home
---

# Welcome to IntenseRP Next v2

<p align="center">
  <img
    src="assets/logo-strip.png"
    alt="IntenseRP Next v2 logo strip"
    style="max-width: 900px; width: 100%; height: auto;"
  />
</p>

**IntenseRP Next v2** is a local desktop app that lets OpenAI-compatible clients, like SillyTavern, talk to supported provider web chats through a real browser.

In plain terms, you log into a provider website, IntenseRP automates that page, and your client talks to IntenseRP through a local `/v1` API as usual. It's fragile by nature, because provider web UIs were not built to be APIs, but v2 was built to make that flow steadier and easier to live with.

!!! info "Active Community Fork"
    The original repository was archived by LyubomirT. This fork (`Lemomation/irn`) is actively maintained by Lemomation with ongoing model additions, provider fixes, and feature updates.

<div class="grid cards" markdown>

-   :material-rocket-launch: **Getting Started**

    Install the app, pick a provider, and connect SillyTavern.

    [:arrow_right: Get Started](getting-started.md)

-   :material-transfer: **Migration Guide**

    Coming from **IntenseRP Next v1** or **IntenseRP API**? Start here before copying old settings over.

    [:arrow_right: Migrate to v2](migration.md)

-   :material-cloud: **Providers**

    Find the knobs, caveats, and model controls for each supported provider.

    [:arrow_right: Browse Providers](providers.md)

-   :material-bug: **Troubleshooting**

    Browser won't open, login is stuck, or SillyTavern can't connect? Use the checklist.

    [:arrow_right: Fix a Problem](hands/troubleshooting.md)

</div>

## Where to Start

If this is your first install, go straight to :material-rocket-launch: [Getting Started](getting-started.md). It keeps things simple and links out when something gets provider-specific.

If you're upgrading from v1 or the older API project, read the :material-transfer: [Migration Guide](migration.md) first. The formatting system changed enough that some of your old settings might not work right away, and the guide can help you figure out what to change.

If you already have the app running and want to tune behavior, the most useful sections are :material-cloud: [Providers](providers.md), :material-format-text: [Formatting](features/formatting.md), and :material-lan: [Network & API](features/network-api.md).

## What Changed in v2?

v2 is a full rewrite, in simple words! The important part for users is not the rewrite itself, though. The upgrade is more about what the rewrite allows:

- A native desktop UI instead of the older customtkinter setup. (much more pretty, extensible, and faster to load)
- Playwright/Patchright browser control with network interception, instead of relying on fragile HTML scraping.
- Better provider-specific settings, account handling, logs, and troubleshooting tools.
- A cleaner OpenAI-compatible API for clients like SillyTavern.
- Support for a lot more providers than just DeepSeek, with more on the way.

!!! note "Community Maintenance"
    Provider websites can change without warning. Check the [GitHub repository](https://github.com/Lemomation/irn), the [News page](news.md), or the [Troubleshooting checklist](hands/troubleshooting.md) for updates and release notes.

## Community & Project Status

Need project context, old troubleshooting notes, or community links?

[Contact The Dev](hands/contact.md){ .md-button }
[Project Status](hands/support.md){ .md-button }
