# Telemetry Developer Guide

This document explains how Mash telemetry is structured and how to release it.

## What Lives Where

- Telemetry API server code: `src/mash/telemetry/server.py`
- Optional UI loader (`--ui auto|on|off`): `src/mash/telemetry/ui_loader.py`
- Telemetry web source (React/Vite): `src/mash/telemetry/web/`
- Companion UI package (published separately): `packages/mash-telemetry-web/`

## Runtime Model

- `mashpy` owns telemetry API routes (`/api/v1/*`) and telemetry primitives.
- `mash-telemetry-web` is the optional observer UI package with static assets.
- Apps pass runtime paths (`--log`, optional `--memory-db`) when running:

```bash
python -m mash.telemetry --log /path/to/events.jsonl [--memory-db /path/to/memory.db] [--ui auto|on|off]
```

## Local Development

For local API + web UI development together:

```bash
# Terminal 1: telemetry API server
python -m mash.telemetry \
  --log /path/to/events.jsonl \
  --memory-db /path/to/memory.db \
  --ui off

# Terminal 2: web dev server (proxies /api -> :8765)
make telemetry-web-install
make telemetry-web
```

Default dev UI endpoint:

- Web UI: `http://127.0.0.1:5173`

## Step 2: Validate Packaged UI Path

Before release, validate the runtime path that serves UI from the optional package:

```bash
# From repo root: rebuild and sync static assets into the package
make telemetry-web-package-sync

# Install/update the local package in your active environment
pip install -e ./packages/mash-telemetry-web

# Run telemetry server and require packaged UI to be present
python -m mash.telemetry \
  --log /path/to/events.jsonl \
  --memory-db /path/to/memory.db \
  --ui on
```

Then open `http://127.0.0.1:8765` and verify the observer UI loads from the packaged assets.

## Release Process

When you change code in `src/mash/telemetry`, decide which package(s) to release:

1. API/backend only changes (`server.py`, route behavior, envelopes, search behavior):
- Release `mashpy`.

2. Web UI source changes (`src/mash/telemetry/web/src/*`, styling, UI behavior):
- Release `mash-telemetry-web`.

3. Both backend and UI changed:
- Release both packages.
- Publish order: `mash-telemetry-web` first, then `mashpy`.

## Step-by-Step Release Flow

### 1) Make code changes

Edit telemetry code under `src/mash/telemetry`.

### 2) If UI changed, rebuild and sync static assets

From repo root:

```bash
make telemetry-web-package-sync
```

This updates static files inside:
`packages/mash-telemetry-web/src/mash_telemetry_web/static/`

### 3) Bump versions

- For UI package release: update `packages/mash-telemetry-web/pyproject.toml` version.
- For SDK release: update root `pyproject.toml` version.
- If `mashpy` now depends on a newer UI package, update root extra constraint:
  - `telemetry-web = ["mash-telemetry-web>=X.Y.Z"]`

### 4) Commit and push

Commit all relevant files, including synced static assets if UI changed.

### 5) Create GitHub Release with the correct tag

Publishing is handled by `.github/workflows/publish.yml` on Release publish/release events.

Supported tags:

- `v<version>` or `mashpy-v<version>`
  - Publishes `mashpy` from repo root.

- `mash-telemetry-web-v<version>`
  - Publishes `mash-telemetry-web` from `packages/mash-telemetry-web`.

Tag version must exactly match the target package `pyproject.toml` version.

### 6) Validate installation

After publishing, verify:

```bash
pip install "mashpy[telemetry-web]"
# or
uv add "mashpy[telemetry-web]"
```

Then smoke-test runtime:

```bash
python -m mash.telemetry --log /path/to/events.jsonl --ui auto
```

## Notes

- `pip install "mashpy[telemetry-web]"` works only after `mash-telemetry-web` is published.
- `tool.uv.sources` in root `pyproject.toml` helps local uv development, but pip ignores it.
