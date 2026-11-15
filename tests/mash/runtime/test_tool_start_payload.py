"""What a tool call's start event says about the work it starts."""

from __future__ import annotations

from mash.core.context import ToolCall
from mash.logging import bound_subagent_id_prefix
from mash.runtime.engine.steps import _tool_start_payload, child_request_id


def _call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="t-1", name=name, arguments=arguments)


def test_a_run_subagents_call_lists_its_children() -> None:
    call = _call("RunSubAgents", {"requests": [{"agent_id": "a"}, {"agent_id": "b"}]})
    with bound_subagent_id_prefix("r-1-0-0"):
        payload = _tool_start_payload(call)
    assert payload["subagents"] == [
        {"agent_id": "a", "request_id": child_request_id("r-1-0-0", 0)},
        {"agent_id": "b", "request_id": child_request_id("r-1-0-0", 1)},
    ]
    assert "workflow_run" not in payload


def test_a_run_workflow_call_names_the_run_it_starts() -> None:
    call = _call("RunWorkflow", {"workflow_id": " export ", "input": {"table": "t"}})
    with bound_subagent_id_prefix("r-1-0-0"):
        payload = _tool_start_payload(call)
    assert payload["workflow_run"] == {"workflow_id": "export", "dedup_key": "r-1-0-0"}
    assert payload["arguments"] == {"workflow_id": " export ", "input": {"table": "t"}}


def test_a_run_workflow_call_without_a_position_or_an_id_names_no_run() -> None:
    assert "workflow_run" not in _tool_start_payload(_call("RunWorkflow", {"workflow_id": "export"}))
    with bound_subagent_id_prefix("r-1-0-0"):
        assert "workflow_run" not in _tool_start_payload(_call("RunWorkflow", {}))
        assert "workflow_run" not in _tool_start_payload(_call("search", {"workflow_id": "x"}))
