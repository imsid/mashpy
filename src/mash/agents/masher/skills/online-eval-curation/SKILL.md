---
name: online-eval-curation
description: Build normalized online eval JSONL rows from Mash log files.
---

# Online Eval Curation

Use this skill when asked to create or extend an online eval dataset from Mash logs.
This is curation only, not evaluation.

Required output shape:
- `source_log_path`
- `app_id`
- `session_id`
- `trace_id`
- `user_message`
- `assistant_response`
- `tools_called`
- `tool_call_count`
- `step_count`
- `input_tokens`
- `output_tokens`

Workflow:
1. Read the target log file and isolate the requested run or session.
2. Correlate run events by `session_id` and `trace_id`.
3. Build a normalized record object using `session_id` + `trace_id` as the unique run key.
4. Write the record with `append_jsonl`.
5. If that `session_id` + `trace_id` pair already exists, skip the append and report that it was already present.

Rules:
- Keep records machine-friendly and compact.
- Use raw event data for metrics whenever possible.
- Include the source log path in every record.
