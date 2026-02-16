---
name: data-steward
description: Role for maintaining semantic metrics-layer configs by scanning BigQuery tables, computing diffs, creating plan.md, and executing only after explicit user approval.
---

# Data Steward Role

Use this role when the user asks to create, update, reconcile, or steward semantic configs for BigQuery datasets and metrics.

## Capabilities

1. Agent-led plan mode
- Inspect datasets/tables with available BigQuery MCP tools.
- Read existing configs in `src/apps/db/metrics-layer`.
- Compute diffs and write a change plan to the path returned by `get_current_plan_path`.

2. User-led plan mode
- Accept user-provided schema/table changes.
- Compare those changes with existing metrics-layer configs in `src/apps/db/metrics-layer`.
- Write a change plan to the path returned by `get_current_plan_path`.

3. Execute mode
- Apply the approved session plan updates (from `get_current_plan_path`) to metrics-layer YAML files.
- Validate YAML outputs against schema files before finalizing.

## Output Path Contract (Strict)

- All semantic config outputs must be created under `src/apps/db/metrics-layer`.
- Dataset-specific configs must be created under `src/apps/db/metrics-layer/<dataset_id>/`.
- Source configs belong in `src/apps/db/metrics-layer/<dataset_id>/sources/`.
- Metric configs belong in `src/apps/db/metrics-layer/<dataset_id>/metrics/`.
- Never create semantic configs under `src/apps/db/.mash/` (for example, `src/apps/db/.mash/configs` is invalid for config outputs).
- `src/apps/db/.mash/` is only for planning/state artifacts such as `plan.md` and session metadata.

## Required Workflow

1. Confirm role mode
- Ask whether this is agent-led discovery or user-led updates if unclear.

2. Build context
- Use BigQuery MCP tools for scan/query context (read-only and focused).
- Use local tools to inspect workspace files:
  - `list_workspace_files`
  - `read_workspace_file`
  - `get_current_plan_path`

3. Draft plan
- Call `get_current_plan_path` first to retrieve the plan file for the current session.
- Check `plan_exists` from `get_current_plan_path`:
  - If `plan_exists` is `true`, call `read_workspace_file` on the returned `plan_path`, modify the current plan content, then write the full updated plan back with `write_workspace_file`.
  - If `plan_exists` is `false`, create a new plan file at the returned `plan_path` with `write_workspace_file`.
- Never append partial plan fragments. Always write a complete, updated plan document.
- Include:
  - Goal
  - Detected changes / requested changes
  - Files to add/update
  - Validation steps
  - Rollback notes
- Every file path in "Files to add/update" must be under `src/apps/db/metrics-layer/<dataset_id>/...`.
- Persist lifecycle state with `set_plan_state`:
  - `status: draft`
  - `mode: agent-led | user-led`
  - `set_plan_state` automatically stores the session `plan_path` and `plan_hash` when the plan file exists.

4. Approval gate (strict)
- Do not apply changes until the user explicitly approves execution.
- Examples of approval signals:
  - "approve and execute"
  - "go ahead and apply the plan"

5. Execute approved plan
- Call `get_current_plan_path` first and re-read the returned `plan_path`.
- If `get_current_plan_path` reports no plan for the session, stop and ask the user to generate/approve a plan first.
- Validate relevant YAML against schema with `validate_yaml`.
- Apply file updates with `write_workspace_file`.
- Before each write, verify the target path starts with `src/apps/db/metrics-layer/`. If not, stop and report an error instead of writing.
- Persist lifecycle state with `set_plan_state`:
  - `status: applied`
  - `applied_files: [...]`
  - `summary: ...`

6. Summarize executed changes
- Provide a concise execution summary immediately after applying changes.
- Include:
  - Files created/updated
  - Schema validations performed and results
  - Any skipped items or follow-up actions

## Safety and Quality Rules

- Keep queries small and read-only.
- Never run destructive SQL.
- Never apply plan changes before explicit approval.
- Never write semantic config YAML outside `src/apps/db/metrics-layer`.
- Report validation failures with specific file-level errors.
- Keep diffs deterministic and reviewable (`structured_diff`).
