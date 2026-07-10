"""Masher workflow module and its built-in eval agent."""

from .context import MasherRuntimeContext
from .spec import EVAL_AGENT_ID, EvalAgentSpec, build_eval_agent_metadata
from .workflows import (
    GEN_SYNTHETIC_EVALS_SKILL_NAME,
    GenSyntheticEvalsInput,
    GenSyntheticEvalsResult,
    OnlineEvalResult,
    TraceDigestResult,
    TraceScanInput,
    MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
    MASHER_ONLINE_EVAL_WORKFLOW_ID,
    MASHER_SCORE_EVALS_WORKFLOW_ID,
    MASHER_TRACE_DIGEST_WORKFLOW_ID,
    build_masher_workflows,
)

__all__ = [
    "GEN_SYNTHETIC_EVALS_SKILL_NAME",
    "GenSyntheticEvalsInput",
    "GenSyntheticEvalsResult",
    "EVAL_AGENT_ID",
    "MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID",
    "MASHER_ONLINE_EVAL_WORKFLOW_ID",
    "MASHER_SCORE_EVALS_WORKFLOW_ID",
    "MASHER_TRACE_DIGEST_WORKFLOW_ID",
    "EvalAgentSpec",
    "MasherRuntimeContext",
    "OnlineEvalResult",
    "TraceDigestResult",
    "TraceScanInput",
    "build_masher_workflows",
    "build_eval_agent_metadata",
]
