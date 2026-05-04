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
