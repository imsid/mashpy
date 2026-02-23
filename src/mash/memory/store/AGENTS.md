# AGENTS Guide for `src/mash/memory/store`

## Scope
Memory store protocol and backend implementations (SQLite today, other backends later).

## Public API Boundary
- Callers should import store types from `mash.memory.store`.
- `MemoryStore` is the backend-agnostic protocol contract.
- Backend implementation details should remain internal to this package.

## Backend Organization
- Backend code belongs under `src/mash/memory/store/backends/<backend>/`.
- Keep backend internals isolated from callers; expose only stable store types from `store/__init__.py`.
- New backends (e.g. Postgres, MongoDB) should implement the `MemoryStore` protocol and preserve return shapes.

## Behavioral Invariants
- Preserve `MemoryStore` method signatures and return payload shapes across backends.
- Memory search retrieval must access storage through `MemoryStore.keyword_search()` and `MemoryStore.semantic_search()`.
- Keep search result contracts stable (`turn_id`, `session_id`, `score`, `preview` for store-level search hits).

## SQLite Notes
- SQLite operations must remain guarded by the store lock for thread safety.
- `save_turn()` uses `trace_id` as `turn_id`.
- `get_turns()` returns chronological order.
- Signal persistence is numeric-only (non-numeric values are skipped).
- `semantic_search()` is currently a stub (`NotImplementedError`) for SQLite.
