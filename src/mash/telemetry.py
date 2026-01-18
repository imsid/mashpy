"""Telemetry helpers for agent workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class TokenUsage:
    """Token usage summary for a request or session."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, usage: "TokenUsage") -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens

    def to_dict(self) -> Dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


class TelemetryCollector:
    """Tracks token usage per session."""

    def __init__(self) -> None:
        self._totals: Dict[str, TokenUsage] = {}

    def record_request(self, session_id: str, usage: TokenUsage) -> None:
        total = self._totals.setdefault(session_id, TokenUsage())
        total.add(usage)

    def session_total(self, session_id: str) -> TokenUsage:
        return self._totals.get(session_id, TokenUsage())
