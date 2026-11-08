"""Paths the host calls a container's daemon at stay on the route they name."""

from __future__ import annotations

import httpx
import pytest

from mash.sandbox.containers import UnsafePathError, daemon_path


def test_segments_and_tail_are_percent_encoded() -> None:
    assert daemon_path("exec") == "/exec"
    assert daemon_path("files", "research", tail="out/a?b #1%.txt") == (
        "/files/research/out/a%3Fb%20%231%25.txt"
    )
    assert daemon_path("ports", "8050", tail="", query="q=1") == "/ports/8050/?q=1"
    assert daemon_path("ports", "8050", tail="a/b/") == "/ports/8050/a/b/"


def test_the_http_client_keeps_the_path_as_built() -> None:
    path = daemon_path("ports", "1", tail="a/%2E%2E/b")
    assert httpx.URL(f"http://sandbox:9000{path}").raw_path == b"/ports/1/a/%252E%252E/b"


@pytest.mark.parametrize("segment", ["", ".", "..", "a/b"])
def test_a_segment_must_be_one_segment(segment: str) -> None:
    with pytest.raises(UnsafePathError):
        daemon_path("downloads", segment)


@pytest.mark.parametrize("tail", ["..", "../exec", "a/../../exec", "./x", "a/."])
def test_a_tail_may_not_climb(tail: str) -> None:
    with pytest.raises(UnsafePathError):
        daemon_path("ports", "1", tail=tail)
