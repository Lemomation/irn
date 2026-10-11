# Developer Handoff & Project Context

> **Date**: October 11, 2026  
> **Repository**: IntenseRP Next (`irn`)  
> **Local Workspace**: `C:\Users\gulza\Desktop\Apps\I-R-P`  
> **Active Branch**: `v2-rewrite`  

---

## 1. Repository & Remote Setup

- **`origin`**: `https://github.com/Lemomation/irn.git` (User's personal repository / fork)
- **`upstream`**: `https://github.com/Phobeuscz/irn.git` (Original upstream maintainer repository)

### ⚠️ Critical Working Constraints
1. **Push ONLY to `origin` (`Lemomation/irn`) on branch `v2-rewrite`**:
   - Never push directly to `upstream`.
   - Do **NOT** open new PRs to `upstream` unless explicitly asked by the user.
2. **Release Workflow**:
   - The user runs packaged release builds created by GitHub Actions (`.github/workflows/release-pyinstaller-windows.yml`).
   - The workflow triggers when a release tag is pushed (currently tagged `v2.9.4`).
   - When updating the release, force-update tag `v2.9.4` on `origin` (`git tag -f v2.9.4 && git push origin v2.9.4 --force`).
3. **Upstream Inactivity & Fork Direction**:
   - The upstream maintainer (`Phobeuscz`) has been inactive for ~4 weeks (last commit September 17, 2026; issues are disabled).
   - Two PRs remain open without maintainer activity:
     - **PR #1** (`feat(aistudio): add Gemini 3.6/3.7/3.8 Flash support`): Open since Sep 20, 2026.
     - **PR #2** (`Fixed multiple models.`): Open since Oct 5, 2026, contains all combined improvements.
   - If upstream remains inactive, the user is considering updating `updater/` to point updater checks directly to `Lemomation/irn` releases to maintain the tool independently.

---

## 2. Core Architecture & Key Files

| File | Purpose |
|---|---|
| [`main.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/main.py) | Main PyQt GUI application, lifecycle, remote control server, and runtime model switching (`_apply_remote_model_switch`). |
| [`api.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/api.py) | OpenAI/Claude-compatible FastAPI proxy server (`/v1/chat/completions`, `/v1/models`). Handles request dispatching, token streaming, and reasoning effort. |
| [`config/schema.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/config/schema.py) | Full settings schema definition (`SCHEMA`), field types, default values, visibility rules, `deprecated_options`, and `PROVIDER_BEHAVIOR_GROUPS`. |
| [`config/migrator.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/config/migrator.py) | `SettingsMigrator.migrate(settings)` runs on app boot to auto-migrate old `config.json` keys, retired model strings, and loadout definitions. |
| [`utils/model_ids.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/utils/model_ids.py) | Universal Model Names (`intenserp-auto`, `-chat`, `-reasoner`), real-to-API model ID mapping, and provider-specific deprecated aliases (`GLM_DEPRECATED_MODEL_ALIASES`, `QWEN_DEPRECATED_MODEL_ALIASES`, `MIMO_DEPRECATED_MODEL_ALIASES`). |
| [`ui/windows/settings_window.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/ui/windows/settings_window.py) | Settings dialog UI. Manages dynamic dropdown filtering when `show_deprecated_models` is toggled. |
| [`ui/widgets/components.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/ui/widgets/components.py) | `StyledComboBox` (max 8 visible items, dark-theme scrollbar) and `DeprecatedOptionItemDelegate` (red `[Deprecated]` badge and disabled state for retired models). |
| [`mimo_driver.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/mimo_driver.py) | Xiaomi MiMo Studio driver (`DriverProvider.MIMO`). Automates `mimo.xiaomi.com`. |
| [`glm_driver.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/glm_driver.py) | Zhipu AI GLM Chat driver (`DriverProvider.GLM_CHAT`). Automates `chatglm.cn`. |
| [`qwen_driver.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/qwen_driver.py) | Alibaba Qwen driver (`DriverProvider.QWEN_LM`). Automates `qwenlm.ai`. |
| [`aistudio_driver.py`](file:///C:/Users/gulza/Desktop/Apps/I-R-P/aistudio_driver.py) | Google AI Studio driver (`DriverProvider.AI_STUDIO`). Automates `aistudio.google.com`. |

---

## 3. Recent Features & Changes Implemented

### A. Xiaomi MiMo 2.6 Series Update
- **New Active Models**: Added `MiMo-V2.6-Pro` (new default) and `MiMo-V2.6-Flash`.
- **Retired 2.5 Models**: `MiMo-V2.5-Pro` and `MiMo-V2.5` were removed from Xiaomi's website. They are marked in `deprecated_options`.
- **Automatic Fallback & Migration**:
  - `config/migrator.py` automatically migrates stored `MiMo-V2.5*` settings/loadouts to `MiMo-V2.6-Pro`.
  - `mimo_driver.py` has `MIMO_RETIRED_MODEL_FALLBACKS` ensuring any request targeting a 2.5 model safely falls back to `MiMo-V2.6-Pro` instead of failing.
  - `utils/model_ids.py` has `MIMO_DEPRECATED_MODEL_ALIASES` mapping `mimo-v2-5*` API requests to `MiMo-V2.6-Pro`.
- **Toggle Added**: `show_deprecated_models` added to `mimo_behavior` Core card.

### B. GLM Chat Thinking Levels & Model Pruning
- **Thinking Effort**: Added `Low`, `High`, and `Max` options for `deepthink_effort` setting.
  - GLM-5.3 and GLM-5.3-Flash support `Low`, `High`, `Max`.
  - GLM-5.2 supports `High`, `Max` (normalizes `Low` $\rightarrow$ `High`).
  - `visible_depends` ensures the dropdown appears for all 3 models when Deep Think is enabled.
  - `api.py` accepts `reasoning_effort: "low"` for GLM.
- **Model Pruning**: Active models pruned to `GLM-5.3-Flash`, `GLM-5.3`, `GLM-5.2`. Retired models (`GLM-5.1`, `GLM-5-Turbo`, `GLM-5V-Turbo`, `GLM-4.7`) are in `deprecated_options`.
- **Toggle Added**: `show_deprecated_models` added to `glm_behavior`.

### C. Qwen 3.8 Series & Model Pruning
- **Active Models**: Added `Qwen3.8-Max` and `Qwen3.8-Omni-Flash`. Pruned the active list to the 6 models currently available on the web UI (`Qwen3.8-Max`, `Qwen3.8-Omni-Flash`, `Qwen3.7-Plus`, `Qwen3.7-Max`, `Qwen3.6-Plus`, `Qwen3.5-Plus`).
- **Retired Models**: 20 older preview/legacy checkpoints moved to `deprecated_options`.
- **Toggle Added**: `show_deprecated_models` added to `qwen_behavior`.

### D. Reusable Deprecated Models & Dropdown UI System
- **"Show Retired Models" Toggle (`show_deprecated_models`)**:
  - Exists in `glm_behavior`, `qwen_behavior`, and `mimo_behavior`.
  - When **OFF** (default): Deprecated models are hidden from the dropdown to keep the UI clean.
  - When **ON**: Retired models appear with a dark-red badge labeled **`[Deprecated]`** (tooltip: `"Model has been retired"`) and are disabled from selection (`Qt.ItemIsEnabled` stripped).
  - **Preservation Safety**: If a user already had a retired model saved in their configuration, it remains visible in the dropdown even when the toggle is OFF, preventing silent option disappearance.
- **Dropdown Height Constraint**:
  - `StyledComboBox` limits max visible items to 8 (`setMaxVisibleItems(8)`) with a custom vertical scrollbar, preventing large model lists from spanning across the entire screen.

---

## 4. Verification & Testing Instructions

Because Playwright/Patchright dependencies run in the packaging environment, test Python code changes locally using:

1. **Syntax / Compilation Check**:
   ```powershell
   python -m py_compile config/schema.py config/migrator.py mimo_driver.py glm_driver.py qwen_driver.py utils/model_ids.py main.py ui/windows/settings_window.py ui/widgets/components.py
   ```

2. **Unit / Logic Verification**:
   When testing drivers without `patchright` installed in the current shell, mock `patchright` before importing:
   ```python
   import sys
   from unittest.mock import MagicMock
   sys.modules['patchright'] = MagicMock()
   sys.modules['patchright.async_api'] = MagicMock()
   ```

3. **Git Status & Branch Verification**:
   ```powershell
   git status
   git log -n 3 --oneline
   ```
   Ensure working tree is clean and `origin/v2-rewrite` is up to date.

---

## 5. Summary of Current State

- **Current Commit**: `dfe8888` on `v2-rewrite`.
- **Working Tree**: Clean.
- **Latest Tag**: `v2.9.4` (pushed to `origin`).
- **All tests**: Passing.
