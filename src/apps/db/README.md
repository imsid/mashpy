# DB App (`db-agent`)

`src/apps/db` is a Mash-based CLI agent for BigQuery exploration plus local metrics-layer config work.

It combines:

- Remote BigQuery MCP tools (dataset/table inspection + SQL execution)
- Local metrics-layer tools (read/validate/write YAML configs, compile metrics to SQL)
- Mash CLI/agent runtime (REPL, memory, skills, logging)

## Quick Start

Run from the repo root.

1. Install dependencies (if needed):

```bash
uv sync
```

2. Create `src/apps/db/.env` with at least:

```env
ANTHROPIC_API_KEY=...
# Optional
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
BIGQUERY_PROJECT_ID=your-gcp-project-id
BIGQUERY_MCP_URL=https://bigquery.googleapis.com/mcp
```

3. Set up Google Application Default Credentials (ADC) so the app can fetch a BigQuery access token (for example, `gcloud auth application-default login`, or another ADC method such as `GOOGLE_APPLICATION_CREDENTIALS`).

4. Start the agent:

```bash
uv run db-agent
```

Notes:

- The console command is `db-agent`, but the internal Mash app id is `data-agent`.
- If ADC auth fails at startup, the app still opens, but BigQuery MCP tools will not be connected.

## How It Works

At startup, the app:

1. Loads config from `src/apps/db/.env`.
2. Creates a Mash `SQLiteStore` for memory at `src/apps/db/.mash/data-agent.db`.
3. Registers local tools from `src/apps/db/local_tools.py`:
   - Data steward tools: list/read/validate/write metrics-layer YAML configs
   - Data analyst tool: `compile_metric_configs_to_sql`
4. Loads custom skills from `src/apps/db/.mash/skills` (if present).
5. Creates a BigQuery MCP connection using an ADC-generated bearer token and an allowlist of BigQuery tools.
6. Starts the Mash REPL and logs events to `src/apps/db/logs/db.jsonl`.

### Metrics Layer Workflow (Important)

SQL execution is intentionally a two-step flow:

1. Compile metric configs into SQL with `compile_metric_configs_to_sql` (local tool)
2. Execute the returned SQL with BigQuery MCP `execute_sql` (remote MCP tool)

This keeps semantic metric definitions in YAML (`metrics_layer/<dataset>/sources|metrics`) while using BigQuery for actual query execution.

## How It Integrates With Mash

This app is a `MashApp` subclass (`DataAgentApp` in `src/apps/db/cli.py`) and uses Mash framework components directly:

- `MashApp` for the CLI REPL and slash commands
- `Agent` + `AgentConfig` for the think/act loop
- `ToolRegistry` for local db tools
- `SkillRegistry` for role-based skills
- `SQLiteStore` for persistent memory
- MCP integration for remote BigQuery tools

In short, `db-agent` is a standard Mash app with a db-specific prompt, local metrics-layer tools, and a BigQuery MCP backend.
