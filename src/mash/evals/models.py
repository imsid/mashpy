from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ScoringCriterion:
    name: str
    description: str
    weight: float
    scoring_prompt: str
    scale_min: int = 1
    scale_max: int = 5


@dataclass(frozen=True)
class ScoringRubric:
    rubric_id: str
    eval_id: str
    global_scoring_prompt: str
    criteria: tuple[ScoringCriterion, ...]
    updated_at: datetime


@dataclass(frozen=True)
class DatasetRow:
    row_id: str
    dataset_id: str
    input: str
    scenario_description: str
    sampling_category: str
    expected_behavior: str
    target_agents: tuple[str, ...]


@dataclass(frozen=True)
class Eval:
    eval_id: str
    host_id: str
    user_guidance: str
    host_composition: dict[str, Any]
    agent_spec_baseline: dict[str, Any]
    dataset_id: str
    rubric_id: str
    created_at: datetime


@dataclass(frozen=True)
class AgentSpecDelta:
    agent_id: str
    system_prompt_changed: bool
    tools_added: tuple[str, ...]
    tools_removed: tuple[str, ...]
    llm_model_changed: bool
    mcp_servers_added: tuple[str, ...]
    mcp_servers_removed: tuple[str, ...]


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    eval_id: str
    agent_spec_snapshot: dict[str, Any]
    agent_spec_delta: tuple[AgentSpecDelta, ...]
    status: str
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
    input: str
    actual_output: str | None
    weighted_score: float | None
    scores: dict[str, CriterionScore]
    created_at: datetime
