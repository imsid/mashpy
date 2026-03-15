# Mash Examples

This directory contains one canonical Mash app example:

**example_app.py**: Defines one primary agent, one research subagent, exposes `build_host()`, and can run the API server directly.

The example shows the intended Mash app shape:

- define one or more `AgentSpec`s
- expose `build_host()`
- let mash manage persistence under `MASH_DATA_DIR/<agent_id>/...`
- run the app behind `mash.api`

## Run from the repo

Create the repo virtualenv and install dependencies first:

```bash
uv venv
uv sync
source .venv/bin/activate
```

Set `MASH_DATA_DIR` in `examples/.env` before starting the app. Example:

```dotenv
MASH_DATA_DIR=.mash
```

Start the example host:

```bash
python -m examples.example_app \
  --workspace-root /Users/sid/Projects/mashpy \
  --host 127.0.0.1 \
  --port 8000 \
  --api-key secret
```

Notes:

- Interactive responses require a valid `ANTHROPIC_API_KEY`.
- The example auto-loads env from both repo `.env` and `examples/.env`.
- Put `MASH_DATA_DIR` in `examples/.env` for local development.
- Persistence lives under `MASH_DATA_DIR/<agent_id>/...`.

## Connect with the remote CLI

`mash` is bundled with `mashpy`, so once the example host is running you can connect immediately.

In the activated repo environment:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret
mash status
mash agents
mash repl --agent primary
```

## Telemetry web

As a third step, open the built-in telemetry UI:

- [http://127.0.0.1:8000/telemetry](http://127.0.0.1:8000/telemetry)

Use it to inspect sessions, traces, and events from the running example app.

## Container

Build the base runtime image from the repo root:

```bash
docker build -t mashpy/mash-host-base:latest .
```

Build the example app image:

```bash
docker build -t mashpy/example-app:latest -f examples/Dockerfile .
```

Run it locally:

```bash
docker run \
  -p 8000:8000 \
  -e ANTHROPIC_API_KEY=... \
  -e MASH_API_KEY=secret \
  -e MASH_DATA_DIR=/var/lib/mash \
  -v $(pwd)/data:/var/lib/mash \
  mashpy/example-app:latest
```

Container contract:

- `MASH_HOST_APP=examples.example_app:build_host`
- `MASH_DATA_DIR=/var/lib/mash`
- mount persistent storage at `/var/lib/mash`
- expose port `8000`
- pass provider secrets such as `ANTHROPIC_API_KEY`
