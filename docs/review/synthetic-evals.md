# Synthetic Evals

Status: Draft

Last Updated: 2026-07-02

## What We Are Solving

Before an agent sees any real traffic, there are no traces to learn from. A developer who has just defined a host composition has declared what the agent is supposed to do — its capabilities, its tools, its subagents — but has no empirical signal on whether it actually does those things well. This is the cold-start problem: you want to evaluate your agent before rolling it out, and you have nothing to evaluate against.

Synthetic evals address this directly. Given a host composition and some developer guidance, the system generates a representative dataset of test cases and a scoring rubric derived from the host's declared capabilities. The developer can then run that dataset through the host at any point and get scored results — before the first user ever sends a message.

Synthetic evals are also useful beyond cold start. As a host evolves, there will always be scenarios it has not yet encountered in production. A synthetic dataset can be deliberately seeded to cover edge cases, capability boundaries, and multi-agent paths that organic traffic may take weeks to surface.

This is not a substitute for evaluating against live traces, which carry their own value — real distribution, real user intent, real failure modes. Synthetic and live evals are complementary. This document covers synthetic evals only.

The system lives entirely inside Mash — as workflow specs within the masher agent — with no external eval infrastructure required.

---

## Concepts

### Host

A host is a named composition of agents: one primary and zero or more subagents. It is the unit of deployment and the unit of evaluation. When a user request arrives, the primary agent handles it and can delegate to subagents via the `InvokeSubagent` tool.

```
Host: travel-assistant
  primary:   TravelConcierge
  subagents: [FlightSearch, HotelSearch]
```

### AgentSpec

An AgentSpec is the implementation of one agent: its system prompt, tools, LLM model, and MCP servers. AgentSpecs evolve — developers change system prompts, add or remove tools. The host composition (who is in the host and in what role) stays the same; the AgentSpec of each member can change independently.

### Dataset

A dataset is a set of up to 100 synthetic test cases generated for a specific host composition. Each row describes one scenario: what the user input is, which capability it exercises, which agents should be involved, and what good behavior looks like. The dataset is not a list of expected outputs — it is a structured description of intent that the scoring rubric operates against.

### Scoring Rubric

A rubric is a set of weighted criteria derived from the host's declared capabilities and the developer's guidance. Each criterion has a name, a description of what it measures, a weight, and a specific scoring prompt for the LLM judge. The rubric is the stable reference for what "good" means for this host — it does not change between experiments.

### eval_id

An eval bundles three things together under one ID: the dataset, a snapshot of the host composition, and a snapshot of the AgentSpec of every agent in that composition at the time the eval was created. This frozen AgentSpec state is the baseline — the "before" against which future experiments are compared.

An eval is created by running the `gen-synthetic-evals` workflow. Each run produces a new eval_id.

### experiment_id

An experiment is one execution of a dataset against the host, with scoring. When score-evals runs, it loads the dataset and rubric from the eval, snapshots the current AgentSpec of every agent in the frozen composition, computes the delta from the eval baseline, runs every dataset input through the host, and scores the outputs. The result — including the AgentSpec snapshot, the delta, per-row scores, and an aggregate — is persisted under a new experiment_id.

An eval_id can have many experiment_ids. Each experiment_id represents the system's state at one point in time.

---

## How the Concepts Relate

```
Host
 └── composition (frozen in eval_id)
      ├── primary AgentSpec   ─┐
      └── subagent AgentSpecs ─┴── baseline snapshot (frozen in eval_id)
                                    └── current snapshot (captured per experiment_id)
                                         └── delta (baseline → current)

eval_id
 ├── host_composition (frozen)
 ├── agent_spec_baseline (frozen)
 ├── dataset (up to 100 rows)
 └── scoring_rubric

experiment_id
 ├── eval_id (FK)
 ├── agent_spec_snapshot (current)
 ├── agent_spec_delta (diff from baseline)
 └── experiment_runs (one per dataset row)
      ├── row_id (FK → dataset)
      ├── actual_output
      ├── weighted_score
      └── scores (per criterion: score + rationale)
```

The key invariants:

- The host composition (who is in the host) is fixed per eval_id. score-evals always routes inputs through the same primary and subagents the dataset was designed for.
- The rubric is fixed per eval_id. Scores across experiments are always measured on the same criteria.
- The AgentSpec can change freely between experiments. The delta captures exactly what changed.

---

## Structural Design

### Workflows

Both workflows live inside the masher agent as `WorkflowSpec` definitions.

**`gen-synthetic-evals`**

Triggered from the admin UI Evals tab. Takes user guidance as its initial input message.

| Task | What It Does |
|---|---|
| `snapshot-composition` | Reads the target host's current composition and AgentSpec state |
| `generate-dataset` | SKILL-backed: generates up to 100 synthetic inputs from composition + user guidance |
| `generate-rubric` | SKILL-backed: derives scoring criteria from host capabilities + user guidance |
| `persist-eval` | Writes dataset, rubric, composition snapshot, AgentSpec baseline to Postgres; returns eval_id |

**`score-evals`**

Triggered from the admin UI with an eval_id as input.

| Task | What It Does |
|---|---|
| `load-eval` | Reads dataset, rubric, frozen composition, and AgentSpec baseline from eval_id |
| `snapshot-agent-specs` | Captures current AgentSpec for each agent in the frozen composition; computes delta |
| `run-inputs` | Submits each dataset row to the host; collects outputs (parallelized, bounded by `max_parallel_tools`) |
| `score-outputs` | LLM-as-judge scores each (input, output) pair against rubric criteria |
| `persist-experiment` | Writes results, AgentSpec snapshot, delta to Postgres; returns experiment_id |

### SKILL

The generation SKILL provides masher with detailed instructions for how to read a host composition and produce a high-quality dataset and rubric. It covers: how to infer scenario diversity from declared capabilities, how to generate inputs that exercise multi-agent routing, how to construct rubric criteria that match the host's purpose, and how to weight criteria. The SKILL is the only place generation logic lives — changing it changes generation behavior without touching workflow code.

---

## Data Model

### Dataset Row

```python
{
    "row_id": str,                  # UUID
    "input": str,                   # synthetic user message
    "scenario_description": str,    # which capability or path this tests
    "sampling_category": str,       # random | multi_tool | multi_agent |
                                    # high_tokens | long_running | short_running
    "expected_behavior": str,       # description of what good looks like —
                                    # not a fixed output, a behavioral contract
    "target_agents": list[str],     # which agents should be involved
}
```

`expected_behavior` is a prose description, not a string-match target. For example: "The primary agent should delegate flight queries to FlightSearch, synthesize the results, and respond with at least one concrete option including price and dates." The LLM judge evaluates the actual output against this description.

### Scoring Rubric

```python
{
    "rubric_id": str,               # UUID
    "criteria": [
        {
            "name": str,            # e.g. "task_completion"
            "description": str,     # what this criterion measures
            "weight": float,        # importance weight, sums to 1.0 across criteria
            "scoring_prompt": str,  # specific judge prompt for this criterion
            "scale": {
                "min": 1,
                "max": 5,
            },
        }
    ],
    "global_scoring_prompt": str,   # overall context given to the LLM judge
}
```

Typical criteria for a multi-agent host: `task_completion`, `subagent_coordination`, `tool_selection_accuracy`, `response_quality`, `factual_grounding`. Weights reflect the host's purpose — a research host weights `factual_grounding` higher; a booking host weights `task_completion`. Weights must sum to 1.0 and are editable by the developer after generation.

### Eval

```python
{
    "eval_id": str,                     # UUID
    "created_at": datetime,
    "host_id": str,
    "user_guidance": str,               # developer's seed guidance
    "host_composition": {               # frozen structural snapshot
        "primary": str,                 # agent_id
        "subagents": list[str],         # agent_ids
    },
    "agent_spec_baseline": {            # frozen AgentSpec state per agent
        "<agent_id>": {
            "system_prompt": str,
            "tools": list[str],
            "llm_model": str,
            "mcp_servers": list[str],
        }
    },
    "dataset_id": str,                  # FK → dataset
    "rubric_id": str,                   # FK → rubric
}
```

### Experiment

```python
{
    "experiment_id": str,               # UUID
    "eval_id": str,                     # FK → eval
    "created_at": datetime,
    "agent_spec_snapshot": {            # AgentSpec state at run time, same shape as baseline
        "<agent_id>": { ... }
    },
    "agent_spec_delta": {               # what changed from eval baseline to this run
        "<agent_id>": {
            "system_prompt_changed": bool,
            "tools_added": list[str],
            "tools_removed": list[str],
            "llm_model_changed": bool,
            "mcp_servers_added": list[str],
            "mcp_servers_removed": list[str],
        }
    },
    "status": str,                      # pending | running | complete | failed
}
```

Aggregate scores are not stored on the experiment. They are computed on the fly from `experiment_runs` when the developer selects experiments to compare, using the rubric weights in effect for that eval.

### ExperimentRun

One row per dataset row per experiment. This is the leaf-level record — all scoring data lives here.

```python
{
    "run_id": str,                      # UUID
    "experiment_id": str,               # FK → experiment
    "row_id": str,                      # FK → dataset row
    "input": str,                       # denormalized from dataset row for query convenience
    "actual_output": str,               # full agent response
    "weighted_score": float,            # sum(score * weight) across criteria for this row
    "scores": {
        "<criterion_name>": {
            "score": float,             # 1–5
            "rationale": str,           # judge's explanation
        }
    },
}
```

`weighted_score` per run is pre-computed at score time (it is a deterministic function of `scores` and rubric weights). Experiment-level aggregates — mean, per-criterion breakdown — are derived from `weighted_score` and per-criterion scores across all `experiment_runs` for a given `experiment_id` at query time.

---

## Example End-to-End Flow

**Setup.** A developer has built a travel assistant host:

```
Host: travel-assistant
  primary:   TravelConcierge  (system_prompt: "You are a travel planning assistant...")
  subagents: [FlightSearch, HotelSearch]
```

**Step 1: Generate an eval.**

The developer opens the Evals tab in the admin UI, selects `travel-assistant`, and runs `gen-synthetic-evals` with the following guidance:

> "Users typically ask about round trips, multi-city itineraries, and budget constraints. FlightSearch and HotelSearch should both be exercised."

The workflow snapshots the current composition and AgentSpec state, runs the SKILL, and produces:

- 100 synthetic inputs such as:
  - "Find me a round-trip flight from NYC to Paris under $1000 in mid-August"
  - "Plan a 10-day trip covering Rome, Florence, and Venice with hotels under $150/night"
  - "What's the cheapest way to get from London to Tokyo next month?"
- A rubric with four criteria: `task_completion` (0.4), `subagent_coordination` (0.3), `response_quality` (0.2), `factual_grounding` (0.1)

Result: `eval_id = eval-001`. AgentSpec baseline is frozen.

**Step 2: Run the first experiment (baseline).**

The developer immediately runs `score-evals` with `eval_id = eval-001`. The current AgentSpec matches the baseline exactly, so `agent_spec_delta` is empty. All 100 inputs run through the host; outputs are scored.

Result: `experiment_id = exp-001`. 100 `experiment_runs` written, one per dataset row.

**Step 3: Change the primary agent's system prompt.**

The developer rewrites TravelConcierge's system prompt to explicitly handle budget constraints and multi-city itineraries.

**Step 4: Run a second experiment.**

The developer runs `score-evals` again with `eval_id = eval-001`. The workflow snapshots the current AgentSpec and computes the delta:

```
agent_spec_delta:
  TravelConcierge:
    system_prompt_changed: true
  FlightSearch:  (no changes)
  HotelSearch:   (no changes)
```

The same 100 inputs run through the same frozen host composition. Outputs are scored against the same rubric.

Result: `experiment_id = exp-002`. 100 `experiment_runs` written.

**Step 5: Compare in the Evals tab.**

The developer selects `exp-001` as the baseline and `exp-002` as the control. Aggregate scores and per-criterion breakdowns are computed on the fly from `experiment_runs`:

```
eval-001  travel-assistant  100 rows  2026-07-02

  baseline: exp-001  (no delta from eval — initial run)
  control:  exp-002  (TravelConcierge: system_prompt changed)

  aggregate score         3.6  →  4.1   (+0.5)

  by criterion:
    task_completion       3.4  →  4.2   (+0.8)   weight: 0.4
    subagent_coordination 3.8  →  3.9   (+0.1)   weight: 0.3
    response_quality      3.5  →  4.3   (+0.8)   weight: 0.2
    factual_grounding     3.2  →  3.8   (+0.6)   weight: 0.1
```

The developer then expands the row-level view to see which specific inputs drove the delta. Rows are ranked by absolute score change between the two experiments:

```
  top regressions (baseline → control):
    row-047  "What's the cheapest way to get to Tokyo?"          3.2 → 2.8  (-0.4)
    row-031  "Can I fly business class for under $500?"          2.9 → 2.6  (-0.3)

  top improvements (baseline → control):
    row-003  "Find a round-trip to Paris under $1000"            2.8 → 4.6  (+1.8)
    row-019  "Plan 10 days in Italy with hotels under $150"      3.0 → 4.5  (+1.5)
    row-055  "Multi-city: NYC → London → Rome, cheapest option"  2.5 → 4.1  (+1.6)
```

The row-level view surfaces that the prompt rewrite improved budget and multi-city scenarios significantly but slightly regressed on open-ended "cheapest overall" queries — an actionable signal, not just a number.
