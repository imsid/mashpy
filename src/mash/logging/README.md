# Logging

`src/mash/logging` provides structured runtime events and trace correlation utilities.

## What This Package Does
- Defines the structured event payloads emitted across the system.
- Implements the JSONL event logger used by hosted agents.
- Provides helpers for creating and propagating trace IDs across agent, tool, and host boundaries.

## Main Components
- `events.py`: structured event types used across Mash.
- `logger.py`: JSONL event logger implementation.
- `trace_context.py`: trace ID helpers.

## Role In The System
- Logging is shared by core execution, runtime hosting, tools, and subagent orchestration.
- Event shapes should remain machine-readable and stable enough for downstream inspection and tests.
