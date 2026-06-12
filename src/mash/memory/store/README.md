# Memory Store

`src/mash/memory/store` defines the backend-agnostic storage contract used by runtime memory, session persistence, structured event logging, and memory-search retrieval.

## What This Package Exposes
- `MemoryStore`: protocol contract that all storage backends must implement.
- `SQLiteStore`: the built-in file-backed backend.
- `PostgresStore`: the built-in Postgres-backed backend.

## Supported Backends
- `SQLiteStore` in `src/mash/memory/store/backends/sqlite/store.py`
- `PostgresStore` in `src/mash/memory/store/backends/postgres/store.py`

Backend expectations:
- Backends must preserve the `MemoryStore` method signatures.
- Backends must preserve return shapes so callers and agents can switch backends without changing logic.
- Search methods must return store-level hit dictionaries with:
  - `turn_id`
  - `session_id`
  - `score`
  - `preview`

Current backend status:
- Keyword search is implemented in `SQLiteStore` via SQLite FTS5.
- Keyword search is implemented in `PostgresStore` via Postgres full-text search.
- Semantic search is part of the protocol, but both built-in backends currently raise `NotImplementedError`.

## Protocol Methods

### Structured Logs

`save_logs(logs) -> None`
- Persists one or more structured log rows.
- Required per-row inputs:
  - `app_id`
  - `session_id`
  - `trace_id`
  - `event_class`
  - `event_type`
  - `created_at`
  - `payload`

`get_logs(app_id, session_id=None, trace_id=None, limit=None, after_log_id=None) -> list[dict]`
- Returns reconstructed public event records for one app scope.
- Expected shape per item:
  - `event_type`
  - `ts`
  - `app_id`
  - `session_id`
  - `event_class`
  - `payload`
  - class-specific top-level fields such as `trace_id`, `duration_ms`, `model`, `tool_calls`, `metadata`
- Notes:
  - SQLite uses an internal autoincrement `id` for cursoring and returns it as `log_id`.
  - API/tool callers strip `log_id` before returning events to users.

`list_recent_log_traces(app_id, session_id, limit=5) -> list[dict]`
- Returns recent trace summaries grouped from the `logs` table.
- Expected shape per item:
  - `trace_id`
  - `session_id`
  - `app_id`
  - `started_at`
  - `last_event_at`
  - `event_count`

`get_latest_log_trace(app_id, session_id) -> dict | None`
- Convenience wrapper over `list_recent_log_traces(..., limit=1)`.

### Turn Persistence

`save_turn(...) -> str`
- Persists one conversation turn.
- Inputs:
  - `trace_id`: used as the stored `turn_id`
  - `session_id`
  - `app_id`
  - `user_message`
  - `agent_response`
  - `signals`
  - `session_total_tokens`
  - `metadata`
- Returns:
  - the persisted `turn_id`

Notes:
- `signals` are backend-defined JSON-compatible values associated with the turn.

`get_turns(session_id, app_id, limit=None) -> list[dict]`
- Returns conversation turns for one session.
- Expected shape per item:
  - `turn_id`
  - `user_message`
  - `agent_response`
  - `session_total_tokens`
  - `signals`
  - `metadata`
  - `created_at`

Behavior:
- The contract is chronological turn order.
- `app_id` is required and scopes reads to one agent.
- In SQLite, `limit=None` returns all turns ascending by time; limited reads still return ascending order after an internal reverse query.

`get_turn_by_ids(pairs, app_id) -> list[dict] | None`
- Bulk lookup by exact `{session_id, turn_id}` pairs.
- Expected shape per returned item:
  - `turn_id`
  - `session_id`
  - `user_message`
  - `agent_response`

Behavior:
- `app_id` is required and scopes reads to one agent.
- Missing pairs are omitted.
- Returns `None` when no requested pairs are valid or found.

`get_session_signals(session_id, app_id, limit=None) -> list[dict]`
- Returns chronological per-turn signal payloads for one session.
- Expected shape per item:
  - `turn_id`
  - `created_at`
  - `signals`

Behavior:
- The contract is chronological turn order.
- `app_id` is required and scopes reads to one agent.
- Turns with no persisted signals must still be returned with `signals = {}`.
- This method returns signal values only; signal definitions are runtime-owned metadata surfaced by the runtime/API layer.

### Session Listing And Trace Lookup

`list_sessions(app_id) -> list[dict]`
- Lists persisted sessions for one app.
- Expected shape per item:
  - `session_id`
  - `turn_count`
  - `last_activity_at`
  - `session_total_tokens`

`get_latest_session(app_id) -> dict | None`
- Convenience wrapper returning the first item from `list_sessions(app_id)`.

`list_recent_traces(app_id, session_id, limit=5) -> list[dict]`
- Returns recent turns/traces for a specific session within an app.
- Expected shape per item:
  - `trace_id`
  - `session_id`
  - `user_message`
  - `agent_response`
  - `metadata`
  - `created_at`

`get_latest_trace(app_id, session_id) -> dict | None`
- Convenience wrapper over `list_recent_traces(..., limit=1)`.

### Search Methods

`keyword_search(column, query_term, limit, session_id=None, app_id=None) -> list[dict]`
- Searches one text column by keyword.
- Inputs:
  - `column`: currently expected to be `user_message` or `agent_response`
  - `query_term`
  - `limit`
  - optional `session_id`
  - optional `app_id`
- Required hit shape:
  - `turn_id`
  - `session_id`
  - `score`
  - `preview`

SQLite behavior:
- Uses FTS5 with token-AND semantics.
- Returns normalized scores in `(0, 1]` based on result rank, not raw BM25.

`semantic_search(column, query_term, query_embedding, limit, session_id=None, app_id=None) -> list[dict]`
- Protocol hook for semantic/vector retrieval over one text column.
- Required hit shape matches `keyword_search`.

SQLite behavior:
- Not implemented yet.

## Return Shape Notes
- Store methods return plain dictionaries/lists, not typed model objects.
- `metadata` is intended to be JSON-compatible.
- Callers should not assume backend-specific extra keys beyond the documented shapes.

## Backend Selection
- `AgentSpec.build_memory_store()` uses `MASH_DATABASE_URL` when it is set.
- When `MASH_DATABASE_URL` is unset, the default fallback is SQLite at `<MASH_DATA_DIR>/<agent_id>/state.db`.
- In multi-agent pools, `AgentPool` shares a single `PostgresStore` instance
  across all agents that use the default `build_memory_store()`. Agents that
  override `build_memory_store()` get their own store instance. Store lifecycle
  (open/close) is owned by the host, not by individual runtimes.

## SQLite-Specific Notes
- Thread safety is enforced with a store-level lock around DB operations.
- `trace_id` is persisted as the canonical `turn_id`.
- Structured runtime events are persisted in the `logs` table inside the same `state.db` database file.
- The `logs` table uses an internal autoincrement `id` for stable ordering and streaming cursors.
- FTS index rows are rebuilt automatically if the main `turns` table has data but the FTS table is empty.
- Database files are created on demand; parent directories are also created automatically.

## Postgres-Specific Notes
- `PostgresStore` persists only the protocol-required storage objects: turns, signals, and logs.
- Keyword search is column-scoped and implemented with Postgres full-text search.
- `memory_logs.id` is the stable cursor for `after_log_id`.
