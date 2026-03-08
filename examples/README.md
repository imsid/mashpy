# Mash SDK Examples

These examples are runnable CLI apps built with Mash SDK.

## Run from repository root

```bash
uv run --extra cli python -m examples.simple_app
uv run --extra cli python -m examples.command_app
uv run --extra cli python -m examples.subagent_app
uv run --extra api python -m examples.api_app
```

Pass example-specific arguments normally:

```bash
uv run --extra cli python -m examples.simple_app --root /tmp/mash-example
uv run --extra cli python -m examples.command_app --root /tmp/mash-example
uv run --extra cli python -m examples.subagent_app --workspace-folder /Users/sid/Projects/mashpy
uv run --extra api python -m examples.api_app --port 8000
```

Notes:

- Interactive model responses require a valid `ANTHROPIC_API_KEY`.
- Examples auto-load env from both repo `.env` and `examples/.env`.
- The examples write sqlite and jsonl files under `<root>/.mash/`.

## Run telemetry web with `examples.api_app`

Start the API example first:

```bash
uv run --extra api python -m examples.api_app --port 8000
```

In a second terminal, start the telemetry web dev server:

```bash
cd src/mash/telemetry/web
npm run dev
```

Then open `http://127.0.0.1:5173`.
The Vite dev proxy forwards `/api/*` requests to `http://127.0.0.1:8000`.
