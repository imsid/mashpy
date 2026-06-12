from __future__ import annotations

import pytest

from mash.cli.main import build_parser


def test_build_parser_accepts_sessions_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(["sessions", "--agent", "primary"])
    assert args.command == "sessions"
    assert args.agent == "primary"


def test_build_parser_accepts_host_serve_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["host", "serve", "--host-app", "copilot.spec:build_host"]
    )
    assert args.command == "host"
    assert args.host_command == "serve"
    assert args.host_app == "copilot.spec:build_host"


def test_build_parser_rejects_removed_invoke_subcommand() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["invoke", "hello"])


def test_build_parser_accepts_compose_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "compose",
            "--host",
            "assistant",
            "--primary",
            "concierge",
            "--subagents",
            "email,calendar",
        ]
    )
    assert args.command == "compose"
    assert args.host_id == "assistant"
    assert args.primary == "concierge"
    assert args.subagents == "email,calendar"


def test_build_parser_rejects_composition_flags_on_connect() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["connect", "--host", "assistant", "--primary", "concierge"]
        )


def test_compose_defines_host_and_saves_config(tmp_path, monkeypatch) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main

    fake_client = MagicMock()
    fake_client.define_host.return_value = {
        "host_id": "assistant",
        "primary": {"agent_id": "concierge"},
        "subagents": [{"agent_id": "email"}, {"agent_id": "calendar"}],
        "workflows": [],
    }
    saved = {}

    def fake_save_config(config):
        saved["config"] = config
        return tmp_path / "cli.json"

    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "save_config", fake_save_config
    ), patch.object(cli_main, "load_config", return_value=None):
        code = cli_main.main(
            [
                "compose",
                "--api-base-url",
                "http://127.0.0.1:8000",
                "--host",
                "assistant",
                "--primary",
                "concierge",
                "--subagents",
                "email,calendar",
            ]
        )

    assert code == 0
    fake_client.define_host.assert_called_once_with(
        "assistant",
        primary="concierge",
        subagents=["email", "calendar"],
        workflows=[],
    )
    assert saved["config"].host_id == "assistant"
    assert saved["config"].agent_id is None
    assert saved["config"].api_base_url == "http://127.0.0.1:8000"


def test_compose_clears_saved_agent_target(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main
    from mash.cli.config import CLIConfig

    fake_client = MagicMock()
    fake_client.define_host.return_value = {
        "host_id": "assistant",
        "primary": {"agent_id": "concierge"},
        "subagents": [],
        "workflows": [],
    }
    saved = {}

    def fake_save_config(config):
        saved["config"] = config
        return tmp_path / "cli.json"

    existing = CLIConfig(
        api_base_url="http://127.0.0.1:8000",
        api_key="k",
        agent_id="email",
    )
    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "save_config", fake_save_config
    ), patch.object(cli_main, "load_config", return_value=existing):
        code = cli_main.main(
            ["compose", "--host", "assistant", "--primary", "concierge"]
        )

    assert code == 0
    assert saved["config"].host_id == "assistant"
    assert saved["config"].agent_id is None
    assert saved["config"].api_base_url == "http://127.0.0.1:8000"
    assert saved["config"].api_key == "k"


def test_compose_requires_primary() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["compose", "--host", "assistant"])
