---
name: vinput-registry-dev
description: Complete development, resource indexing, cloud ASR provider protocols, local model specifications, LLM adapter guidelines, quality gate, and Issue+PR workflow for vinput-registry. Use when adding or modifying cloud ASR providers (streaming or batch), local ASR models (sherpa-onnx, vinput_model), LLM scene adapters, updating registry indexes (providers.json, models.json, adapters.json), maintaining i18n catalogs (en_US.json, zh_CN.json), running pre-PR clean-up inspections (代码体检) via hk and mise, or following the Issue+Draft PR lifecycle in vinput-registry. Trigger also on Chinese requests such as 开发 vinput 资源、添加语音识别提供商、新增流式/批处理 ASR provider、添加模型、配置 vinput_model、添加场景适配器/LLM adapter、修改 registry/providers.json、修改 i18n 双语、代码体检与格式化自检、提 PR/开 PR.
---

# vinput-registry Unified Development Skill

This skill is the master operational manual for developing, contributing, and maintaining resources (Cloud ASR Providers, Local ASR Models, and LLM Scene Adapters) in `vinput-registry`.

---

## 1. Ecosystem Routing & Positioning

Verify repository roles before making changes:

| Repository | GitHub URL | Local Path | Role & Purpose |
| :--- | :--- | :--- | :--- |
| **`vinput-registry`** (Here) | [xifan2333/vinput-registry](https://github.com/xifan2333/vinput-registry) | `.` (`~/Code/vinput-registry`) | Resource catalog: index for local ASR models (`models.json`), cloud ASR provider scripts (`providers.json` + `resources/providers/`), and LLM scene adapters (`adapters.json` + `resources/adapters/`). |
| **`fcitx5-vinput`** (Core) | [xifan2333/fcitx5-vinput](https://github.com/xifan2333/fcitx5-vinput) | `~/Code/fcitx5-vinput` | Main C++20 repository: Fcitx5 addon, background daemon, CLI, PipeWire capture, local sherpa-onnx runtime, D-Bus service. |
| **`aur-auto`** | [xifan2333/aur-auto](https://github.com/xifan2333/aur-auto) | `~/Code/aur-auto` | Arch Linux AUR packaging automation (`fcitx5-vinput-bin`). |
| **`flatpak-auto`** | [xifan2333/flatpak-auto](https://github.com/xifan2333/flatpak-auto) | `~/Code/flatpak-auto` | Flatpak OSTree repository & flatpakref publishing. |

---

## 2. Core Repository Layout

- `registry/`: Master resource catalogs:
  - `providers.json`: Cloud ASR provider definitions (Streaming and Batch).
  - `models.json`: Local ASR model catalog with `vinput_model` runtime specs.
  - `adapters.json`: Managed local LLM scene adapter bridge scripts.
- `resources/providers/<group>/<name>/`: Standalone Python 3 scripts (`entry.py` + `README.md`).
- `resources/adapters/<group>/<name>/`: Standalone Python 3 adapter scripts (`entry.py` + `README.md`).
- `i18n/`: Multilingual display text (`en_US.json`, `zh_CN.json`).
- `scripts/`: Maintenance and validation utilities (`validate_registry.py`, `check_upstream_models.py`).

---

## 3. Hard Engineering Rules

1. **Zero External Dependencies**: All provider and adapter scripts (`entry.py`) must use the **Python 3 standard library only** (e.g. `urllib.request`, `socket`, `ssl`, `json`, `hashlib`, `struct`). Never import third-party packages (no `requests`, `websockets`, `aiohttp`).
2. **Resource ID Structure**: Stable machine IDs follow `<kind>.<folder>.<name>`:
   - Providers: `provider.<folder>.<name>` (streaming providers **must** end with `.streaming`).
   - Models: `model.<backend>.<name>` (e.g., `model.sherpa-onnx.<name>`).
   - Adapters: `adapter.<folder>.<name>`.
   - `short_id` is required for CLI/GUI display, but never used for internal resolution.
3. **Environment Variable Rules**:
   - Cloud ASR Providers must use the `VINPUT_ASR_*` namespace.
   - LLM Adapters have naming freedom (can use target-specific names like `MTRAN_URL`, `OLLAMA_HOST`), but must follow the **Minimally Viable Env Set** rule.
4. **Mirror Fallback URLs**: `script_urls` must provide 3 fallback mirrors in order:
   - `https://raw.githubusercontent.com/xifan2333/vinput-registry/main/...`
   - `https://gh-proxy.com/https://raw.githubusercontent.com/xifan2333/vinput-registry/main/...`
   - `https://ghfast.top/https://raw.githubusercontent.com/xifan2333/vinput-registry/main/...`
5. **i18n Parity**: Any new or updated resource must contain both `<id>.title` and `<id>.description` in both `i18n/en_US.json` and `i18n/zh_CN.json`.
6. **Adapter Stderr Silence (Desktop Notification Coupling)**: `vinput-daemon` reads an adapter's `stderr` line-by-line and translates every non-empty line directly into a D-Bus desktop error notification (`EmitNotification`). Adapters must **never print operational logs, startup banners, options, or HTTP 200 access logs to `stderr`**. Normal operation must be 100% silent; only fatal startup failures or HTTP 4xx/5xx errors may be emitted to `stderr`.
7. **Explicit Adapter Port Requirement (No Implicit Defaults)**: Adapters must require an explicit, namespaced port environment variable (e.g., `TEXT_CLEANER_PORT`, `MTRAN_PORT`) declared with `"required": true` in `registry/adapters.json`. Scripts must never silently fall back to an internal hardcoded default port when the env variable is missing or empty, so users always know exactly which port to set in their matching LLM provider `base_url`.

---

## 4. Universal Code Quality Gate (`mise` / `hk`)

Always run verification before committing or reporting completion:

```bash
# 1. Preview which linter/checker steps match edited files
mise run check:plan

# 2. Check changed files safely
mise run check:changed

# 3. Automatically fix code formatting & trailing whitespace
mise run fix

# 4. Run linter and full registry integrity verification
mise run lint
```

---

## 5. Progressive Reference Guides (Read as Needed)

Follow progressive disclosure: consult specific reference files depending on your current task:

### Task: Writing or Debugging a Cloud ASR Provider
Read **[references/provider-protocol.md](references/provider-protocol.md)**
- Streaming duplex JSONL protocol (stdin `audio`, `finish`, `cancel` vs stdout `session_started`, `partial`, `final`, `error`, `closed`).
- Batch raw PCM stdin to plain text stdout protocol.
- Standard exit codes (`0` success, `1` runtime error, `2` usage/config error).
- Audio framing (16000Hz, mono, S16_LE), start buffering, fallback final, and finish grace period.

### Task: Registering Models, Providers, or Adapters
Read **[references/resource-schema.md](references/resource-schema.md)**
- `registry/*.json` schemas and mandatory fields.
- Local model `vinput_model` mapping to `sherpa-onnx` C API (family, backend, recognizer, model, tokens/tokenizer, hotwords).
- LLM Adapter environment variable freedom and the **Minimally Viable Env Set** rule (keeping user `config.json` clean).
- `README.md` documentation template (Required vs Optional envs).

### Task: Code Health Check & Linter Execution
Read **[references/quality-gate.md](references/quality-gate.md)**
- Dual-planning model: Task Planning vs Quality Gate Pre-check (`hk --plan`).
- `hk.pkl` rules and step mappings.
- Clean execution without polluting system Python.

### Task: Implementing an Issue / Feature / PR
Read **[references/issue-pr-workflow.md](references/issue-pr-workflow.md)**
- The 5-step Issue + Draft PR lifecycle (`gh pr create --draft`).
- Single-item local atomic commits, updating PR checklist checkboxes (`- [x]`).
- Finalizing, marking ready (`gh pr ready`), and squash merging.
