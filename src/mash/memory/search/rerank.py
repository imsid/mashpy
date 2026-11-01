"""Reranking for hybrid memory search."""

from __future__ import annotations

from typing import Any

from .types import MAX_PREVIEW_CHARS, FusedHit, FusionWeights, RetrievalHit


def _cap_preview(preview: str) -> str:
    """Cap preview to the configured max length."""
    return str(preview)[:MAX_PREVIEW_CHARS]


class WeightedFusionReranker:
    """Weighted fusion reranker for keyword + semantic results."""

    def __init__(self, weights: FusionWeights | None = None) -> None:
        self._weights = weights or FusionWeights()
        if self._weights.semantic_weight < 0 or self._weights.keyword_weight < 0:
            raise ValueError("Fusion weights must be non-negative")
        if self._weights.semantic_weight == 0 and self._weights.keyword_weight == 0:
            raise ValueError("At least one fusion weight must be positive")

    async def rerank(
        self,
        *,
        keyword_hits: list[RetrievalHit],
        semantic_hits: list[RetrievalHit],
    ) -> list[FusedHit]:
        """Fuse hits by turn ID using weighted score combination."""
        accum: dict[str, dict[str, Any]] = {}

        for hit in semantic_hits:
            entry = accum.setdefault(
                hit.trace_id,
                {
                    "session_id": hit.session_id,
                    "semantic_score": 0.0,
                    "keyword_score": 0.0,
                    "semantic_preview": "",
                    "keyword_preview": "",
                },
            )
            if str(entry["session_id"]) != hit.session_id:
                raise ValueError(
                    f"Conflicting session_id for trace_id {hit.trace_id}: "
                    f"{entry['session_id']} vs {hit.session_id}"
                )
            entry["semantic_score"] = max(float(entry["semantic_score"]), hit.score)
            if hit.preview and not entry["semantic_preview"]:
                entry["semantic_preview"] = _cap_preview(hit.preview)

        for hit in keyword_hits:
            entry = accum.setdefault(
                hit.trace_id,
                {
                    "session_id": hit.session_id,
                    "semantic_score": 0.0,
                    "keyword_score": 0.0,
                    "semantic_preview": "",
                    "keyword_preview": "",
                },
            )
            if str(entry["session_id"]) != hit.session_id:
                raise ValueError(
                    f"Conflicting session_id for trace_id {hit.trace_id}: "
                    f"{entry['session_id']} vs {hit.session_id}"
                )
            entry["keyword_score"] = max(float(entry["keyword_score"]), hit.score)
            # Prefer keyword preview when available.
            if hit.preview:
                entry["keyword_preview"] = _cap_preview(hit.preview)

        fused: list[FusedHit] = []
        for trace_id, entry in accum.items():
            semantic_score = float(entry["semantic_score"])
            keyword_score = float(entry["keyword_score"])
            final_score = (
                self._weights.semantic_weight * semantic_score
                + self._weights.keyword_weight * keyword_score
            )
            preview = entry["keyword_preview"] or entry["semantic_preview"] or ""
            fused.append(
                FusedHit(
                    trace_id=trace_id,
                    session_id=str(entry["session_id"]),
                    final_score=final_score,
                    preview=_cap_preview(preview),
                    semantic_score=semantic_score,
                    keyword_score=keyword_score,
                )
            )

        fused.sort(
            key=lambda item: (
                -item.final_score,
                -item.semantic_score,
                -item.keyword_score,
                item.trace_id,
            )
        )
        return fused
