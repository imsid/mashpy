# AGENTS Guide for `src/mash/telemetry`

## Scope
This module is deprecated. Runtime and observability APIs now live in `mash-api`.

## Invariants
- Telemetry server entrypoint files are removed; do not reintroduce local telemetry server startup here.
- The module should clearly direct users to `mash-api`.
- Do not add new API route logic in this package.

## Change Rules
- Keep deprecation messaging explicit and actionable.
- Route all new API/server work to `packages/mash-api`.
