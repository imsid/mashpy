"""Types for memory search pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

SearchColumn = Literal["user_message", "agent_response"]
SearchMethod = Literal["keyword", "semantic"]

MAX_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class ParsedSearchQuery:
    """Normalized query produced by the parser stage."""

    raw_query: str
    column: SearchColumn
    query_term: str
    keyword_query: str
    semantic_query_text: str
    semantic_embedding: Optional[list[float]]


@dataclass(frozen=True)
class RetrievalHit:
    """A scored hit from an individual retrieval method."""

    trace_id: str
    session_id: str
    score: float
    preview: str
    column: SearchColumn
    method: SearchMethod
    rank: int


@dataclass(frozen=True)
class FusedHit:
    """Combined hit after weighted fusion reranking."""

    trace_id: str
    session_id: str
    final_score: float
    preview: str
    semantic_score: float
    keyword_score: float


@dataclass(frozen=True)
class SearchResult:
    """Public search result returned by the search service."""

    trace_id: str
    session_id: str
    similarity_score: float
    preview: str


@dataclass(frozen=True)
class RetrievalConfig:
    """Feature toggles for retrieval methods."""

    enable_keyword: bool = True
    enable_semantic: bool = False


@dataclass(frozen=True)
class FusionWeights:
    """Weights used by the weighted fusion reranker."""

    semantic_weight: float = 0.7
    keyword_weight: float = 0.3
