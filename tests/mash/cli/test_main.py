from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mash.cli.main import build_parser


@pytest.fixture(autouse=True)
def _isolated_cli_env(tmp_path, monkeypatch) -> None:
    """Keep the developer's environment out of the CLI under test.

    ``main()`` loads the nearest ``.env`` from the working directory up, and
    ``MASH_*`` variables win over the saved connection. Run from the repo, the
    repo's ``.env`` would set ``MASH_API_KEY`` for this test and every later one.
    """
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("MASH_"):
            monkeypatch.delenv(name)


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
            "--agents",
            "email,calendar",
        ]
    )
    assert args.command == "compose"
    assert args.host_id == "assistant"
    assert args.agents == "email,calendar"


def test_build_parser_rejects_targeting_flags_on_connect() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["connect", "--agent", "concierge"])
    with pytest.raises(SystemExit):
        parser.parse_args(["connect", "--host", "assistant"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["connect", "--host", "assistant", "--agents", "concierge"]
        )


def test_build_parser_accepts_workflows_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(["workflows"])
    assert args.command == "workflows"


def test_connect_validates_connection_before_saving(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main

    fake_client = MagicMock()
    fake_client.health.return_value = {"status": "ok"}
    saved = {}

    def fake_save_config(config):
        saved["config"] = config
        return tmp_path / "cli.json"

    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "save_config", fake_save_config
    ), patch.object(cli_main, "load_config", return_value=None):
        code = cli_main.main(
            [
                "connect",
                "--api-base-url",
                "http://127.0.0.1:8000",
                "--api-key",
                "secret",
            ]
        )

    assert code == 0
    fake_client.health.assert_called_once_with()
    assert saved["config"].api_base_url == "http://127.0.0.1:8000"
    assert saved["config"].api_key == "secret"
    assert saved["config"].host_id is None


def test_connect_fails_loudly_and_saves_nothing_when_unreachable(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main

    fake_client = MagicMock()
    fake_client.health.side_effect = RuntimeError("connection refused")
    saved = {}

    def fake_save_config(config):
        saved["config"] = config
        return tmp_path / "cli.json"

    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "save_config", fake_save_config
    ), patch.object(cli_main, "load_config", return_value=None):
        code = cli_main.main(
            ["connect", "--api-base-url", "http://127.0.0.1:8000"]
        )

    assert code == 1
    assert "config" not in saved


def test_workflows_subcommand_lists_deployment_workflows(capsys) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main

    fake_client = MagicMock()
    fake_client.list_workflows.return_value = [
        {
            "workflow_id": "changelog",
            "step_count": 2,
            "step_kinds": {"code": 1, "agent": 1},
        },
        {
            "workflow_id": "backfill",
            "step_count": 1,
            "step_kinds": {"code": 1, "agent": 0},
        },
    ]

    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "load_config", return_value=None
    ):
        code = cli_main.main(
            ["workflows", "--api-base-url", "http://127.0.0.1:8000"]
        )

    assert code == 0
    fake_client.list_workflows.assert_called_once_with()
    output = capsys.readouterr().out
    assert "changelog" in output
    assert "backfill" in output
    assert "1 agent, 1 code" in output


def test_compose_defines_host_and_saves_config(tmp_path, monkeypatch) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main

    fake_client = MagicMock()
    fake_client.define_host.return_value = {
        "host_id": "assistant",
        "agents": [{"agent_id": "email"}, {"agent_id": "calendar"}],
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
                "--agents",
                "email,calendar",
            ]
        )

    assert code == 0
    fake_client.define_host.assert_called_once_with(
        "assistant",
        agents=["email", "calendar"],
        workflows=[],
    )
    assert saved["config"].host_id == "assistant"
    assert saved["config"].api_base_url == "http://127.0.0.1:8000"


def test_compose_reuses_saved_connection(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main
    from mash.cli.config import CLIConfig

    fake_client = MagicMock()
    fake_client.define_host.return_value = {
        "host_id": "assistant",
        "agents": [{"agent_id": "concierge"}],
        "workflows": [],
    }
    saved = {}

    def fake_save_config(config):
        saved["config"] = config
        return tmp_path / "cli.json"

    existing = CLIConfig(
        api_base_url="http://127.0.0.1:8000",
        api_key="k",
        user_id="u-42",
    )
    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "save_config", fake_save_config
    ), patch.object(cli_main, "load_config", return_value=existing):
        code = cli_main.main(
            ["compose", "--host", "assistant", "--agents", "concierge"]
        )

    assert code == 0
    assert saved["config"].host_id == "assistant"
    assert saved["config"].api_base_url == "http://127.0.0.1:8000"
    assert saved["config"].api_key == "k"
    assert saved["config"].user_id == "u-42"


def test_compose_no_longer_takes_a_primary() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["compose", "--host", "assistant", "--primary", "concierge"])
    with pytest.raises(SystemExit):
        parser.parse_args(["compose", "--host", "assistant", "--subagents", "a"])


def test_connect_saves_the_user_id(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main

    fake_client = MagicMock()
    fake_client.health.return_value = {"status": "ok"}
    saved = {}

    def fake_save_config(config):
        saved["config"] = config
        return tmp_path / "cli.json"

    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "save_config", fake_save_config
    ), patch.object(cli_main, "load_config", return_value=None):
        code = cli_main.main(
            ["connect", "--api-base-url", "http://127.0.0.1:8000", "--user-id", "u-42"]
        )

    assert code == 0
    assert saved["config"].user_id == "u-42"


def test_repl_requires_a_user_id(capsys) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main
    from mash.cli.config import CLIConfig

    fake_client = MagicMock()
    shell = MagicMock()
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000")
    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "load_config", return_value=existing
    ), patch.object(cli_main, "Repl", shell):
        assert cli_main.main(["repl", "--agent", "primary"]) == 1
        shell.assert_not_called()
        assert cli_main.main(["repl", "--agent", "primary", "--user-id", "u-42"]) == 0

    target = shell.call_args.args[1]
    assert target.user_id == "u-42"


def test_repl_debug_is_off_by_default() -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main
    from mash.cli.config import CLIConfig

    shell = MagicMock()
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000", user_id="u-42")
    with patch.object(cli_main, "MashHostClient", return_value=MagicMock()), patch.object(
        cli_main, "load_config", return_value=existing
    ), patch.object(cli_main, "Repl", shell):
        assert cli_main.main(["repl", "--agent", "primary"]) == 0
        assert shell.call_args.kwargs["debug"] is False
        assert cli_main.main(["repl", "--agent", "primary", "--debug"]) == 0
        assert shell.call_args.kwargs["debug"] is True


def _repl_target(argv: list[str], sessions: list[dict], picked=None):
    """Run ``mash repl`` with a fake client; return (exit code, Target or None, client)."""
    from unittest.mock import MagicMock, patch

    from mash.cli import main as cli_main
    from mash.cli.config import CLIConfig

    client = MagicMock()
    client.list_sessions.return_value = sessions
    shell = MagicMock()
    picker = MagicMock()
    if isinstance(picked, BaseException):
        picker.return_value.run.side_effect = picked
    else:
        picker.return_value.run.return_value = picked
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000", user_id="u-42")
    with patch.object(cli_main, "MashHostClient", return_value=client), patch.object(
        cli_main, "load_config", return_value=existing
    ), patch.object(cli_main, "Repl", shell), patch.object(cli_main, "menu", picker):
        code = cli_main.main(["repl", "--agent", "primary", *argv])
    target = shell.call_args.args[1] if shell.call_args else None
    return code, target, client, picker


_SESSIONS = [
    {"session_id": "s-new", "first_message": "why was march high", "turn_count": 2,
     "last_activity_at": 2.0},
    {"session_id": "s-old", "first_message": "compare quotes", "turn_count": 1,
     "last_activity_at": 1.0},
]


def test_repl_continue_goes_on_in_the_users_latest_session() -> None:
    code, target, client, picker = _repl_target(["--continue"], _SESSIONS[:1])
    assert code == 0
    client.list_sessions.assert_called_once_with("primary", user_id="u-42", limit=1)
    assert (target.session_id, target.resumed, target.title) == (
        "s-new", True, "why was march high"
    )
    picker.assert_not_called()


def test_repl_resume_opens_the_picker() -> None:
    code, target, client, picker = _repl_target(["--resume"], _SESSIONS, picked=_SESSIONS[1])
    assert code == 0
    client.list_sessions.assert_called_once_with("primary", user_id="u-42", limit=20)
    options = picker.call_args.args[0]
    assert [value["session_id"] for value, _ in options] == ["s-new", "s-old"]
    assert (target.session_id, target.resumed, target.title) == ("s-old", True, "compare quotes")


def test_repl_resume_left_with_ctrl_c_starts_nothing() -> None:
    code, target, _, _ = _repl_target(["--resume"], _SESSIONS, picked=KeyboardInterrupt())
    assert code == 130
    assert target is None


def test_repl_continue_without_earlier_sessions_starts_a_new_one(capsys) -> None:
    code, target, _, _ = _repl_target(["--continue"], [])
    assert code == 0
    assert target.resumed is False and target.title == ""
    assert "No earlier sessions. Starting a new one." in capsys.readouterr().out


def test_repl_session_id_resumes_that_session_and_excludes_the_other_flags(capsys) -> None:
    import pytest

    code, target, client, _ = _repl_target(["--session-id", "s-7"], _SESSIONS)
    assert code == 0
    assert (target.session_id, target.resumed) == ("s-7", True)
    client.list_sessions.assert_not_called()
    with pytest.raises(SystemExit):
        _repl_target(["--session-id", "s-7", "--continue"], _SESSIONS)
    capsys.readouterr()


def test_cli_loads_dotenv_from_the_working_directory(tmp_path, monkeypatch) -> None:
    from mash.cli.main import load_env_file

    (tmp_path / ".env").write_text("MASH_TEST_FROM_FILE=file\nMASH_TEST_SET=file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MASH_TEST_FROM_FILE", raising=False)
    monkeypatch.setenv("MASH_TEST_SET", "env")

    load_env_file(["host", "serve"])

    assert os.environ["MASH_TEST_FROM_FILE"] == "file"
    assert os.environ["MASH_TEST_SET"] == "env"
    monkeypatch.delenv("MASH_TEST_FROM_FILE")


def test_agent_processes_never_read_dotenv(tmp_path, monkeypatch) -> None:
    """The host leaves its secret out of an agent's environment; .env must not restore it."""
    from mash.cli.main import load_env_file

    (tmp_path / ".env").write_text("MASH_HOST_SECRET=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MASH_HOST_SECRET", raising=False)

    load_env_file(["agent", "serve", "--agent", "a"])

    assert "MASH_HOST_SECRET" not in os.environ


def test_importing_the_library_reads_no_dotenv() -> None:
    script = (
        "import dotenv\n"
        "def refuse(*args, **kwargs):\n"
        "    raise SystemExit('the library loaded .env')\n"
        "dotenv.load_dotenv = refuse\n"
        "import mash, mash.api, mash.core.config, mash.mcp, mash.runtime, mash.cli.main\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
