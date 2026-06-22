# SKILL: triage

Go from an informal bug report or feature request to a filed issue and open PR.

---

## Phase 1 — Ground the request

Before drafting anything, read the code.

- Locate the relevant files by searching for the symbols, event types, or subsystems in the report.
- Trace the execution path end-to-end: where does the data originate, where is it transformed, where does it surface?
- Identify the root cause, not the symptom.

If the report is ambiguous after grounding, ask the user a focused clarifying question before drafting.

---

## Phase 2 — Draft, present, and file the issue

Draft an issue that includes:

- **Root cause** — the file(s) and line(s) where the bug lives or the feature is missing, and the mechanism
- **Affected code** — specific paths involved
- **Fix direction** — one or two sentences on what the correct fix looks like (not a diff)
- **Label** — `bug` for defects, `enhancement` for new behavior

Present the draft to the user. Wait for explicit approval before filing.

```bash
gh issue create \
  --title "<concise root cause>" \
  --body "..." \
  --label "bug"   # or "enhancement"
```

---

## Phase 3 — Implement and push

Wait for the user to say go ("approved", "go ahead", etc.), then implement.

Follow `docs/posts/releasing.md` for branch naming, commit format, and PR title conventions. Do not edit `CHANGELOG.md` or bump the version — both are automated.

Run tests before pushing:

```bash
uv run --extra dev pytest -q tests/mash
```

Then push and open the PR:

```bash
git push origin <branch>

gh pr create \
  --title "<Conventional Commit title>" \
  --base main \
  --body "$(cat <<'EOF'
Fixes #<issue-number>

## Summary

- <one bullet per meaningful change>

## Changes

- <file or function> — <what changed and why>

## Test plan

- [ ] `uv run --extra dev pytest -q tests/mash` passes
- [ ] Tests updated or added for changed behavior
- [ ] Docs / READMEs updated if behavior changed

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
