# vinput-registry Development Reference

Single source of truth for AI assistants working in `vinput-registry`.

---

## 1. Quick Commands

```bash
# Quality Gate (hk + mise)
mise run check:plan         # Preview steps matching edited files
mise run check:changed      # Check modified & staged files
mise run fix                # Automatically format files & clean whitespace
mise run lint               # Run ruff check, json validation & registry integrity
mise run validate:registry  # Validate registry metadata, mirrors & i18n parity

# Git Hooks
mise run hooks-install      # Install hk git hooks into .git/hooks/
```

---

## 2. Hard Constraints

- **Python 3 Standard Library Only**: `resources/**/entry.py` must never import third-party packages.
- **System Python Isolation**: All tasks use host `/usr/bin/python3`. Never install isolated Python via `uv` or `mise`.
- **Minimally Viable Env Set**: Keep `envs` in `registry/*.json` limited strictly to the required minimum. Do not pollute user configs with optional tuneables that have internal defaults.
- **ID & Mirror Standards**: IDs follow `<kind>.<folder>.<name>` (`.streaming` suffix required for streaming providers). `script_urls` must include GitHub raw, `gh-proxy.com`, and `ghfast.top`.
- **i18n Parity**: All items require title and description in both `en_US.json` and `zh_CN.json`.

---

## 3. Workflow SOP

Follow strict Issue + Draft PR development:
1. `gh issue view <id>` -> `git checkout -b <type>/issue-<id>-<name>`
2. Empty commit -> push -> `gh pr create --draft` with `- [ ]` checklist
3. Implement item -> `mise run check:plan` -> `mise run check:changed` -> atomic commit -> update PR checklist
4. Push -> `gh pr checks` -> `gh pr ready` -> `gh pr merge --squash --delete-branch`

Detailed guides: `.agents/skills/vinput-registry-dev/SKILL.md`
