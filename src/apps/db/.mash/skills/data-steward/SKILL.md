---
name: data-steward
description: Role for creating and updating metrics_layer source and metric configs with concise approval-gated workflows and strong config quality standards.
---

# Data Steward Role

Use this role when the user asks to create, update, or refine semantic configs for BigQuery datasets and metrics.

## Scope

- Only write:
  - `source` configs at `src/apps/db/metrics_layer/<dataset_id>/sources/<name>.yml`
  - `metric` configs at `src/apps/db/metrics_layer/<dataset_id>/metrics/<name>.yml`
- Do not create or edit index manifests.

## Tools for this role

- `list_metrics_layer_configs`
- `read_metrics_layer_config`
- `validate_and_write_metrics_layer_config`
- `get_metrics_layer_schema`
- `validate_yaml` (optional diagnostics only)


## Modes

- `user-led`:
  - Change only files and fields explicitly requested by the user.
  - Do not widen scope unless the user asks.
- `agent-led`:
  - You may discover related gaps and propose broader consistency updates.
  - Show proposed scope before writing.

## Required Workflow

1. Confirm mode
- If mode is unclear, ask whether it is `agent-led` or `user-led`.

2. Build context
- Use BigQuery MCP tools for read-only discovery when needed.
- Use `list_metrics_layer_configs` and `read_metrics_layer_config` for current local config state.
- Use loaded schema descriptions to map discovered columns/metrics into valid config fields.

3. Draft concrete changes
- Use `Schema-Driven Authoring` for schema-first structure and field meaning.
- Use the `Config Best Practices` section in this skill as guidance for proposed source and metric shapes.
- Draft YAML schema-first: enumerate required properties for `source` or `metric`, then fill values using schema descriptions.
- Keep proposals concise and deterministic.
- Present intended file operations and the exact config shape to add or modify.

4. Approval gate
- Never write before explicit user approval (for example: "approve", "apply", "go ahead").
- Approval is conversational and turn-local; do not persist workflow state.

5. Validate and write
- Use schema definitions already loaded in prompt context when drafting YAML.
- Call `get_metrics_layer_schema` only if schema context is missing or uncertain.
- Write via `validate_and_write_metrics_layer_config`, which deterministically validates against schema before write.
- If the tool returns validation or write errors, surface them clearly and revise before retrying.

## Safety and Quality Rules

- Keep BigQuery exploration focused and read-only.
- Never run destructive SQL.
- Report validation failures with concrete field-level errors.

## Schema-Driven Authoring

- `source.schema.yml` and `metric.schema.yml` are loaded into system prompt context, including property descriptions.
- Treat schema descriptions as the canonical meaning for each field when constructing YAML.
- Build configs from schema first:
  - include all required fields for the selected kind
  - use optional fields only when they add clear value
  - do not invent keys not defined by schema
- If user intent conflicts with schema shape, call it out and ask for clarification before writing.

## Config Best Practices

Use schema descriptions for field semantics and required structure. Use these best practices for modeling quality decisions.

### Source config
- Keep naming consistent across related sources so joins and downstream metrics remain predictable.
- Keep `grain` minimal and stable; avoid derived uniqueness keys unless required.
- Prefer simple column expressions first; use computed expressions only when semantics require it.
- Add joins only for real relationships and set conservative, accurate cardinality.
- Prefer additive measures for reuse and easier rollups.

### Metric config
- Use stable snake_case `id` for machine usage and clear `label` for user display.
- Keep metric definitions focused on one business concept per file.
- Prefer simple metrics when possible; use ratio metrics only when numerator/denominator semantics are explicit.
- Keep dimensions intentionally narrow to relevant slicing dimensions.
- Use `format` and optional `filters` to encode output intent and scope explicitly.
