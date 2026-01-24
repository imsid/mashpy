"""Signal collection for feedback loops."""

from __future__ import annotations

from typing import Any, Callable, Dict


SignalCollectorFunc = Callable[[Dict[str, Any]], Any]


class SignalCollector:
    """Collector for tracking signals from agent interactions."""

    def __init__(self) -> None:
        """Initialize signal collector."""
        self._collectors: Dict[str, SignalCollectorFunc] = {}

    def register_signal(
        self,
        name: str,
        collector: SignalCollectorFunc,
    ) -> None:
        """Register a signal collector function.

        Args:
            name: Signal name (e.g., "user_continued", "response_time").
            collector: Function that extracts the signal value from an event.
                       Should take a dict with keys: context, action, results.
                       Should return a numeric value or None.

        Example:
            ```python
            signals = SignalCollector()

            # Track if user continued conversation (1 or 0)
            signals.register_signal("user_continued", lambda event:
                1 if len(event["context"].messages) > 2 else 0
            )

            # Track response time (lower is better, so negate)
            signals.register_signal("response_time", lambda event:
                -event.get("duration_ms", 0)
            )

            # Track tool diversity
            signals.register_signal("tool_diversity", lambda event:
                len(set(tc.name for tc in event["action"].tool_calls))
            )
            ```
        """
        if not name:
            raise ValueError("Signal name cannot be empty")
        if not callable(collector):
            raise ValueError("Signal collector must be callable")

        self._collectors[name] = collector

    def unregister_signal(self, name: str) -> None:
        """Unregister a signal collector.

        Args:
            name: Signal name to remove.
        """
        self._collectors.pop(name, None)

    def collect(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Collect all registered signals from an event.

        Args:
            event: Event dictionary with context, action, and results.

        Returns:
            Dictionary mapping signal names to their values.
        """
        signals: Dict[str, Any] = {}

        for name, collector in self._collectors.items():
            try:
                value = collector(event)
                if value is not None:
                    signals[name] = value
            except Exception as e:
                # Log error but don't fail collection
                # In production, would use proper logging
                pass

        return signals

    def list_signals(self) -> list[str]:
        """List all registered signal names.

        Returns:
            List of signal names.
        """
        return list(self._collectors.keys())

    def __len__(self) -> int:
        """Get number of registered signals."""
        return len(self._collectors)

    def __contains__(self, name: str) -> bool:
        """Check if a signal is registered."""
        return name in self._collectors
