# Telemetry Module Status

`mash.telemetry` is deprecated.

Telemetry and runtime HTTP APIs are now consolidated in the `mash-api` package.

## What changed

- Telemetry server entrypoint files were removed (`src/mash/telemetry/server.py`, `src/mash/telemetry/__main__.py`).
- Use `mash-api` as the canonical API service for:
  - runtime interactions
  - runtime controls
  - telemetry endpoints

## Migration

1. Install `mash-api`.
2. Start your service via `mash-api --app module:factory` or by composing `mash_api.create_app(...)`.
3. Update clients to the consolidated `/api/v1/*` routes exposed by `mash-api`.
