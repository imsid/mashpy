"""Pilot tools demonstrating the requires_approval interface."""

from __future__ import annotations

import os
from typing import Any, Dict

from mash.tools.base import ToolResult


class UpdateDocsTool:
    """Tool that updates README.md or AGENTS.md files. Requires user approval."""

    name = "update_docs"
    requires_approval = True
    description = (
        "Write updated content to a README.md or AGENTS.md file. "
        "Requires explicit user approval before the file is modified."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "Relative path to the documentation file to update "
                    "(must end with README.md or AGENTS.md)."
                ),
            },
            "content": {
                "type": "string",
                "description": "The full updated content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace_root: str) -> None:
        self._workspace_root = workspace_root

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        basename = os.path.basename(file_path)
        if basename not in ("README.md", "AGENTS.md"):
            return ToolResult.error(
                f"Only README.md or AGENTS.md files can be updated, got: {basename}"
            )

        resolved = os.path.normpath(os.path.join(self._workspace_root, file_path))
        if not resolved.startswith(self._workspace_root):
            return ToolResult.error("Path escapes workspace root.")

        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.success(f"Updated {file_path}")

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
