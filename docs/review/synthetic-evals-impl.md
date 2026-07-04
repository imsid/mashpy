# Synthetic Evals — Implementation Plan

Tracks the technical design for issue #123. The product brief is in `synthetic-evals.md`.

---

## Module Layout

```
src/mash/evals/
  __init__.py          # exports: Eval, DatasetRow, ScoringRubric, Experiment, ExperimentRun, EvalService
  models.py            # all frozen dataclasses
  service.py           # EvalService
  postgres/
    __init__.py
    store.py           # PostgresEvalStore
    migrations/        # ordered *.sql files, applied on open()
    loaders/
      __init__.py
      eval.py          # eval table: reads + writes
      dataset.py       # eval_dataset + eval_dataset_row tables
      rubric.py        # eval_rubric table
      experiment.py    # eval_experiment table
      run.py           # eval_experiment_run table

src/mash/api/routes/evals.py   # new route module
```

---

## Data Models (`models.py`)

Frozen dataclasses, same pattern as `RuntimeEvent` / `FeedbackRecord`.

```python
@dataclass(frozen=True)
class ScoringCriterion:
    name: str
    description: str
    weight: float          # all weights across criteria sum to 1.0
    scoring_prompt: str
    scale_min: int = 1
    scale_max: int = 5

@dataclass(frozen=True)
class ScoringRubric:
    rubric_id: str
    eval_id: str
    global_scoring_prompt: str
    criteria: list[ScoringCriterion]
    updated_at: datetime

@dataclass(frozen=True)
class DatasetRow:
    row_id: str
    dataset_id: str
    input: str
    scenario_description: str
    sampling_category: str   # random|multi_tool|multi_agent|high_tokens|long_running|short_running
    expected_behavior: str
    target_agents: list[str]

@dataclass(frozen=True)
class Eval:
    eval_id: str
    host_id: str          # host the eval was generated for; no state snapshot
    user_guidance: str
    dataset_id: str
    rubric_id: str
    created_at: datetime

@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    eval_id: str
    host_composition: dict[str, Any]      # live composition at run start
    agent_spec_snapshot: dict[str, Any]   # per-agent spec state at run start
    status: str                           # pending|running|completed|failed
    created_at: datetime
    completed_at: datetime | None

@dataclass(frozen=True)
class CriterionScore:
    score: int
    rationale: str

@dataclass(frozen=True)
class ExperimentRun:
    run_id: str
    experiment_id: str
    row_id: str
    input: str                           # denormalized
    actual_output: str | None
    weighted_score: float | None         # pre-computed: sum(criterion.weight * score)
    scores: dict[str, CriterionScore]   # keyed by criterion name
    created_at: datetime
```

---

## Postgres Schema (`migrations/`)

Six tables; cascading deletes from `eval` propagate everywhere. The effective schema below reflects migration `005_experiment_refactor.sql`, which moved the host state snapshot from the eval to the experiment and dropped stored deltas.

```sql
CREATE TABLE IF NOT EXISTS eval (
    eval_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    user_guidance TEXT NOT NULL DEFAULT '',
    dataset_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS eval_host_id_idx ON eval (host_id);

CREATE TABLE IF NOT EXISTS eval_dataset (
    dataset_id TEXT PRIMARY KEY,
    eval_id TEXT NOT NULL REFERENCES eval(eval_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_dataset_row (
    row_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES eval_dataset(dataset_id) ON DELETE CASCADE,
    input TEXT NOT NULL,
    scenario_description TEXT NOT NULL,
    sampling_category TEXT NOT NULL,
    expected_behavior TEXT NOT NULL,
    target_agents JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_rubric (
    rubric_id TEXT PRIMARY KEY,
    eval_id TEXT NOT NULL REFERENCES eval(eval_id) ON DELETE CASCADE,
    global_scoring_prompt TEXT NOT NULL,
    criteria JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_experiment (
    experiment_id TEXT PRIMARY KEY,
    eval_id TEXT NOT NULL REFERENCES eval(eval_id) ON DELETE CASCADE,
    host_composition JSONB NOT NULL DEFAULT '{}',
    agent_spec_snapshot JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS eval_experiment_eval_id_idx ON eval_experiment (eval_id);

CREATE TABLE IF NOT EXISTS eval_experiment_run (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES eval_experiment(experiment_id) ON DELETE CASCADE,
    row_id TEXT NOT NULL,
    input TEXT NOT NULL,
    actual_output TEXT,
    weighted_score NUMERIC(5,4),
    scores JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS eval_run_experiment_id_idx ON eval_experiment_run (experiment_id);
```

---

## Loaders (`postgres/loaders/`)

One file per table, each containing both read and write SQL as free `async def fn(pool, ...) -> T` functions plus co-located row mapper functions. Same call convention as the existing `src/mash/runtime/events/store/postgres/loaders.py`.

Dynamic filter building pattern used throughout:
```python
clauses = ["host_id = %s"]
params: list[Any] = [host_id]
if status:
    clauses.append("status = %s")
    params.append(status)
query = f"SELECT ... FROM ... WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT %s OFFSET %s"
params.extend([limit, offset])
```

### `loaders/eval.py`
```python
async def insert_eval(pool, *, eval_id, host_id, user_guidance, dataset_id, rubric_id) -> Eval
async def list_evals(pool, *, host_id=None, limit=50, offset=0) -> list[Eval]
async def get_eval(pool, eval_id) -> Eval | None
async def delete_eval(pool, eval_id) -> bool

def row_to_eval(row: dict) -> Eval
```

### `loaders/dataset.py`
```python
async def insert_dataset(pool, *, dataset_id, eval_id) -> str           # returns dataset_id
async def insert_dataset_rows(pool, dataset_id, rows: list[dict]) -> list[DatasetRow]
async def get_dataset_rows(pool, dataset_id) -> list[DatasetRow]

def row_to_dataset_row(row: dict) -> DatasetRow                         # parses target_agents JSONB
```

### `loaders/rubric.py`
```python
async def insert_rubric(pool, *, rubric_id, eval_id, global_scoring_prompt, criteria) -> ScoringRubric
async def get_rubric(pool, rubric_id) -> ScoringRubric | None
async def update_rubric_criteria(pool, rubric_id, criteria) -> ScoringRubric

def row_to_rubric(row: dict) -> ScoringRubric                           # parses criteria JSONB → list[ScoringCriterion]
```

### `loaders/experiment.py`
```python
async def insert_experiment(pool, *, experiment_id, eval_id, host_composition, agent_spec_snapshot) -> Experiment
async def list_experiments(pool, eval_id, *, limit=20, offset=0) -> list[Experiment]
async def get_experiment(pool, experiment_id) -> Experiment | None
async def update_experiment_status(pool, experiment_id, status, *, completed_at=None) -> None

def row_to_experiment(row: dict) -> Experiment                          # parses host_composition / agent_spec_snapshot JSONB
```

### `loaders/run.py`
```python
async def upsert_run(pool, run: ExperimentRun) -> ExperimentRun
async def list_runs(pool, experiment_id, *, limit=100, offset=0) -> list[ExperimentRun]

def row_to_run(row: dict) -> ExperimentRun                             # parses scores JSONB → dict[str, CriterionScore]
```

---

## Store (`postgres/store.py`)

`PostgresEvalStore` follows the `PostgresRuntimeStore` pattern exactly:
- Constructor stores `database_url`, sets `_pool = None`, `_open_lock = asyncio.Lock()`
- `async open()` — lazy `AsyncConnectionPool(min_size=1, max_size=5, autocommit=True, row_factory=dict_row)`, runs `migrations.run(pool)`
- `async close()` — drains pool
- Every public method calls `await self.open()` then delegates to the appropriate loader module

---

## Service (`service.py`)

`EvalService` is a thin orchestration layer over the store. Follows `WorkflowService` pattern.

```python
class EvalService:
    def __init__(self, store: PostgresEvalStore) -> None

    # Read — called by API routes
    async def list_evals(self, *, host_id=None, limit=50, offset=0) -> list[Eval]
    async def get_eval_detail(self, eval_id) -> dict    # Eval + rows + rubric + derived `locked` flag
    async def is_eval_locked(self, eval_id) -> bool     # true once the eval has an experiment
    async def list_experiments(self, eval_id, *, limit=20, offset=0) -> list[Experiment]
    async def get_experiment_summary(self, experiment_id) -> dict  # Experiment + aggregate scores
    async def compare_experiments(self, eval_id, *, baseline_id, control_id) -> dict
    async def list_runs(self, experiment_id, *, limit=100, offset=0) -> list[ExperimentRun]

    # Write — called by masher workflow tasks via MasherContext injection
    async def persist_eval(self, *, host_id, user_guidance, dataset_rows, rubric) -> Eval
    async def persist_experiment(self, *, eval_id, host_composition, agent_spec_snapshot) -> Experiment
    async def persist_run(self, run: ExperimentRun) -> ExperimentRun
    async def update_experiment_status(self, experiment_id, status, *, completed_at=None) -> None

    # Admin
    async def update_rubric(self, eval_id, *, criteria: list[dict]) -> ScoringRubric  # raises EvalLockedError once locked
    async def delete_eval(self, eval_id) -> bool
```

`get_eval_detail` and `get_experiment_summary` fan out to multiple loaders and assemble the combined response dict. Aggregate experiment scores (mean, per-criterion breakdown) are derived at query time from `ExperimentRun.weighted_score` and `scores` — no stored aggregate column.

`compare_experiments` is the read-time comparison: it validates both experiments belong to the eval, diffs their `agent_spec_snapshot`s with `diff_agent_specs` (a pure function in `service.py`), returns both aggregates side by side, and pairs runs by `row_id` ranked by absolute score movement. Nothing about a comparison is stored.

---

## API Endpoints (`src/mash/api/routes/evals.py`)

`build_evals_router() -> APIRouter` factory — same pattern as every other route module.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/evals` | List evals. Query params: `host_id`, `limit`, `offset` |
| `GET` | `/evals/{eval_id}` | Eval detail: eval + dataset rows + rubric |
| `DELETE` | `/evals/{eval_id}` | Delete eval and cascade to all experiments/runs |
| `PUT` | `/evals/{eval_id}/rubric` | Update rubric criteria; `409 EVAL_LOCKED` once the eval has experiments |
| `GET` | `/evals/{eval_id}/experiments` | List experiments for an eval |
| `GET` | `/evals/{eval_id}/experiments/compare?baseline=&control=` | Read-time comparison: spec delta, aggregates, per-row deltas |
| `GET` | `/evals/{eval_id}/experiments/{experiment_id}` | Experiment summary + aggregate scores |
| `GET` | `/evals/{eval_id}/experiments/{experiment_id}/runs` | Paginated run results |

**No `POST /evals`** — eval creation is triggered via the existing workflow endpoint:
```
POST /api/v1/workflows/gen-synthetic-evals/runs   { "input": { "host_id": "...", "user_guidance": "...", "row_count": 20 } }
POST /api/v1/workflows/score-evals/runs           { "input": { "eval_id": "..." } }
```
`row_count` is optional (default 20, max 100); the persistence tool rejects a dataset whose size does not match it, so the dataset size is deterministic.
The masher workflow tasks call `EvalService.persist_eval` / `persist_experiment` directly.

Pydantic request model (local to the route file):
```python
class UpdateRubricRequest(BaseModel):
    criteria: list[dict[str, Any]] = Field(min_length=1)
```

All responses use the existing `success(data)` envelope. Errors raise `APIError` (e.g. 404 on missing eval).

---

## App Wiring

**`AppRuntimeState` in `src/mash/api/routes/common.py`** — add one field:
```python
eval_service: EvalService | None   # None when MASH_DATABASE_URL is not set
```

**`src/mash/api/app.py`** — in `create_app`:
- Initialize `PostgresEvalStore(database_url)` and `EvalService(store)` when `database_url` is set
- Add to lifespan open/close
- Include `build_evals_router()`

**`src/mash/agents/masher/tool.py`** — add `eval_service: EvalService | None` to `MasherContext`. The masher `persist-eval` and `persist-experiment` workflow tasks call this directly.

**`src/mash/agents/masher/spec.py`** — register `gen-synthetic-evals` and `score-evals` `WorkflowSpec` stubs (task IDs wired; skill implementations are a follow-on).

---

## Admin UI Contract

The SPA is a dumb client — no business logic. All Evals tab views map 1:1 to API calls:

| View | API calls |
|---|---|
| Evals list | `GET /evals?host_id=<current>` |
| Generate evals | `POST /workflows/gen-synthetic-evals/runs` → poll `GET /workflows/.../runs/{id}` |
| Eval detail | `GET /evals/{eval_id}` |
| Edit rubric weights | `PUT /evals/{eval_id}/rubric` |
| Run experiment | `GET /hosts/{host_id}/snapshot` (preview of what will be evaluated), then `POST /workflows/score-evals/runs` → poll |
| Experiments list | `GET /evals/{eval_id}/experiments` |
| Experiment detail | `GET /evals/{eval_id}/experiments/{exp_id}` |
| Compare experiments | `GET /evals/{eval_id}/experiments/compare?baseline=&control=` |
| Run results | `GET /evals/{eval_id}/experiments/{exp_id}/runs` |
| Delete eval | `DELETE /evals/{eval_id}` |

Workflow polling reuses the pattern already implemented in the Workflows tab.

---

## Phases

### Phase 1 — Core models + DB schema + loaders
**Scope:** `src/mash/evals/models.py`, `postgres/migrations.py`, `postgres/loaders/` (all five files).  
**Done when:** `uv run python -c "from mash.evals.postgres import loaders"` imports cleanly; migrations apply against a local Postgres with all six tables and indexes created.

### Phase 2 — Store + Service
**Scope:** `postgres/store.py`, `service.py`, `evals/__init__.py`.  
**Done when:** `PostgresEvalStore.open()` runs migrations; `EvalService.persist_eval(...)` round-trips through the store and returns a valid `Eval`.

### Phase 3 — API endpoints
**Scope:** `src/mash/api/routes/evals.py`.  
**Done when:** All seven endpoints return correct shapes; `GET /evals` on empty DB returns `{"data": {"evals": [], "total": 0}}`; `DELETE /evals/{id}` cascades; `PUT /evals/{id}/rubric` updates and returns the updated rubric.

### Phase 4 — App wiring
**Scope:** `AppRuntimeState` in `common.py`, `create_app` in `app.py`.  
**Done when:** `mash host serve` starts cleanly, all six tables exist, and the eval routes are live.

### Phase 5 — Masher integration
**Scope:** `MasherContext` in `tool.py`, `WorkflowSpec` stubs in `spec.py`.  
**Done when:** `gen-synthetic-evals` and `score-evals` workflows are registered (stubs), and `MasherContext` carries `eval_service`.

### Phase 6 — Admin UI
**Scope:** New Evals tab in the SPA (frontend build).  
**Done when:** All eight views render correctly against a running server with seed eval data; no API calls are made outside the endpoint list above.
