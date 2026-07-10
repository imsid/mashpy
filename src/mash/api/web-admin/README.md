# Admin UI

Reference for the Mash admin dashboard — the React SPA in this directory
(`src/mash/api/web-admin/`). It is built with Vite, bundled into
`src/mash/api/static/admin/`, and served at `/admin` by `mash.api.admin_ui`
(mounted only when the bundle is present).

This README is intended to be prompt-cache friendly for the `admin-copilot`
agent: it maps every dashboard tab to what it surfaces and the API call that
feeds it, without duplicating component source. For HTTP route internals see
`../README.md` (the `api-copilot` doc); for the telemetry data model see
`src/mash/runtime`.

## What this answers

- "Is X tracked / visible in the admin UI?" — find the tab below.
- "What does X mean in the UI?" — each tab lists what it displays.
- "Which tab shows X?" / "What endpoint feeds tab Y?" — see the tab table.

## Stack & layout

- React 18 + `react-router-dom`, Vite, Tailwind. Entry: `src/main.jsx` → `App.jsx`.
- `App.jsx` declares the routes; `components/Shell.jsx` renders the left-nav and
  the routed `<Outlet>`.
- Routes (one file per tab) live in `src/routes/`; reusable UI in
  `src/components/`; the API client and helpers in `src/lib/`.
- All data is read through `src/lib/api.js`, a thin client over the host API at
  `/api/v1`. Auth rides the same-origin `mash_api_key` cookie that the `/admin`
  index response sets — requests carry no explicit Authorization header. The
  success envelope `{ "data": ... }` is unwrapped to `data`; failures raise a
  typed `ApiError`.

## Tabs

Nav order is defined in `components/Shell.jsx`. The sidebar is grouped into
**Deployment** (Agents, Workflows, Hosts, Tools, Skills — the pool and its
compositions) and **Activity** (Logs, Feedback, Evals — what the deployment
did), with Overview on top and Reference below a divider. Each tab is a route
component in `src/routes/`.

| Tab | Route | Surfaces | API (`api.*` in `lib/api.js`) → endpoint |
| --- | --- | --- | --- |
| Overview | `/` (`Overview.jsx`) | Per-agent usage/cost and recent-session rollups across the pool; summary cards and charts. | `listAgents` → `GET /agent`; per agent `usage` → `GET /telemetry/usage`, `listSessions` → `GET /agent/{id}/sessions` |
| Agents | `/agents` (`Agents.jsx`) | The pooled agents and hosts in the deployment. | `listAgents` → `GET /agent` |
| Workflows | `/workflows` (`Workflows.jsx`), `/workflows/:workflowId` (`WorkflowDetail.jsx`), `/workflows/:workflowId/runs` (`WorkflowRuns.jsx`), `/workflows/:workflowId/runs/:runId` (`WorkflowRunDetail.jsx`) | Registered workflow definitions, typed step pipelines, schema-driven run submission, store-backed run history, live step status, snapshots, audit events, results, and resume/run-again actions. | `listWorkflows` → `GET /workflow`; `getWorkflow` → `GET /workflow/{id}`; `runWorkflow` → `POST /workflow/{id}/run`; run/history/audit methods under `/workflow/{id}/runs` |
| Hosts | `/hosts` (`Hosts.jsx`) | Host compositions; create/edit a host (`PUT`) and submit a test request to its primary. | `listHosts` → `GET /hosts`, `listAgents` → `GET /agent`; `defineHost` → `PUT /hosts/{id}`; `submitHostRequest` → `POST /hosts/{id}/request` |
| Tools | `/tools` (`Tools.jsx`), `/tools/:toolName` (`ToolDetail.jsx`) | Tool catalog as cards with invocation counts; detail view per tool. | `listTools` → `GET /tools`; `listToolInvocations` → `GET /telemetry/tool-invocations` |
| Skills | `/skills` (`Skills.jsx`), `/skills/:skillName` (`SkillDetail.jsx`) | Skill catalog as cards with invocation counts; detail view per skill. | `listSkills` → `GET /skills`; `listSkillInvocations` → `GET /telemetry/skill-invocations` |
| Logs | `/logs` (`Logs.jsx`) | Session rollups with their traces (tokens plus cache read/write at both levels), filterable by participating agent (primary or subagent) and by workflow, with a session count; plus the raw HTTP API event log with request/response detail in a drawer. | `listSessionRollups` → `GET /telemetry/sessions` (`agent_id`, `workflow_id`, returns `total`); `listTraces` → `GET /telemetry/traces`; `listApiEvents` → `GET /telemetry/api/events` |
| Feedback | `/feedback` (`Feedback.jsx`) | Submitted feedback, filterable by agent and free-text query. | `listFeedback` → `GET /feedback`; `listAgents` → `GET /agent` |
| Evals | `/evals` (`Evals.jsx`), `/evals/:evalId` (`EvalDetail.jsx`), `/evals/:evalId/experiments/:experimentId` (`ExperimentDetail.jsx`), `/evals/:evalId/compare` (`ExperimentCompare.jsx`) | Synthetic eval datasets and rubrics; experiments with per-run scores, detail and side-by-side compare views. | `listEvals` → `GET /evals`; `getEval` → `GET /evals/{id}`; `listExperiments` → `GET /evals/{id}/experiments`; run/compare views under the same prefix |
| Reference | `/reference` (`Reference.jsx`) | Generated API reference from the live OpenAPI schema, plus the bundled CLI reference (`src/cli.json`). | `openapi` → `GET /openapi.json` |

Notes:

- The **Tools** and **Skills** tabs (catalog cards + detail views) were added in
  the admin-ui change that introduced `ToolDetail.jsx` / `SkillDetail.jsx`.
- Tool/skill invocation **counts** come from telemetry
  (`/telemetry/tool-invocations`, `/telemetry/skill-invocations`), while the
  catalog of available tools/skills comes from the pool (`/tools`, `/skills`).
- Host compositions created in the UI are in-memory and reset on restart unless
  defined in code (surfaced in the Hosts editor subtitle).

## Shared components (`src/components/`)

- `Shell.jsx` — left-nav + routed outlet (the tab list lives here).
- `Page.jsx` — `PageHeader`/page scaffold used by every route.
- `Table.jsx`, `BarChart.jsx`, `Chip.jsx`, `Json.jsx`, `Markdown.jsx`,
  `CopyId.jsx` — presentation primitives.
- `Drawer.jsx` / `TraceDrawer.jsx` — slide-over panels; `TraceDrawer` renders a
  session trace.
- `Form.jsx`, `State.jsx` — form controls and load/empty/error state wrappers
  (`State` pairs with `lib/useApi.js`).
- `components/workflows/` — workflow pipeline, status, run submission, and step
  inspection components.

## Helpers (`src/lib/`)

- `api.js` — the API client (`api.*` methods above) and `ApiError`.
- `useApi.js` — `useApi(loader, deps)` hook returning `{ data, error, loading }`.
- `format.js` — duration/number/token formatting.
- `conversation.js` — shaping session history into a renderable conversation.
- `workflow.js` — schema projection, input validation, catalog classification,
  queued-step merging, and workflow event helpers.

## Build & serve

- Dev: `npm install` then `npm run dev` (Vite) in this directory.
- Build: `npm run build` → emits into `../static/admin/`; `mount_admin_ui` then
  exposes `/admin`, `/admin/{path}`, and `/admin/assets/...`. When the bundle is
  absent the route is simply not mounted.
