# Cortex

`src/mash/agents/cortex` is Mash's per-app memory. The host keeps one Cortex
per app in Postgres, shared by every agent in the pool and built from every
session in the app. A pool turns it on
with `HostBuilder().enable_cortex(profile_max_chars=4000)`.

- `models.py`: `CortexSettings`, `CortexEntry`, `CortexSnapshot`,
  `CortexAppInfo`, `EntryChange`, `CortexConflictError`.
- `store.py`: `CortexStore` over `cortex_app` and `cortex_entry`
  (migration `022_app_and_policy.sql`). It opens its own pool and uses an
  in-memory backend for the `postgresql://test/runtime` sentinel. Every write
  bumps `cortex_app.version`.
- `tools.py`: `CortexReader`, the profile block and `search_cortex`.
- `spec.py`: `CortexAgentSpec`, agent id `cortex-agent`.
- `workflows.py`: the `cortex-build` workflow.
- `context.py`: `CortexRuntimeContext`, the pool, settings and store the
  workflow steps and API routes share on the host.
- `routes.py`: the Cortex API router.

## Tables

`cortex_app` has one row per app: `profile`, `built_through` (the
`created_at` of the newest turn the last build covered), `version`, and
`last_build` (the last build's run id and result). `cortex_entry` holds the
facts behind the profile: `topic`, `content`, `origin` (`build` or `app`) and
`sources` (the agent, session and trace each fact came from). Deleting the
app row deletes its entries.

## Reads in agent processes

`build_agent_server` gives each runtime a `CortexReader` when the pool
enables Cortex. Reads run in the agent's process, which holds the database
credentials; agents' code runs in the policy's sandbox, which has no route to
the database.

On every request, for the request's app:

- `context.load` appends an `APP MEMORY` block with the profile, capped
  at `profile_max_chars` with a `[profile truncated]` marker. It reads inside
  the checkpointed step, so a replay sees the profile the request saw.
- The turn agent gets `search_cortex(query, limit)`, which returns the app's
  entries matching every term, ranked. The app comes from the request; an
  `app_id` in the arguments is ignored, and the output does not include it.

The `/h2a` relay copies the companion request's `app_id` onto its children,
so a child reads the same app. Everything in an app can read every
conversation in it this way; separate memory means separate apps.

## `cortex-build`

```text
collect (code) -> curate (code: cortex-agent) -> write (code)
```

A run builds the Cortex of the app it acts for. `workflow_input`
(`CortexBuildInput`): `limit` (turns per run, default 200, max 1000). Run it
on demand: `POST /api/v1/workflow/cortex-build/run` with
`{"app_id": "slack-bot", "policy_id": "acme-ws", "input": {"limit": 200}}`;
`cortex-agent` runs under the policy.

- **collect** loads the app's Cortex and its turns after `built_through`
  from `MemoryStore.list_app_turns`: across agents and sessions, oldest first, without
  workflow or child turns. Each turn carries the tools it ran (arguments
  from `execute_tool.start`, results from `execute_tool.end`); `search_cortex` results are left out. Messages are
  clipped to 4000 characters, arguments to 500, results to 1000, 20 tool
  calls per turn, and turns stop at 60000 characters of bundle.
- **curate** passes through without calling the model when there are no new
  turns. Otherwise it submits one `cortex-agent` request with a fixed request
  id (run and task), so a replay reattaches to it, and validates its
  structured output (`profile`, `upserts`, `deletes`).
- **write** calls `CortexStore.apply_build` in one transaction: a
  compare-and-set on the version `collect` read, raising
  `CortexConflictError` if a build or an app edit landed in between. Pinned
  `app` entries are never changed or deleted; an upsert naming an unknown
  entry adds a new one. The watermark moves to the last covered turn, and a
  retried write finds its run id in `last_build` and returns the stored
  result.

The result reports `turns_covered`, `entries_written`, `entries_deleted` and
`remaining`. Rerun while `remaining` is above 0.

`cortex-agent` has no tools of its own and `max_steps=4`. It selects its
provider like masher's agents, with `CORTEX_AGENT_<PROVIDER>_MODEL`
overrides.

## Cortex API

Mounted under the host's API prefix only when Cortex is enabled, behind the
host's API key.

| Route | Does |
|---|---|
| `GET /cortex/apps/{app_id}` | `profile`, `built_through`, `version`, `entry_count`, `updated_at` |
| `GET /cortex/apps/{app_id}/entries` | Entries newest first; `q`, `origin` (`build`/`app`), `limit` (1-200, default 50), `after` (the previous page's `next`) |
| `POST /cortex/apps/{app_id}/entries` | Create an `app` entry from `topic` and `content`; creates the app row if missing |
| `PATCH /cortex/apps/{app_id}/entries/{entry_id}` | Change `topic` or `content`; the entry becomes `app` |
| `DELETE /cortex/apps/{app_id}/entries/{entry_id}` | Delete one entry |
| `DELETE /cortex/apps/{app_id}` | Delete the app's whole Cortex |

Writes return the app's new `version`. Unknown apps and entries return
`404 CORTEX_NOT_FOUND`, and a malformed `app_id` returns `400 INVALID_APP_ID`.
The profile is written only by builds, so an app shapes it by editing entries
and running a build. A deleted entry is not relearned from turns already
built; deleting the whole Cortex resets the watermark, so the next build
starts from the app's first turn.
