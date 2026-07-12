"""Tests for request-scoped trace context helpers."""

from __future__ import annotations

import unittest

from mash.logging import bound_request_metadata, get_request_metadata


class RequestMetadataContextTests(unittest.TestCase):
    def test_unbound_returns_empty_dict(self) -> None:
        self.assertEqual(get_request_metadata(), {})

    def test_bound_metadata_is_readable_and_reset(self) -> None:
        with bound_request_metadata({"tenant": "acme", "user_id": "u-1"}):
            self.assertEqual(
                get_request_metadata(), {"tenant": "acme", "user_id": "u-1"}
            )
        self.assertEqual(get_request_metadata(), {})

    def test_returned_metadata_is_a_copy(self) -> None:
        with bound_request_metadata({"tenant": "acme"}):
            get_request_metadata()["tenant"] = "mutated"
            self.assertEqual(get_request_metadata(), {"tenant": "acme"})

    def test_bound_metadata_snapshots_the_caller_dict(self) -> None:
        metadata = {"tenant": "acme"}
        with bound_request_metadata(metadata):
            metadata["tenant"] = "mutated"
            self.assertEqual(get_request_metadata(), {"tenant": "acme"})

    def test_none_and_empty_bind_as_unset(self) -> None:
        with bound_request_metadata(None):
            self.assertEqual(get_request_metadata(), {})
        with bound_request_metadata({}):
            self.assertEqual(get_request_metadata(), {})

    def test_nested_binding_restores_outer_value(self) -> None:
        with bound_request_metadata({"tenant": "outer"}):
            with bound_request_metadata({"tenant": "inner"}):
                self.assertEqual(get_request_metadata(), {"tenant": "inner"})
            self.assertEqual(get_request_metadata(), {"tenant": "outer"})


if __name__ == "__main__":
    unittest.main()
