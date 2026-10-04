"""Opt-in exposed thought summaries for the Admin Thoughts feed."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mushy:
    """Enable provider summary capture without a second model or artifact store.

    Return ``Mushy()`` from ``AgentSpec.build_mushy()``. The provider must
    support ``enable_thought_summaries()``; Admin displays the recorded text.
    """

__all__ = ["Mushy"]
