# vinput-registry Agent Guide

Guidelines, dual-planning model, and hard constraints for AI coding agents working on `vinput-registry`.

---

## 1. Project Overview & Architecture

`vinput-registry` is the resource catalog repository for the `vinput` voice input ecosystem. It serves as the single source of truth for:

- `registry/providers.json`: Cloud ASR provider definitions (streaming and batch).
- `registry/models.json`: Local ASR model metadata and `vinput_model` configurations (sherpa-onnx runtime).
- `registry/adapters.json`: Managed local LLM scene adapter bridge definitions.
- `resources/providers/<group>/<name>/`: Standalone Python 3 scripts (`entry.py` + `README.md`).
- `resources/adapters/<group>/<name>/`: Standalone Python 3 adapter scripts (`entry.py` + `README.md`).
- `i18n/`: Multilingual descriptions (`en_US.json`, `zh_CN.json`).
- `scripts/`: Maintenance tools, upstream monitor (`check_upstream_models.py`), and registry validator (`validate_registry.py`).

---

## 2. Related Repositories & Ecosystem

`vinput-registry` works in tandem with related ecosystem repositories:

| Repository | GitHub URL | Local Path | Role & Purpose |
| :--- | :--- | :--- | :--- |
| **`vinput-registry`** (Here) | [xifan2333/vinput-registry](https://github.com/xifan2333/vinput-registry) | `.` (`~/Code/vinput-registry`) | Resource catalog: index for local ASR models, cloud ASR providers, and LLM scene adapters. |
| **`fcitx5-vinput`** (Core) | [xifan2333/fcitx5-vinput](https://github.com/xifan2333/fcitx5-vinput) | `~/Code/fcitx5-vinput` | Main C++20 repository: Fcitx5 addon, background daemon, CLI, PipeWire capture, local sherpa-onnx runtime, D-Bus service. |
| **`aur-auto`** | [xifan2333/aur-auto](https://github.com/xifan2333/aur-auto) | `~/Code/aur-auto` | Arch User Repository (AUR) automation (`fcitx5-vinput-bin`). |
| **`flatpak-auto`** | [xifan2333/flatpak-auto](https://github.com/xifan2333/flatpak-auto) | `~/Code/flatpak-auto` | Flatpak OSTree repository & flatpakref publishing. |

For detailed operational guides, protocol specifications, schema formats, and PR workflows, refer to the unified skill at `.agents/skills/vinput-registry-dev/SKILL.md`.

---

## 3. Dual-Planning Model for AI Agents

To avoid ambiguity between functional task planning and toolchain validation, agents must distinguish between two distinct planning phases:

| Phase | Concept & Terminology | Timing | Tool & Output | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Phase A** | **Task Planning**<br>*(Feature / Bugfix Breakdown)* | **Pre-development**<br>*(Before coding)* | GitHub Issue & Draft PR body (`- [ ]` checklist) | Defines *what* code to write, module boundaries, and task sequencing. |
| **Phase B** | **Quality Gate Pre-check**<br>*(hk --plan)* | **Post-edit**<br>*(Before committing)* | `mise run check:plan`<br>*(or `hk run check --plan`)* | Previews *which* linters/formatters will run and their side-effects on edited files. |

---

## 4. Strict Chronological Development Workflow (Issue + Draft PR)

All development follows a strict 5-stage lifecycle:

```
[1. Pre-Code Initialization] -> [2. Single-Item Dev] -> [3. Local Quality Gate] -> [4. Local Atomic Commit] -> [5. Push & Merge]
```

1. **Pre-Code Initialization**:
   - Inspect or create an issue (`gh issue view <id>` or `gh issue create`).
   - Create a branch: `git checkout -b <type>/issue-<id>-<name>`.
   - Push an empty commit: `git commit --allow-empty -m "chore: initialize draft pr for #<id>" && git push -u origin <branch>`.
   - Open a Draft PR with **all checkboxes unchecked** (`gh pr create --draft`).
2. **Single-Item Focused Loop**:
   - Implement only one unchecked item at a time.
   - Run quality gate: `mise run check:plan`, `mise run check:changed`, `mise run fix`.
   - Create an atomic commit locally.
   - Update the PR body checklist (`gh pr edit --body ...`).
3. **Unified Push, Checks & Merge**:
   - Push all commits to GitHub.
   - Verify CI status (`gh pr checks`).
   - Mark as ready (`gh pr ready`) and merge (`gh pr merge --squash --delete-branch`).

---

## 5. Hard Engineering Rules

1. **Zero External Dependencies**:
   - Provider and adapter scripts (`entry.py`) must rely solely on the **Python 3 standard library**. No pip packages may be imported.
2. **Host Python Isolation**:
   - Never use `mise` or `uv` to manage an isolated Python version for this repository. All commands execute using the system Python 3 (`/usr/bin/python3`) to avoid system environment pollution.
3. **Minimally Viable Env Set**:
   - In `registry/adapters.json` (and `providers.json`), declare only the bare minimum environment variables necessary to run the resource.
   - When a resource is installed, `fcitx5-vinput` writes all declared envs into `~/.config/vinput/config.json`. Over-declaring optional tuneables with robust defaults pollutes the user's configuration.
4. **Mirror Fallbacks**:
   - Every `script_urls` array must provide 3 fallback mirrors in order: GitHub raw, `gh-proxy.com`, and `ghfast.top`.
5. **i18n Parity**:
   - Every new resource requires both `<id>.title` and `<id>.description` in both `i18n/en_US.json` and `i18n/zh_CN.json`.
6. **Adapter Stderr Silence**:
   - `vinput-daemon` translates every non-empty line of an adapter's `stderr` directly into a user desktop notification (`EmitNotification`). Adapters must be 100% silent on `stderr` during normal operation (no startup banners, options, or HTTP 200 logs); only fatal errors or 4xx/5xx responses may write to `stderr`.
7. **Explicit Adapter Ports**:
   - Adapters must require an explicit port environment variable declared with `"required": true` in `registry/adapters.json`. Never fall back to implicit internal default ports when the env is missing or empty, ensuring users know exactly which port to configure in their LLM provider `base_url`.
