"""Eval-agent judging: score one agent output against an eval rubric.

The eval scoring orchestration (``score_runner``) runs each dataset row through
the host under test, then calls the eval agent once per row with a self-contained judge
message and this structured-output contract. Judging is the only LLM step in
scoring; loading rows, running the host, and persistence are deterministic code.
"""

from __future__ import annotations

import json
from typing import Any

from ...evals.models import CriterionScore

# json_text wrapper mirrors the other eval workflow outputs: dynamic criterion
# names can't be expressed as a closed provider schema, so the model returns a
# serialized JSON string we parse ourselves.
EVAL_JUDGE_STRUCTURED_OUTPUT = {
    "title": "EvalJudgeRowOutput",
    "type": "object",
    "properties": {
        "json_text": {
            "type": "string",
            "description": (
                "A serialized JSON object with 'scores' (a map of rubric "
                "criterion name to {score, rationale}) and 'weighted_score'."
            ),
        }
    },
    "required": ["json_text"],
    "additionalProperties": False,
}


class JudgeError(ValueError):
    """Raised when judge output cannot be parsed into a valid scored row."""


def build_judge_message(
    *, row_input: str, actual_output: str | None, rubric: dict[str, Any]
) -> str:
    """Build a self-contained judge prompt for one row."""
    criteria = rubric.get("criteria") or []
    lines: list[str] = [
        "Score one agent output against the rubric below. Judge only what the "
        "output demonstrates; do not run the task yourself.",
        "",
    ]
    global_prompt = str(rubric.get("global_scoring_prompt") or "").strip()
    if global_prompt:
        lines += ["Global scoring guidance:", global_prompt, ""]

    lines.append("Criteria (score each once, on its integer scale):")
    for c in criteria:
        name = str(c.get("name") or "").strip()
        scale_min = c.get("scale_min", 1)
        scale_max = c.get("scale_max", 5)
        weight = c.get("weight")
        prompt = str(c.get("scoring_prompt") or c.get("description") or "").strip()
        lines.append(
            f"- {name} (weight {weight}, scale {scale_min}-{scale_max}): {prompt}"
        )
    lines += [
        "",
        "Test input:",
        row_input,
        "",
        "Agent output:",
        actual_output if actual_output else "(the agent produced no output)",
        "",
        "Return json_text: a JSON object with:",
        '  "scores": { "<criterion name>": { "score": <int in scale>, '
        '"rationale": "<one sentence>" }, ... },',
        '  "weighted_score": <number>',
        "Include every listed criterion exactly once.",
    ]
    return "\n".join(lines)


def parse_judge_output(
    json_text: str, rubric: dict[str, Any]
) -> tuple[dict[str, CriterionScore], float]:
    """Parse and validate judge output against the rubric.

    Returns ``(scores, weighted_score)``. ``weighted_score`` is recomputed from
    the rubric weights and the model's per-criterion scores (authoritative), not
    taken from the model's own arithmetic. Raises :class:`JudgeError` on any
    missing/invalid criterion score.
    """
    try:
        parsed = json.loads(json_text)
    except (TypeError, ValueError) as exc:
        raise JudgeError(f"judge output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise JudgeError("judge output must be a JSON object")
    raw_scores = parsed.get("scores")
    if not isinstance(raw_scores, dict):
        raise JudgeError("judge output 'scores' must be an object")

    criteria = rubric.get("criteria") or []
    scores: dict[str, CriterionScore] = {}
    weighted_score = 0.0
    for c in criteria:
        name = str(c.get("name") or "").strip()
        entry = raw_scores.get(name)
        if not isinstance(entry, dict):
            raise JudgeError(f"missing score for criterion '{name}'")
        try:
            score = int(entry.get("score", ""))
        except (TypeError, ValueError) as exc:
            raise JudgeError(f"criterion '{name}' score must be an integer") from exc
        scale_min = int(c.get("scale_min", 1))
        scale_max = int(c.get("scale_max", 5))
        score = max(scale_min, min(scale_max, score))
        rationale = entry.get("rationale")
        rationale = rationale.strip() if isinstance(rationale, str) else ""
        scores[name] = CriterionScore(score=score, rationale=rationale)
        try:
            weight = float(c.get("weight", 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        weighted_score += weight * score
    if not scores:
        raise JudgeError("rubric has no criteria to score")
    return scores, weighted_score
