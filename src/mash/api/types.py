"""Public types for Mash host composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Union

from mash.runtime import MashAgentHost, MashAgentHostBuilder


HostFactory = Callable[[], Union[MashAgentHost, MashAgentHostBuilder]]


@dataclass(frozen=True)
class MashHostApp:
    """Host application entrypoint payload used by the host CLI loader."""

    factory: HostFactory
