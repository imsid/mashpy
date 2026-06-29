---
name: workflow:pilot-changelog:v1
description: Generate a changelog from recent git commits in the mashpy repository.
---

# Pilot Changelog Workflow

Read the workflow request JSON. Use `workflow_input.commit_count` as the number
of recent commits to fetch.

Follow these steps in order:

1. Call `mcp_github_list_commits` with `owner: "imsid"`, `repo: "mashpy"`,
   and `per_page` set to `workflow_input.commit_count` to get recent commits.
2. For each commit that needs more detail, call `mcp_github_get_commit` with
   `owner: "imsid"`, `repo: "mashpy"`, and the commit `sha` to inspect its changes.
3. Produce a concise user-facing changelog from the commit data.

Return structured output with these fields:

- `title`: `"Changelog"`
- `commits_scanned`: number of commits processed
- `items`: array of concise changelog item strings
- `markdown`: Markdown changelog content for the period covered
