"""The host serves its API off loopback only with an API key."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

import pytest

from mash.api.app import run_host
from mash.api.config import MashHostConfig, is_loopback_host
from mash.api.main import _run_serve_command


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "10.0.0.5", "example.com"])
def test_hosts_others_can_reach(host: str) -> None:
    assert not is_loopback_host(host)


def test_a_key_is_required_off_loopback() -> None:
    for config in (
        MashHostConfig(bind_host="0.0.0.0"),
        MashHostConfig(bind_host="0.0.0.0", api_key="   "),
    ):
        with pytest.raises(ValueError, match="API key is required"):
            config.check_bind()
    MashHostConfig(bind_host="0.0.0.0", api_key="k").check_bind()
    MashHostConfig(bind_host="127.0.0.1").check_bind()


def test_run_host_refuses_before_serving() -> None:
    with patch("mash.api.app.uvicorn.run") as serve:
        with pytest.raises(ValueError, match="API key is required"):
            run_host(object(), config=MashHostConfig(bind_host="0.0.0.0"))  # type: ignore[arg-type]
    serve.assert_not_called()


def test_host_serve_exits_without_a_key_off_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MASH_API_KEY", raising=False)
    args = Namespace(
        host_app="not.imported:build_pool",
        host="0.0.0.0",
        port=8000,
        runtime_database_url=None,
        api_key=None,
        cors_origin=None,
    )
    with pytest.raises(SystemExit, match="API key is required"):
        _run_serve_command(args)
