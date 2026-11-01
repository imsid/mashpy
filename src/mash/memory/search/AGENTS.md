# AGENTS Guide for `src/mash/memory/search`

## Scope
Hybrid memory search pipeline for `turns`: query parsing, retrieval orchestration, reranking, and service composition.

## Pipeline Contracts
- Keep the pipeline split into three stages with simple boundaries: parser, retrieval, reranking.
- `MemorySearchService.search()` returns ranked `SearchResult` items with `trace_id`, `similarity_score`, and `preview`.
- `MemorySearchService.search()` requires `app_id`.
- Retrieval methods must access storage only through `MemoryStore.keyword_search()` and `MemoryStore.semantic_search()`.

## Events
- Memory search writes no events. A search the model runs is the request's
  `execute_tool` span for `search_conversations`.
- Preserve the short-circuit behavior for non-positive limits: return no results without touching the store.

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
