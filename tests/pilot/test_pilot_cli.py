"""The pilot CLI's connection: flags, then env, then `mash connect`'s saved key."""

from __future__ import annotations

from argparse import Namespace

import pytest

from mash.cli.config import CLIConfig
from pilot import cli


def _args(**overrides: object) -> Namespace:
    return Namespace(**{"api_base_url": None, "api_key": None, **overrides})


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch):
    def _save(url: str, key: str | None) -> None:
        config = CLIConfig(api_base_url=url, api_key=key, host_id=None, user_id="me")
        monkeypatch.setattr(cli, "load_config", lambda: config)

    monkeypatch.delenv("MASH_API_KEY", raising=False)
    monkeypatch.delenv("MASH_API_BASE_URL", raising=False)
    return _save


def test_the_saved_key_is_used_for_the_deployment_it_was_saved_for(saved) -> None:
    saved("http://127.0.0.1:8000/", "saved-key")
    assert cli._resolve_connection(_args()) == ("http://127.0.0.1:8000", "saved-key")


def test_the_saved_key_never_goes_to_another_deployment(saved) -> None:
    saved("https://prod.example.com", "prod-key")
    assert cli._resolve_connection(_args()) == ("http://127.0.0.1:8000", None)


def test_a_flag_or_env_key_wins_over_the_saved_one(saved, monkeypatch) -> None:
    saved("http://127.0.0.1:8000", "saved-key")
    assert cli._resolve_connection(_args(api_key="flag-key"))[1] == "flag-key"
    monkeypatch.setenv("MASH_API_KEY", "env-key")
    assert cli._resolve_connection(_args())[1] == "env-key"
