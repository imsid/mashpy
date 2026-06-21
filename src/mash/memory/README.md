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
- `compaction.py`: memory compaction support.
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
