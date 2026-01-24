"""Example ranking for learning from high-signal interactions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .store import ConversationStore


class ExampleRanker:
    """Ranks conversation examples by combining similarity and signal scores."""

    def __init__(
        self,
        store: ConversationStore,
        signal_weights: Optional[Dict[str, float]] = None,
    ) -> None:
        """Initialize example ranker.

        Args:
            store: Conversation store to query.
            signal_weights: Weights for each signal (default: equal weights).
                           Example: {"user_continued": 0.5, "response_time": 0.3, "tool_diversity": 0.2}
        """
        self.store = store
        self.signal_weights = signal_weights or {}

    def get_best_examples(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.3,
        min_signal_score: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Get best examples based on similarity and signals.

        The ranking algorithm combines:
        1. Semantic similarity to the query
        2. Weighted signal scores

        Final score = similarity * signal_score

        Args:
            query: Query text to find similar examples.
            limit: Maximum number of examples to return.
            min_similarity: Minimum similarity threshold.
            min_signal_score: Minimum signal score threshold.

        Returns:
            List of examples sorted by final score.
        """
        # Generate query embedding (placeholder)
        query_embedding = self._generate_embedding(query)
        if not query_embedding:
            return []

        # Search for similar conversations
        candidates = self.store.search_by_similarity(
            query_embedding=query_embedding,
            limit=limit * 3,  # Get more candidates to filter
        )

        # Score and rank candidates
        scored_examples = []
        for candidate in candidates:
            similarity = candidate.get("similarity", 0.0)

            # Skip if below similarity threshold
            if similarity < min_similarity:
                continue

            # Calculate signal score
            signal_score = self._calculate_signal_score(candidate.get("signals", {}))

            # Skip if below signal score threshold
            if signal_score < min_signal_score:
                continue

            # Calculate final score
            final_score = similarity * signal_score

            scored_examples.append(
                {
                    "turn_id": candidate.get("turn_id"),
                    "user_message": candidate.get("user_message"),
                    "agent_response": candidate.get("agent_response"),
                    "similarity": similarity,
                    "signal_score": signal_score,
                    "final_score": final_score,
                    "signals": candidate.get("signals", {}),
                }
            )

        # Sort by final score (descending)
        scored_examples.sort(key=lambda x: x["final_score"], reverse=True)

        return scored_examples[:limit]

    def _calculate_signal_score(self, signals: Dict[str, float]) -> float:
        """Calculate weighted signal score.

        Args:
            signals: Signal values for a turn.

        Returns:
            Weighted signal score (0.0 to 1.0).
        """
        if not signals:
            return 0.5  # Default neutral score

        # If no weights specified, use equal weights
        if not self.signal_weights:
            # Simple average of normalized signals
            return sum(signals.values()) / len(signals) if signals else 0.5

        # Calculate weighted score
        weighted_sum = 0.0
        total_weight = 0.0

        for signal_name, weight in self.signal_weights.items():
            if signal_name in signals:
                value = signals[signal_name]
                # Normalize value to 0-1 range (assuming signals are already normalized)
                normalized_value = self._normalize_signal_value(value)
                weighted_sum += normalized_value * weight
                total_weight += weight

        if total_weight == 0:
            return 0.5

        return weighted_sum / total_weight

    def _normalize_signal_value(self, value: float) -> float:
        """Normalize a signal value to 0-1 range.

        Args:
            value: Raw signal value.

        Returns:
            Normalized value between 0 and 1.
        """
        # For now, assume signals are already in reasonable ranges
        # In production, would track signal distributions and normalize accordingly

        # Simple sigmoid normalization
        if value >= 0:
            # Positive signals: map to 0.5-1.0
            return 0.5 + 0.5 * (1.0 / (1.0 + abs(value)))
        else:
            # Negative signals: map to 0.0-0.5
            return 0.5 * (1.0 / (1.0 + abs(value)))

    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding for text.

        This is a placeholder. In production, use a real embedding model.
        """
        # Placeholder: return None for now
        # In production: call embedding API or model
        return None

    def update_weights(self, signal_weights: Dict[str, float]) -> None:
        """Update signal weights.

        Args:
            signal_weights: New weights for signals.
        """
        self.signal_weights = signal_weights

    def get_weights(self) -> Dict[str, float]:
        """Get current signal weights.

        Returns:
            Dictionary of signal weights.
        """
        return self.signal_weights.copy()
