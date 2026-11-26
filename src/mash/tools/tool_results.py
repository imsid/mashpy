"""Large tool results: saved whole as a file in the agent's sandbox folder.

A tool result longer than the agent's ``tool_result_context_chars`` is written,
on a request that has a sandbox, to ``tool-results/<trace_id>/<tool_call_id>.txt``
in the agent's sandbox folder, and the result's metadata records that path as
``saved_to``. The model sees a note with the result's size, line count, exit
code and path instead of its text, and reads the file with ``run_code``. The
path comes from the call's ids, so a call run again after a crash writes the
same file.

``execute_tool.end`` keeps the full result: ``ForwardAnswer``, the trace views
and replay read it there, and replay shows the same note. On a request without
a sandbox there is nowhere to put the file, and the model gets the full result.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

RESULTS_FOLDER = "tool-results"
# The result metadata key that holds the saved file's path.
SAVED_TO = "saved_to"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def result_file_path(trace_id: str, tool_call_id: str) -> str:
    """Where a call's result is saved, relative to the agent's sandbox folder."""
    return f"{RESULTS_FOLDER}/{_UNSAFE.sub('_', trace_id)}/{_UNSAFE.sub('_', tool_call_id)}.txt"


def context_content(content: str, *, limit: int, metadata: Mapping[str, Any] | None) -> str:
    """What the model sees of a tool result: all of it, or a note naming its file.

    Only a result longer than ``limit`` and saved to a file becomes a note.
    """
    text = content if isinstance(content, str) else str(content)
    saved_to = (metadata or {}).get(SAVED_TO)
    if len(text) <= limit or not saved_to:
        return text
    lines = len(text.splitlines())
    exit_code = (metadata or {}).get("exit_code")
    exit_part = f", exit code {exit_code}" if exit_code is not None else ""
    return (
        f"[The result is {len(text):,} characters in {lines:,} lines{exit_part}: too long "
        f"to show here. It is saved whole to {saved_to} in your sandbox folder. Read "
        f"it with run_code, e.g. `tail -n 50 {saved_to}`, `grep -n <pattern> "
        f"{saved_to}` or `sed -n '1,200p' {saved_to}`.]"
    )


__all__ = ["RESULTS_FOLDER", "SAVED_TO", "context_content", "result_file_path"]
