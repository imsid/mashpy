"""A pool for ``mash agent serve`` children spawned by the process tests.

The agent id and the model's delay come from the environment, so each test
gets an agent of its own in the shared database.
"""

from __future__ import annotations

import os

from mash import HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


def build_pool():
    return (
        HostBuilder()
        .agent(
            build_spec(
                agent_id=os.environ["TEST_PROCESS_AGENT_ID"],
                response_text="process-ok",
                delay_seconds=float(os.environ.get("TEST_PROCESS_DELAY", "0")),
            ),
            metadata=metadata(),
        )
        .build()
    )
