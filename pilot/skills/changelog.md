---
name: workflow:pilot-changelog:v1
description: Generate a changelog from recent git commits.
---

# Pilot Changelog Workflow

Read the workflow request JSON. Use `workflow_input.commit_count` as the number
of recent commits to scan.

Use targeted git commands from the repo root:

1. `git log --oneline -n <commit_count>`
2. `git show --stat --summary <sha>` or focused `git show` reads for relevant commits

Produce a concise user-facing changelog with these fields:

- `title`: usually `"Changelog"`
- `commits_scanned`: number of commits scanned
- `items`: concise changelog item strings
- `markdown`: Markdown changelog content
