"""Unit tests for structured_output step helpers in steps.py."""

from __future__ import annotations

import unittest

from mash.runtime.engine.steps import (
    _extract_tool_structured_output,
    _validate_tool_structured_output,
)


def _make_payload(tool_name: str, *, structured_output=None) -> dict:
    p: dict = {
        "tool_call_id": "call-1",
        "tool_name": tool_name,
        "duration_ms": 1,
        "result": {"content": "ok", "is_error": False, "metadata": {}},
    }
    if structured_output is not None:
        p["structured_output"] = structured_output
    return p


class ExtractToolStructuredOutputTests(unittest.TestCase):
    def test_none_when_no_tool_sets_it(self) -> None:
        payloads = [_make_payload("tool_a"), _make_payload("tool_b")]
        self.assertIsNone(_extract_tool_structured_output(payloads))

    def test_returns_value_when_exactly_one_tool_sets_it(self) -> None:
        payloads = [
            _make_payload("tool_a"),
            _make_payload("tool_b", structured_output={"digest": "hello"}),
        ]
        result = _extract_tool_structured_output(payloads)
        self.assertEqual(result, {"digest": "hello"})

    def test_raises_when_multiple_tools_set_it(self) -> None:
        payloads = [
            _make_payload("tool_a", structured_output={"a": 1}),
            _make_payload("tool_b", structured_output={"b": 2}),
        ]
        with self.assertRaises(RuntimeError) as cm:
            _extract_tool_structured_output(payloads)
        self.assertIn("multiple tools", str(cm.exception))
        self.assertIn("exactly one", str(cm.exception))

    def test_ignores_non_dict_structured_output(self) -> None:
        payloads = [_make_payload("tool_a", structured_output="not-a-dict")]
        self.assertIsNone(_extract_tool_structured_output(payloads))

    def test_empty_payloads(self) -> None:
        self.assertIsNone(_extract_tool_structured_output([]))


class ValidateToolStructuredOutputTests(unittest.TestCase):
    def test_passes_when_all_required_fields_present(self) -> None:
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "score": {"type": "number"}},
            "required": ["summary", "score"],
        }
        _validate_tool_structured_output({"summary": "ok", "score": 0.9}, schema)

    def test_passes_when_no_required_fields(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        _validate_tool_structured_output({"x": "hi"}, schema)

    def test_passes_with_extra_fields_not_in_required(self) -> None:
        schema = {"type": "object", "required": ["name"]}
        _validate_tool_structured_output({"name": "ada", "extra": True}, schema)

    def test_raises_when_required_field_missing(self) -> None:
        schema = {"type": "object", "required": ["summary", "score"]}
        with self.assertRaises(ValueError) as cm:
            _validate_tool_structured_output({"summary": "ok"}, schema)
        self.assertIn("score", str(cm.exception))
        self.assertIn("missing required field", str(cm.exception))

    def test_raises_when_value_is_not_dict(self) -> None:
        schema = {"type": "object", "required": ["x"]}
        with self.assertRaises(ValueError) as cm:
            _validate_tool_structured_output("not-a-dict", schema)  # type: ignore[arg-type]
        self.assertIn("JSON object", str(cm.exception))
