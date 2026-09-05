# Issue + PR Driven Development Workflow (SOP)

Development in `vinput-registry` must follow a strict, chronological **Pre-Code Draft PR -> Single-Item Loop -> Merge** process.

---

## 1. Chronological Lifecycle

```
+------------------------------------------------------------------------+
| 1. Pre-Code Initialization (MANDATORY BEFORE ANY CODE IS WRITTEN)       |
|    gh issue view <id> (or gh issue create)                             |
|    git checkout -b <type>/issue-<id>-<name>                            |
|    git commit --allow-empty -m "chore: initialize draft pr for #<id>"   |
|    git push -u origin <type>/issue-<id>-<name>                         |
|    gh pr create --draft (ALL tasks unchecked: - [ ])                   |
+-----------------------------------+------------------------------------+
                                    |
                +-------------------v-------------------+
                | 2. Single-Item Focused Development    |
                |    Only implement the first - [ ]     |
                +-------------------+-------------------+
                                    |
                +-------------------v-------------------+
                | 3. Local Quality Gate & Pre-check     |
                |    mise run check:plan (preview steps)|
                |    mise run check:changed             |
                |    mise run fix (if needed)           |
                +-------------------+-------------------+
                                    |
                +-------------------v-------------------+
                | 4. Local Atomic Commit                |
                |    git add <files>                    |
                |    git commit -m "feat(scope): ..."   |
                |    (Keep commit local)                |
                |    gh pr edit --body (check task [x]) |
                +-------------------+-------------------+
                                    | (Remaining tasks?)
                                    +-------- Yes -------+
                                    | No                 |
+-----------------------------------v-------------------+|
| 5. Unified Push, Checks & Merge                       ||
|    git push origin <branch>                           ||
|    gh pr checks (verify PR CI passed)                 ||
|    gh pr ready (mark as ready for review)             ||
|    gh pr merge --squash --delete-branch               ||
+-------------------------------------------------------+|
                                    ^                    |
                                    +--------------------+
```

---

## 2. Detailed Execution Steps

### Phase 1: Pre-Code Initialization
```bash
# 1. Create or inspect issue
gh issue view <issue_id>

# 2. Create feature branch
git checkout -b feat/issue-<id>-<short-description>

# 3. Initialize branch with empty commit and push
git commit --allow-empty -m "chore: initialize draft pr for issue #<issue_id>"
git push -u origin feat/issue-<id>-<short-description>

# 4. Open Draft PR with ALL tasks UNCHECKED (- [ ])
gh pr create --draft \
  --title "<type>: <concise description> (#<issue_id>)" \
  --body "Closes #<issue_id>

### Implementation Tasks
- [ ] 1. Core script in resources/.../entry.py
- [ ] 2. Registry entry & fallback URLs in registry/providers.json
- [ ] 3. Update i18n catalogs (en_US and zh_CN)
- [ ] 4. Run quality gate verification"
```

### Phase 2: Single-Item Execution Loop
For each unchecked `- [ ]` task in order:
1. **Code ONLY Task N**: Pick ONLY the topmost unchecked `- [ ]` item.
2. **Quality Gate**:
   ```bash
   mise run check:plan
   mise run check:changed
   mise run fix # if formatting was modified
   ```
3. **Local Atomic Commit**:
   ```bash
   git add <modified-files>
   git commit -m "<type>(<scope>): <concise message>"
   ```
4. **Update PR Body**: Check off the completed task item (`- [x]`) using `gh pr edit --body "..."`.

### Phase 3: Final Verification & Merge
1. **Push commits**:
   ```bash
   git push origin <branch>
   ```
2. **Verify CI checks**:
   ```bash
   gh pr checks
   ```
3. **Mark ready for review & Merge**:
   ```bash
   gh pr ready
   gh pr merge --squash --delete-branch
   ```
