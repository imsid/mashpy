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
                       Should return a value or None. Non-numeric values can still
                       be surfaced in live responses, but numeric values are the
                       only ones persisted in the SQLite signals table.

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
            except Exception:
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


def collect_unused_tools(event: Dict[str, Any]) -> list[str]:
    """Return sorted tool names offered to the model but never called."""
    tool_usage = event.get("tool_usage")
    if not isinstance(tool_usage, dict):
        return []

    unused = []
    for name, entry in tool_usage.items():
        if not isinstance(entry, dict):
            continue
        try:
            invocations = int(entry.get("invocations", 0))
        except (TypeError, ValueError):
            invocations = 0
        if invocations == 0:
            unused.append(str(name))
    return sorted(unused)


def collect_unused_tool_tokens(event: Dict[str, Any]) -> int:
    """Return the summed token estimate for tools never called."""
    tool_usage = event.get("tool_usage")
    if not isinstance(tool_usage, dict):
        return 0

    total = 0
    for entry in tool_usage.values():
        if not isinstance(entry, dict):
            continue
        try:
            invocations = int(entry.get("invocations", 0))
        except (TypeError, ValueError):
            invocations = 0
        if invocations != 0:
            continue
        try:
            total += int(entry.get("tokens", 0))
        except (TypeError, ValueError):
            continue
    return total


def build_default_signal_collector() -> SignalCollector:
    """Build the default signal collector used by hosted runtimes."""
    collector = SignalCollector()
    collector.register_signal("unused_tools", collect_unused_tools)
    collector.register_signal("unused_tool_tokens", collect_unused_tool_tokens)
    return collector
