"""Reusable local tools for db-agent skills."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from mash.memory.store import MemoryStore
from mash.tools.base import FunctionTool, Tool, ToolResult


def build_local_tools(
    store: MemoryStore,
    app_id: str,
    get_session_id: Callable[[], Optional[str]],
    workspace_root: Path,
) -> List[Tool]:
    """Build general-purpose local tools that can be reused by multiple skills."""

    root = workspace_root.resolve()

    def resolve_workspace_path(raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"path must be within workspace root: {root}")
        return resolved

    def ensure_session_id() -> str:
        session_id = get_session_id()
        if not session_id:
            raise ValueError("session_id is not available")
        return session_id

    def list_workspace_files(args: Dict[str, Any]) -> ToolResult:
        raw_path = str(args.get("path", "."))
        pattern = str(args.get("glob", "*"))
        recursive = bool(args.get("recursive", True))
        limit = int(args.get("limit", 200))

        if limit <= 0:
            return ToolResult.error("limit must be > 0")

        try:
            base = resolve_workspace_path(raw_path)
            if not base.exists():
                return ToolResult.error(f"path does not exist: {raw_path}")
            if not base.is_dir():
                return ToolResult.error(f"path is not a directory: {raw_path}")

            iterator = base.rglob("*") if recursive else base.iterdir()
            results: List[str] = []
            for candidate in iterator:
                if not candidate.is_file():
                    continue
                rel = candidate.relative_to(root).as_posix()
                if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(
                    rel, pattern
                ):
                    results.append(rel)
                if len(results) >= limit:
                    break

            payload = {
                "root": root.as_posix(),
                "path": base.relative_to(root).as_posix(),
                "glob": pattern,
                "recursive": recursive,
                "count": len(results),
                "files": sorted(results),
            }
            return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))
        except Exception as exc:
            return ToolResult.error(f"workspace_list_files failed: {exc}")

    def read_workspace_file(args: Dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult.error("path is required")
        try:
            path = resolve_workspace_path(raw_path)
            if not path.exists():
                return ToolResult.error(f"file not found: {raw_path}")
            if not path.is_file():
                return ToolResult.error(f"not a file: {raw_path}")
            content = path.read_text(encoding="utf-8")
            payload = {
                "path": path.relative_to(root).as_posix(),
                "content": content,
                "size": len(content),
            }
            return ToolResult.success(json.dumps(payload, ensure_ascii=True))
        except Exception as exc:
            return ToolResult.error(f"workspace_read_file failed: {exc}")

    def write_workspace_file(args: Dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        content = args.get("content")
        create_dirs = bool(args.get("create_dirs", False))

        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult.error("path is required")
        if not isinstance(content, str):
            return ToolResult.error("content must be a string")

        try:
            path = resolve_workspace_path(raw_path)
            parent = path.parent
            if not parent.exists() and not create_dirs:
                return ToolResult.error(
                    "parent directory does not exist; set create_dirs=true"
                )
            if create_dirs:
                parent.mkdir(parents=True, exist_ok=True)

            path.write_text(content, encoding="utf-8")
            payload = {
                "path": path.relative_to(root).as_posix(),
                "bytes_written": len(content.encode("utf-8")),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
            return ToolResult.success(json.dumps(payload, ensure_ascii=True))
        except Exception as exc:
            return ToolResult.error(f"workspace_write_file failed: {exc}")

    def structured_diff(args: Dict[str, Any]) -> ToolResult:
        before_text = args.get("before_text")
        after_text = args.get("after_text")
        fmt = str(args.get("format", "unified"))
        from_file = str(args.get("from_file", "before"))
        to_file = str(args.get("to_file", "after"))

        if not isinstance(before_text, str) or not isinstance(after_text, str):
            return ToolResult.error("before_text and after_text must be strings")

        before_lines = before_text.splitlines(keepends=True)
        after_lines = after_text.splitlines(keepends=True)

        try:
            if fmt == "unified":
                lines = list(
                    difflib.unified_diff(
                        before_lines,
                        after_lines,
                        fromfile=from_file,
                        tofile=to_file,
                        lineterm="",
                    )
                )
            elif fmt == "context":
                lines = list(
                    difflib.context_diff(
                        before_lines,
                        after_lines,
                        fromfile=from_file,
                        tofile=to_file,
                        lineterm="",
                    )
                )
            else:
                return ToolResult.error("format must be one of: unified, context")

            payload = {
                "format": fmt,
                "line_count": len(lines),
                "diff": "\n".join(lines),
            }
            return ToolResult.success(json.dumps(payload, ensure_ascii=True))
        except Exception as exc:
            return ToolResult.error(f"structured_diff failed: {exc}")

    def validate_yaml(args: Dict[str, Any]) -> ToolResult:
        document_text = args.get("document_text")
        schema_text = args.get("schema_text")
        if not isinstance(document_text, str) or not isinstance(schema_text, str):
            return ToolResult.error("document_text and schema_text must be strings")

        try:
            document = yaml.safe_load(document_text)
            schema = yaml.safe_load(schema_text)
        except Exception as exc:
            return ToolResult.error(f"invalid yaml: {exc}")

        errors: List[str] = []
        _validate_against_schema(value=document, schema=schema, path="$", errors=errors)

        payload = {
            "valid": len(errors) == 0,
            "errors": errors,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))

    def get_plan_state(args: Dict[str, Any]) -> ToolResult:
        key = str(args.get("key", "plan_state"))
        try:
            session_id = ensure_session_id()
            value = store.get_app_data(
                app_id=app_id,
                session_id=session_id,
                key=key,
            )
            payload = {"key": key, "value": value}
            return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))
        except Exception as exc:
            return ToolResult.error(f"get_plan_state failed: {exc}")

    def set_plan_state(args: Dict[str, Any]) -> ToolResult:
        key = str(args.get("key", "plan_state"))
        state_json = args.get("state_json")
        plan_path_arg = args.get("plan_path")
        session_id = ensure_session_id()

        if not isinstance(state_json, dict):
            return ToolResult.error("state_json must be a JSON object")

        state_to_store = dict(state_json)
        state_to_store["session_id"] = session_id

        if isinstance(plan_path_arg, str) and plan_path_arg.strip():
            try:
                plan_path = resolve_workspace_path(plan_path_arg)
                session_suffix = f"_{session_id}"
                session_plan_name = (
                    plan_path.name
                    if plan_path.stem.endswith(session_suffix)
                    else f"{plan_path.stem}{session_suffix}{plan_path.suffix}"
                )
                session_plan_path = plan_path.with_name(session_plan_name)

                if plan_path.exists() and plan_path.is_file():
                    plan_text = plan_path.read_text(encoding="utf-8")
                    # Keep a session-specific plan file for easier traceability.
                    session_plan_path.parent.mkdir(parents=True, exist_ok=True)
                    session_plan_path.write_text(plan_text, encoding="utf-8")
                    state_to_store["plan_path"] = (
                        session_plan_path.relative_to(root).as_posix()
                    )
                    state_to_store["plan_hash"] = hashlib.sha256(
                        plan_text.encode("utf-8")
                    ).hexdigest()
                else:
                    state_to_store["plan_path"] = (
                        session_plan_path.relative_to(root).as_posix()
                    )
            except Exception as exc:
                return ToolResult.error(
                    f"set_plan_state failed to hash plan_path: {exc}"
                )

        try:
            store.set_app_data(
                app_id=app_id,
                session_id=session_id,
                key=key,
                value=state_to_store,
            )
            payload = {"key": key, "value": state_to_store}
            return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))
        except Exception as exc:
            return ToolResult.error(f"plan_state_set failed: {exc}")

    return [
        FunctionTool(
            name="list_workspace_files",
            description=(
                "List files in the workspace from a relative path and glob pattern. "
                "Useful for discovering config files, plans, and schemas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to workspace root.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Glob pattern (for example: *.yml or metrics-layer/**/*.yml).",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to recurse into subdirectories.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum files to return.",
                    },
                },
            },
            _executor=list_workspace_files,
        ),
        FunctionTool(
            name="read_workspace_file",
            description=(
                "Read a UTF-8 text file from the workspace. "
                "Useful for loading plan.md, YAML configs, and schema files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to workspace root.",
                    }
                },
                "required": ["path"],
            },
            _executor=read_workspace_file,
        ),
        FunctionTool(
            name="write_workspace_file",
            description=(
                "Write UTF-8 text content to a workspace file. "
                "Use for plan artifacts and generated config files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to workspace root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                    "create_dirs": {
                        "type": "boolean",
                        "description": "Create parent directories if missing.",
                    },
                },
                "required": ["path", "content"],
            },
            _executor=write_workspace_file,
        ),
        FunctionTool(
            name="structured_diff",
            description=(
                "Produce a deterministic text diff between before and after strings. "
                "Useful for plan generation and review."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "before_text": {"type": "string"},
                    "after_text": {"type": "string"},
                    "format": {
                        "type": "string",
                        "enum": ["unified", "context"],
                        "description": "Diff output format.",
                    },
                    "from_file": {"type": "string"},
                    "to_file": {"type": "string"},
                },
                "required": ["before_text", "after_text"],
            },
            _executor=structured_diff,
        ),
        FunctionTool(
            name="validate_yaml",
            description=(
                "Validate a YAML document against a lightweight YAML schema. "
                "Supports required fields, basic types, enum values, nested objects, and arrays."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document_text": {
                        "type": "string",
                        "description": "YAML document to validate.",
                    },
                    "schema_text": {
                        "type": "string",
                        "description": "YAML schema definition.",
                    },
                },
                "required": ["document_text", "schema_text"],
            },
            _executor=validate_yaml,
        ),
        FunctionTool(
            name="get_plan_state",
            description=(
                "Read the current plan lifecycle state from app data for this session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "App data key to read (default: plan_state).",
                    }
                },
            },
            _executor=get_plan_state,
        ),
        FunctionTool(
            name="set_plan_state",
            description=(
                "Persist plan lifecycle state into app data for this session. "
                "Optionally include plan_path to store a stable plan hash and "
                "normalize to a session-suffixed plan filename."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "App data key to write (default: plan_state).",
                    },
                    "state_json": {
                        "type": "object",
                        "description": "Plan state object to persist.",
                    },
                    "plan_path": {
                        "type": "string",
                        "description": (
                            "Optional workspace-relative plan path. "
                            "Stored plan path is normalized to include _<session_id>."
                        ),
                    },
                },
                "required": ["state_json"],
            },
            _executor=set_plan_state,
        ),
    ]


def _validate_against_schema(
    value: Any, schema: Any, path: str, errors: List[str]
) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema must be an object")
        return

    expected_type = schema.get("type")
    if expected_type:
        if not _matches_type(value, expected_type):
            errors.append(
                f"{path}: expected type '{expected_type}', got '{type(value).__name__}'"
            )
            return

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(f"{path}: value '{value}' is not in enum {enum_values}")

    if expected_type == "object" and isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{path}: missing required key '{key}'")

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, prop_schema in properties.items():
                if key in value:
                    _validate_against_schema(
                        value=value[key],
                        schema=prop_schema,
                        path=f"{path}.{key}",
                        errors=errors,
                    )

        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            for key in value.keys():
                if key not in properties:
                    errors.append(f"{path}: unexpected key '{key}'")

    if expected_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            for idx, item in enumerate(value):
                _validate_against_schema(
                    value=item,
                    schema=item_schema,
                    path=f"{path}[{idx}]",
                    errors=errors,
                )


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
            value, bool
        )
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
