"""The CLI's saved connection, which holds the API key."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mash.cli import config as cli_config
from mash.cli.config import CLIConfig, save_config


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(cli_config.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_the_saved_connection_is_readable_only_by_its_owner(home: Path) -> None:
    old = os.umask(0o022)
    try:
        path = save_config(CLIConfig(api_base_url="http://h", api_key="secret"))
    finally:
        os.umask(old)

    assert path == home / ".mash" / "cli.json"
    assert _mode(path) == 0o600
    assert "secret" in path.read_text()


def test_saving_over_a_world_readable_file_tightens_it(home: Path) -> None:
    path = home / ".mash" / "cli.json"
    path.parent.mkdir()
    path.write_text("{}")
    path.chmod(0o644)

    save_config(CLIConfig(api_base_url="http://h", api_key="secret"))

    assert _mode(path) == 0o600
    assert "secret" in path.read_text()
