# AGENTS Guide for `src/mash/memory/search`

## Scope
Hybrid memory search pipeline for `turns`: query parsing, retrieval orchestration, reranking, and service composition.

## Pipeline Contracts
- Keep the pipeline split into three stages with simple boundaries: parser, retrieval, reranking.
- `MemorySearchService.search()` returns ranked `SearchResult` items with `turn_id`, `similarity_score`, and `preview`.
- `MemorySearchService.__init__()` requires an `EventLogger` for structured search telemetry.
- `MemorySearchService.search()` requires `app_id` and generates a per-call `query_id` for event correlation.
- Retrieval methods must access storage only through `MemoryStore.keyword_search()` and `MemoryStore.semantic_search()`.

## Structured Event Logging
- Memory search emits `MemorySearchEvent` entries via `src/mash/logging`.
- Logging ownership is split by layer:
  - `MemorySearchService.search()` logs only service-level lifecycle events (`memory.search.start`, `memory.search.complete`).
  - `QueryParser.parse()` logs parse-stage events (`memory.search.parse.complete`, `memory.search.parse.error`).
  - `HybridRetrievalOrchestrator.retrieve()` logs retrieval-stage events (`memory.search.retrieval.complete`, `memory.search.retrieval.error`).
  - `WeightedFusionReranker.rerank()` logs rerank-stage events (`memory.search.rerank.complete`, `memory.search.rerank.error`).
- The service must pass `event_logger` and `query_id` into parser/retrieval/rerank so stage logs share the same correlation ID.
- `memory.search.start` should include the parsed `query_term` (not `query_length`) and other concise request context.
- Keep payloads concise and stable: counts, booleans, selected column, durations, and short error strings only.
- Preserve the current short-circuit behavior for non-positive limits; service logs a concise `memory.search.complete` event with a short-circuit reason.

## Query DSL
- Supported prefixes are `@user:` and `@agent:` only.
- Queries without a valid prefix should raise a clear `ValueError`.
- Parser normalization should remain conservative: trim and collapse whitespace, but do not add backend-specific tokenization/stemming rules here.

## Retrieval Invariants
- Parser resolves to exactly one `SearchColumn` (`user_message` or `agent_response`).
- `RetrievalConfig` must allow enabling/disabling keyword and semantic retrieval independently.
- At least one retrieval method must be enabled; otherwise raise `ValueError`.
- Retrieval scores are expected to be normalized to `[0, 1]`.
- Retrieval previews should be plain text and capped to `MAX_PREVIEW_CHARS` (currently 200).

## Reranking Invariants
- Weighted fusion combines scores by turn ID and deduplicates across retrieval methods.
- Default weights are semantic `0.7`, keyword `0.3`.
- Missing method score should be treated as `0.0`.
- Prefer keyword preview when both methods return a preview for the same turn.
- Sorting should remain deterministic on ties.

## Phase 1 Boundary
- `SQLiteStore.keyword_search()` and `SQLiteStore.semantic_search()` are protocol stubs only in this phase.
- Do not bake SQLite-specific SQL logic into parser/retriever/reranker modules.
