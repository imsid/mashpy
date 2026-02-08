# AGENTS Guide for `src/mash/memory`

## Scope
Persistence and context compaction: SQLite store, signal collection, checkpoint summarization.

## Invariants
- SQLite operations should remain guarded by the store lock for thread safety.
- `save_turn()` uses `trace_id` as `turn_id`; downstream code depends on this identity.
- `get_turns()` returns chronological order.
- Signal persistence is numeric-only (non-numeric values are skipped).

## Data Model Notes
- Conversation turns are session-scoped.
- Preferences and app data are app+session scoped.
- App data values should remain JSON-serializable when possible.

## Compaction
- `compact_conversation()` must create a checkpoint turn with metadata type `summary_checkpoint`.
- Checkpoint metadata fields (`reason`, `turn_limit`, `turn_ids`, `token_usage`) are consumed by CLI history logic.
- Keep compaction prompts concise and deterministic.
