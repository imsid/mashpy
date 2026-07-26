"""Tests for the top-level authoring exports on the ``mash`` package."""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_import_mash_stays_light() -> None:
    """``import mash`` must not drag in DBOS or provider SDKs."""
    code = (
        "import sys\n"
        "import mash\n"
        "heavy = sorted({m.split('.')[0] for m in sys.modules"
        " if m.split('.')[0] in ('dbos', 'anthropic', 'openai', 'google')})\n"
        "assert not heavy, heavy\n"
        "assert isinstance(mash.__version__, str)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_workflow_authoring_exports_stay_light() -> None:
    """Workflow types come from the leaf spec module, not the DBOS service."""
    code = (
        "import sys\n"
        "from mash import WorkflowSpec, CodeStep, AgentStep, StepContext\n"
        "heavy = sorted({m.split('.')[0] for m in sys.modules"
        " if m.split('.')[0] == 'dbos'})\n"
        "assert not heavy, heavy\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_top_level_exports_resolve_to_canonical_objects() -> None:
    import mash
    from mash.runtime import (
        AgentMetadata,
        AgentSpec,
        Host,
        HostBuilder,
        Pool,
    )
    from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec

    assert mash.AgentSpec is AgentSpec
    assert mash.AgentMetadata is AgentMetadata
    assert mash.Host is Host
    assert mash.HostBuilder is HostBuilder
    assert mash.Pool is Pool
    assert mash.WorkflowSpec is WorkflowSpec
    assert mash.CodeStep is CodeStep
    assert mash.AgentStep is AgentStep
    assert mash.StepContext is StepContext


def test_unknown_attribute_raises_attribute_error() -> None:
    import mash

    with pytest.raises(AttributeError):
        mash.DoesNotExist  # noqa: B018
