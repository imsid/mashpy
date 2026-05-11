"""Signal collection for feedback loops."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict

SignalCollectorFunc = Callable[[Dict[str, Any]], Any]


@dataclass(frozen=True)
class SignalDefinition:
    """Typed metadata describing one signal exposed by the runtime."""

    name: str
    value_type: str
    description: str
    computed_at: str
    persisted: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return the public JSON-ready representation."""
        return asdict(self)


@dataclass(frozen=True)
class RegisteredSignal:
    """One registered signal definition plus its collector callable."""

    definition: SignalDefinition
    collector: SignalCollectorFunc


class SignalCollector:
    """Collector for tracking signals from agent interactions."""

    def __init__(self) -> None:
        """Initialize signal collector."""
        self._collectors: Dict[str, RegisteredSignal] = {}

    def register_signal(
        self,
        definition: SignalDefinition | str,
        collector: SignalCollectorFunc,
        *,
        value_type: str = "unknown",
        description: str = "",
        computed_at: str = "turn_complete",
        persisted: bool = True,
    ) -> None:
        """Register a signal collector function.

        Args:
            definition: Signal definition or signal name.
            collector: Function that extracts the signal value from an event.
                       Should take a dict with keys: context, action, results.
                       Should return a value or None.

        Example:
            ```python
            signals = SignalCollector()

            # Track if user continued conversation (1 or 0)
            signals.register_signal(SignalDefinition(
                name="user_continued",
                value_type="integer",
                description="Whether the user continued the conversation.",
                computed_at="turn_complete",
                persisted=True,
            ), lambda event:
                1 if len(event["context"].messages) > 2 else 0
            )

            # Track response time (lower is better, so negate)
            signals.register_signal(SignalDefinition(
                name="response_time",
                value_type="integer",
                description="Negated response time in milliseconds.",
                computed_at="turn_complete",
                persisted=True,
            ), lambda event:
                -event.get("duration_ms", 0)
            )

            # Track tool diversity
            signals.register_signal(SignalDefinition(
                name="tool_diversity",
                value_type="integer",
                description="Count of distinct tools used in the trace.",
                computed_at="turn_complete",
                persisted=True,
            ), lambda event:
                len(set(tc.name for tc in event["action"].tool_calls))
            )
            ```
        """
        if isinstance(definition, SignalDefinition):
            resolved = definition
        else:
            name = str(definition or "").strip()
            resolved = SignalDefinition(
                name=name,
                value_type=value_type,
                description=description,
                computed_at=computed_at,
                persisted=bool(persisted),
            )
        if not resolved.name:
            raise ValueError("Signal name cannot be empty")
        if not callable(collector):
            raise ValueError("Signal collector must be callable")

        self._collectors[resolved.name] = RegisteredSignal(
            definition=resolved,
            collector=collector,
        )

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

        for name, registered in self._collectors.items():
            try:
                value = registered.collector(event)
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

    def list_signal_definitions(self) -> list[SignalDefinition]:
        """List all registered signal definitions."""
        return [
            registered.definition
            for registered in self._collectors.values()
        ]

    def get_signal_definitions(self) -> Dict[str, Dict[str, Any]]:
        """Return signal definitions keyed by signal name."""
        return {
            definition.name: definition.to_dict()
            for definition in self.list_signal_definitions()
        }

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
    collector.register_signal(
        SignalDefinition(
            name="unused_tools",
            value_type="string_list",
            description=(
                "Tools offered to the model but never invoked during the "
                "completed trace."
            ),
            computed_at="turn_complete",
            persisted=True,
        ),
        collect_unused_tools,
    )
    collector.register_signal(
        SignalDefinition(
            name="unused_tool_tokens",
            value_type="integer",
            description=(
                "Estimated token footprint of tool definitions offered but "
                "never invoked during the completed trace."
            ),
            computed_at="turn_complete",
            persisted=True,
        ),
        collect_unused_tool_tokens,
    )
    return collector
