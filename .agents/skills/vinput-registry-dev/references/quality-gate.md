# Quality Gate & Code Health Check SOP

This document defines the quality gate standards and verification procedures for `vinput-registry`.

---

## 1. Dual-Planning Model

To avoid confusion between functional task planning and toolchain validation:

| Phase | Concept & Terminology | Timing | Tool & Output | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Phase A** | **Task Planning**<br>*(Feature / Bugfix Breakdown)* | **Pre-development**<br>*(Before coding)* | GitHub Issue & Draft PR body (`- [ ]` checklist) | Defines *what* to implement, schemas to modify, and task sequence. |
| **Phase B** | **Quality Gate Pre-check**<br>*(hk --plan)* | **Post-edit**<br>*(Before committing)* | `mise run check:plan`<br>*(or `hk run check --plan`)* | Previews *which* linters/formatters will run and their side-effects on edited files. |

---

## 2. Universal Verification Toolchain (`mise` + `hk`)

`vinput-registry` uses `hk` (driven by `hk.pkl`) and `mise` for local code health checks without polluting the host environment:

- **System Python Isolation**: Never use `mise` or `uv` to download/manage an isolated Python version for development tasks; all scripts run against the system Python 3 (`/usr/bin/python3`).
- **Ruff Linter**: Installed as a standalone rust binary via `mise.toml`.

### Quality Gate Commands

```bash
# 1. Preview which steps will run against modified files
mise run check:plan

# 2. Run checks on modified and untracked files safely
mise run check:changed

# 3. Automatically fix formatting and whitespace
mise run fix

# 4. Full linter and schema integrity check
mise run lint

# 5. Verify JSON syntax across registry and i18n
mise run validate:json

# 6. Deep registry schema, mirror URL, and i18n parity check
mise run validate:registry
```

---

## 3. Linters Configured in `hk.pkl`

1. **`ruff-check`**: Validates all Python scripts (`**/*.py`) with PEP8, pyupgrade, flake8-bugbear, and ruff rules.
2. **`ruff-format`**: Enforces strict code formatting (120 char line length).
3. **`json-validate`**: Verifies JSON syntax with Python's built-in `json.tool`.
4. **`registry-validate`**: Runs `scripts/validate_registry.py` to ensure resource metadata correctness.
5. **`detect-private-key`**: Scans files for accidental inclusion of private keys or real API credentials.
6. **`trailing-whitespace` & `newlines`**: Cleans up whitespace and ensures clean file endings.
7. **`check-merge-conflict`**: Prevents accidental commits of Git conflict markers.
