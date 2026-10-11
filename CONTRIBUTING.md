# Contributing to IRN (IntenseRP Next)

Thanks for helping out!

This project moves fast and provider web interfaces change often, so contributions from the community are warmly welcomed.

> **💡 Philosophy:** Contributions are encouraged! Just make sure you know what Playwright is, and verify that everything works. Vibe coding is completely accepted as long as it runs cleanly without bugs!

---

## 📅 Maintenance & Model Update Schedule

- **Driver & Model Updates**: If a new model drops or a provider tweaks their web interface, support will usually be added over the **weekend**. Please be patient!
- **Bug Fixes**: Critical driver breakages or crashes are prioritized when reproducible reports come in.

---

## 🐛 Bug Reports

If something breaks, please open a GitHub Issue:  
👉 **https://github.com/Lemomation/irn/issues**

When reporting a bug, please include:
1. **App Version**: From `version.json` or the app title bar (e.g., `v2.9.4`).
2. **Provider & Model**: (e.g., Google AI Studio `gemini-2.5-flash`, Xiaomi MiMo `MiMo-V2.6-Pro`, etc.).
3. **OS**: Windows or Linux.
4. **Steps to Reproduce**: What you clicked or sent right before it failed.
5. **Logs / Error Message**: Relevant error text from the UI console or terminal (please redact any personal info or session cookies).

---

## 🛠️ Dev Setup (Running from Source)

**Requirements**: Python 3.12+ (3.12 or 3.13 recommended)

```bash
# Clone this repository
git clone https://github.com/Lemomation/irn.git
cd irn

# Create and activate virtual environment
python -m venv venv

# Linux / Mac:
source venv/bin/activate
# Windows (PowerShell / Command Prompt):
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run IntenseRP Next
python main.py
```

---

## 🚀 Pull Requests & Guidelines

- **Keep PRs focused**: Small, single-purpose PRs (e.g. updating selectors for a specific provider driver or adding a new model to dropdowns) are easiest to review and merge quickly.
- **Match existing driver patterns**: Check existing drivers (`aistudio_driver.py`, `glm_driver.py`, `mimo_driver.py`, `qwen_driver.py`) for reference on how streaming, cookies, and selectors are managed.
- **Test locally**: Run the app and test at least one end-to-end prompt through SillyTavern or the dry-run console to confirm completion and streaming work as expected.
- **Keep it clean**: Avoid committing temporary artifacts, caches, session folders, or personal configs (check `.gitignore`).

---

## 💬 Getting in Touch

For bugs, ideas, and model requests, use **[GitHub Issues](https://github.com/Lemomation/irn/issues)** and **[Discussions](https://github.com/Lemomation/irn/discussions)**.
