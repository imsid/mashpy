# Memory

`src/mash/memory` provides persistent memory storage, retrieval, and compaction support for Mash agents.

## What This Package Does
- Defines the memory store protocol used by agents.
- Provides a Postgres-backed store implementation.
- Implements retrieval/search services over stored memory.
- Provides compaction and signaling helpers for longer-lived memory flows.

## Main Components
- `store/`: storage protocol and concrete Postgres backend.
- `search/`: retrieval service, parser/rerank logic, and typed search results.
- `compaction.py`: memory compaction support. A summary checkpoint keeps the ledgers of the turns it replaces, the latest entry per agent session or workflow run, at most 30.
- `ledger.py`: the ledger. When a turn is saved, each `RunSubAgents` item and `RunWorkflow` call the request made becomes an entry in the turn's `metadata["ledger"]`: who did the work, its session or run, its trace, its status, the brief clipped to 200 characters and the answer clipped to 500. Replay appends the entries to the turn's assistant message as a `[ledger]` block that only the model sees.
- `signals.py`: memory-related signaling hooks plus typed signal definitions.

## Public Exports
- `MemorySearchService`
- `MemoryStore`
- `RetrievalConfig`
- `SearchResult`

## Role In The System
- Memory is shared infrastructure for hosted agents and built-in specialists.
- Persistence concerns belong in `store/`, while retrieval concerns belong in `search/`.
- Signal values are persisted per turn by the active memory-store backend.
- Signal definitions are runtime-owned metadata returned alongside per-turn signal reads; they are not persisted as store rows.
