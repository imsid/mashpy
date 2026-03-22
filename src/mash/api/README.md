# API

`src/mash/api` exposes the hosted HTTP surface for Mash agents.

## What This Package Does
- Builds the FastAPI application around a `MashAgentHost`.
- Exposes host-serving entrypoints used by the CLI and deployments.
- Owns API-facing config such as bind host, bind port, API key, CORS, and observability settings.
- Serves the packaged telemetry UI and its related static assets.

## Main Entry Points
- `app.py`: builds the FastAPI app and wires runtime-facing routes.
- `main.py`: command helpers for `mash host serve` and host target resolution.
- `config.py`: `MashHostConfig`.
- `types.py`: public host/app typing surface.

## UI And Static Assets
- `telemetry_ui.py`: resolves the packaged telemetry UI directory.
- `static/`: built frontend assets served by the host.
- `web/`: telemetry UI source.

## Typical Flow
1. A caller provides a `MashAgentHost`, `MashAgentHostBuilder`, or `MashHostApp`.
2. `create_app` wraps that host in HTTP routes.
3. `run_host` starts the HTTP service with `MashHostConfig`.
4. The CLI and other remote clients consume the resulting API surface.

## Public Exports
- `create_app`
- `run_host`
- `MashHostConfig`
- `MashHostApp`
- `get_telemetry_static_dir`
