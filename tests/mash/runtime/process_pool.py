"""A pool for ``mash agent serve`` children spawned by the process tests.

The agent ids (comma-separated) and the model's delay come from the
environment, so each test gets agents of its own in the shared database.
"""

from __future__ import annotations

import os

from mash import HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


def build_pool():
    builder = HostBuilder()
    for agent_id in os.environ["TEST_PROCESS_AGENT_ID"].split(","):
        builder.agent(
            build_spec(
                agent_id=agent_id,
                response_text="process-ok",
                delay_seconds=float(os.environ.get("TEST_PROCESS_DELAY", "0")),
            ),
            metadata=metadata(),
        )
    return builder.build()
