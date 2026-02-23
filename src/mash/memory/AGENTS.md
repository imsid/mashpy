# AGENTS Guide for `src/mash/memory`

## Scope
Persistence and memory utilities: memory store protocol + backends, signal collection, checkpoint summarization, and memory search pipeline.

## Invariants
- SQLite operations should remain guarded by the store lock for thread safety.
- `save_turn()` uses `trace_id` as `turn_id`; downstream code depends on this identity.
- `get_turns()` returns chronological order.
- Signal persistence is numeric-only (non-numeric values are skipped).
- Memory search retrieval must go through `MemoryStore` protocol methods (`keyword_search`, `semantic_search`) rather than bypassing the store.

## Store Organization
- Public memory store imports remain under `src/mash/memory/store/` (package) and should expose backend-agnostic types plus selected concrete backends.
- Backend implementation details live under `src/mash/memory/store/backends/` (currently `sqlite`).
- Backend-specific store invariants and extension guidance should be documented in `src/mash/memory/store/AGENTS.md`.

## Data Model Notes
- Conversation turns are session-scoped.
- Preferences and app data are app+session scoped.
- App data values should remain JSON-serializable when possible.
- Memory search operates on `turns.user_message` and `turns.agent_response` and returns turn-level results.

## Search
- Search pipeline code lives under `src/mash/memory/search/` with stage-separated modules (`parser`, `retrieval`, `rerank`, `service`).
- Query DSL currently requires a prefix: `@user:` or `@agent:`.
- Search result previews should remain plain text and capped at 200 characters.
- Weighted fusion defaults to semantic `0.7` and keyword `0.3`.
- SQLite implementations of `keyword_search()` / `semantic_search()` may be phase-gated; keep protocol contracts stable while backend support catches up.

## Compaction
- `compact_conversation()` must create a checkpoint turn with metadata type `summary_checkpoint`.
- Checkpoint metadata fields (`reason`, `turn_limit`, `turn_ids`, `token_usage`) are consumed by CLI history logic.
- Keep compaction prompts concise and deterministic.
