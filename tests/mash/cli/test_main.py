from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mash.cli.app import build_parser


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

    from mash.cli import app as cli_main

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

    from mash.cli import app as cli_main

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

    from mash.cli import app as cli_main

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

    from mash.cli import app as cli_main

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

    from mash.cli import app as cli_main
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
        app_id="app-42",
        policy_id="p-1",
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
    assert (saved["config"].app_id, saved["config"].policy_id) == ("app-42", "p-1")


def test_compose_no_longer_takes_a_primary() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["compose", "--host", "assistant", "--primary", "concierge"])
    with pytest.raises(SystemExit):
        parser.parse_args(["compose", "--host", "assistant", "--subagents", "a"])


def test_connect_saves_the_app_and_policy(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main

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
                "--app-id",
                "app-42",
                "--policy-id",
                "p-1",
            ]
        )

    assert code == 0
    assert (saved["config"].app_id, saved["config"].policy_id) == ("app-42", "p-1")


def test_repl_requires_an_app_and_a_policy(capsys) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    fake_client = MagicMock()
    shell = MagicMock()
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000")
    with patch.object(cli_main, "MashHostClient", return_value=fake_client), patch.object(
        cli_main, "load_config", return_value=existing
    ), patch.object(cli_main, "Repl", shell):
        assert cli_main.main(["repl", "--agent", "primary"]) == 1
        assert cli_main.main(["repl", "--agent", "primary", "--app-id", "app-42"]) == 1
        shell.assert_not_called()
        assert "an app id and a policy id are required" in capsys.readouterr().out
        assert (
            cli_main.main(
                ["repl", "--agent", "primary", "--app-id", "app-42", "--policy-id", "p-1"]
            )
            == 0
        )

    target = shell.call_args.args[1]
    assert (target.app_id, target.policy_id) == ("app-42", "p-1")


def test_repl_debug_is_off_by_default() -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    shell = MagicMock()
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000", app_id="app-42", policy_id="p-1")
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

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    client = MagicMock()
    client.list_sessions.return_value = sessions
    shell = MagicMock()
    picker = MagicMock()
    if isinstance(picked, BaseException):
        picker.return_value.run.side_effect = picked
    else:
        picker.return_value.run.return_value = picked
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000", app_id="app-42", policy_id="p-1")
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


def test_repl_continue_goes_on_in_the_apps_latest_session() -> None:
    code, target, client, picker = _repl_target(["--continue"], _SESSIONS[:1])
    assert code == 0
    client.list_sessions.assert_called_once_with("primary", app_id="app-42", limit=1)
    assert (target.session_id, target.resumed, target.title) == (
        "s-new", True, "why was march high"
    )
    picker.assert_not_called()


def test_repl_resume_opens_the_picker() -> None:
    code, target, client, picker = _repl_target(["--resume"], _SESSIONS, picked=_SESSIONS[1])
    assert code == 0
    client.list_sessions.assert_called_once_with("primary", app_id="app-42", limit=20)
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
    from mash.cli.app import load_env_file

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
    from mash.cli.app import load_env_file

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
        "import mash, mash.api, mash.core.config, mash.mcp, mash.runtime, mash.cli.app\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _one_shot(argv: list[str], *, sessions: list[dict] | None = None, identity: bool = True,
              stdin: str | None = None):
    """Run ``mash run`` with a fake client; return (exit code, run_once mock, client)."""
    import io
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    client = MagicMock()
    client.list_sessions.return_value = sessions or []
    once = MagicMock(return_value=0)
    existing = CLIConfig(
        api_base_url="http://127.0.0.1:8000",
        **({"app_id": "app-42", "policy_id": "p-1"} if identity else {}),
    )
    patches = [
        patch.object(cli_main, "MashHostClient", return_value=client),
        patch.object(cli_main, "load_config", return_value=existing),
        patch.object(cli_main, "run_once", once),
    ]
    if stdin is not None:
        patches.append(patch.object(cli_main.sys, "stdin", io.StringIO(stdin)))
    for item in patches:
        item.start()
    try:
        code = cli_main.main(["run", "--agent", "primary", *argv])
    finally:
        for item in reversed(patches):
            item.stop()
    return code, once, client


def test_run_submits_the_message_in_a_new_session() -> None:
    code, once, client = _one_shot(["why was march high"])
    assert code == 0
    kwargs = once.call_args.kwargs
    assert once.call_args.args == (client,)
    assert kwargs["session_id"]
    assert {key: value for key, value in kwargs.items() if key != "session_id"} == {
        "agent_id": "primary", "host_id": None, "app_id": "app-42", "policy_id": "p-1",
        "message": "why was march high", "json_output": False,
        "theme": None, "tool_lines": None,
    }
    client.list_sessions.assert_not_called()


def test_run_returns_the_requests_exit_status() -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    existing = CLIConfig(api_base_url="http://127.0.0.1:8000", app_id="app-42", policy_id="p-1")
    with patch.object(cli_main, "MashHostClient", return_value=MagicMock()), patch.object(
        cli_main, "load_config", return_value=existing
    ), patch.object(cli_main, "run_once", MagicMock(return_value=2)):
        assert cli_main.main(["run", "--agent", "primary", "deploy"]) == 2


def test_run_reads_the_message_from_stdin_and_takes_json() -> None:
    code, once, _ = _one_shot(["--json"], stdin="summarize this\n")
    assert code == 0
    assert once.call_args.kwargs["message"] == "summarize this"
    assert once.call_args.kwargs["json_output"] is True


def test_run_continue_goes_on_in_the_apps_latest_session() -> None:
    code, once, client = _one_shot(["--continue", "and april?"], sessions=_SESSIONS[:1])
    assert code == 0
    client.list_sessions.assert_called_once_with("primary", app_id="app-42", limit=1)
    assert once.call_args.kwargs["session_id"] == "s-new"


def test_run_session_id_names_the_session_and_excludes_continue(capsys) -> None:
    code, once, client = _one_shot(["--session-id", "s-7", "hi"])
    assert code == 0
    assert once.call_args.kwargs["session_id"] == "s-7"
    client.list_sessions.assert_not_called()
    with pytest.raises(SystemExit):
        _one_shot(["--session-id", "s-7", "--continue", "hi"])
    capsys.readouterr()


def test_run_requires_an_app_a_policy_and_a_message(capsys) -> None:
    code, once, _ = _one_shot(["hi"], identity=False)
    assert code == 1
    assert "an app id and a policy id are required" in capsys.readouterr().out
    code, once, _ = _one_shot([], stdin="  \n")
    assert code == 1
    once.assert_not_called()
    assert "empty" in capsys.readouterr().out


class _WorkflowClient:
    """The client calls ``mash workflow`` makes, recorded."""

    def __init__(self, events: list[dict] | None = None) -> None:
        self.runs: list[dict] = []
        self.resumed: list[tuple[str, str]] = []
        self.status: list[tuple[str, str]] = []
        self.events = events if events is not None else [
            {"event": "step.started", "data": {"step_id": "scan"}},
            {"event": "step.completed", "data": {"step_id": "scan"}},
            {"event": "workflow.completed", "data": {"result": {"rows": 3}}},
        ]

    def list_agents(self):
        return [{"agent_id": "primary"}]

    def list_workflows(self, *, host=None):
        return [{"workflow_id": "export", "step_count": 1,
                 "step_preview": [{"step_id": "export", "kind": "code"}]}]

    def run_workflow(
        self, workflow_id, *, dedup_key=None, workflow_input=None, app_id=None, policy_id=None
    ):
        self.runs.append({"workflow_id": workflow_id, "dedup_key": dedup_key,
                          "workflow_input": workflow_input, "app_id": app_id,
                          "policy_id": policy_id})
        return {"workflow_id": workflow_id, "run_id": "run-1", "status": "queued"}

    def stream_workflow_run(self, workflow_id, run_id):
        yield from self.events

    def get_workflow_run(self, workflow_id, run_id):
        self.status.append((workflow_id, run_id))
        return {"workflow_id": workflow_id, "run_id": run_id, "status": "completed",
                "steps": [], "result": {"rows": 3}}

    def resume_workflow_run(self, workflow_id, run_id):
        self.resumed.append((workflow_id, run_id))
        return {"workflow_id": workflow_id, "run_id": run_id, "status": "running"}

    def close(self) -> None:
        pass


def _workflow(argv: list[str], client: _WorkflowClient, *, identity: bool = True) -> int:
    from unittest.mock import patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    existing = CLIConfig(
        api_base_url="http://127.0.0.1:8000",
        **({"app_id": "app-42", "policy_id": "p-1"} if identity else {}),
    )
    with patch.object(cli_main, "MashHostClient", return_value=client), patch.object(
        cli_main, "load_config", return_value=existing
    ):
        return cli_main.main(["workflow", "--agent", "primary", *argv])


def test_workflow_run_follows_the_run_to_its_result(capsys) -> None:
    client = _WorkflowClient()
    code = _workflow(["run", "export", "--input", '{"table": "bills"}', "--dedup-key", "k1"], client)
    assert code == 0
    assert client.runs == [{"workflow_id": "export", "dedup_key": "k1",
                            "workflow_input": {"table": "bills"}, "app_id": "app-42",
                            "policy_id": "p-1"}]
    out = capsys.readouterr().out
    assert "Run ID: run-1" in out
    assert "Workflow step scan completed" in out
    assert "Workflow status: completed" in out
    assert '"rows": 3' in out


def test_workflow_run_reads_its_input_from_a_file_and_runs_without_an_app(tmp_path) -> None:
    path = tmp_path / "input.json"
    path.write_text('{\n  "table": "two words"\n}\n', encoding="utf-8")
    client = _WorkflowClient()
    assert _workflow(["run", "export", "--input", f"@{path}"], client, identity=False) == 0
    assert client.runs == [{"workflow_id": "export", "dedup_key": None,
                            "workflow_input": {"table": "two words"}, "app_id": None,
                            "policy_id": None}]


def test_workflow_run_no_follow_prints_the_run_id_and_exits(capsys) -> None:
    client = _WorkflowClient(events=[{"event": "workflow.error", "data": {"error": "not read"}}])
    assert _workflow(["run", "export", "--no-follow"], client) == 0
    out = capsys.readouterr().out
    assert "Run ID: run-1" in out and "Status: queued" in out
    assert "not read" not in out


def test_workflow_run_exits_1_when_a_step_fails_or_the_input_is_bad(capsys) -> None:
    client = _WorkflowClient(events=[
        {"event": "step.started", "data": {"step_id": "scan"}},
        {"event": "step.failed", "data": {"step_id": "scan", "payload": {"error": "no such table"}}},
    ])
    assert _workflow(["run", "export"], client) == 1
    assert "no such table" in capsys.readouterr().out
    assert _workflow(["run", "export", "--input", "[1]"], client) == 1
    assert "Workflow input must be a JSON object" in capsys.readouterr().out
    assert _workflow(["run", "export", "--input", "@/no/such/file.json"], client) == 1
    assert len(client.runs) == 1


def test_workflow_status_resume_and_list(capsys) -> None:
    client = _WorkflowClient()
    assert _workflow(["status", "export", "run-1"], client) == 0
    assert _workflow(["resume", "export", "run-1"], client) == 0
    assert _workflow(["list"], client) == 0
    assert client.status == [("export", "run-1")]
    assert client.resumed == [("export", "run-1")]
    out = capsys.readouterr().out
    assert "completed" in out and "export (code)" in out
    with pytest.raises(SystemExit):
        _workflow([], client)
    capsys.readouterr()


def test_policy_put_reads_a_file_and_get_prints_the_policy(tmp_path, capsys) -> None:
    import json
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    client = MagicMock()
    client.put_policy.return_value = {"policy_id": "acme-ws", "sandbox": False}
    client.get_policy.return_value = {"policy_id": "acme-ws", "sandbox": False}
    path = tmp_path / "policy.json"
    path.write_text('{"sandbox": false}', encoding="utf-8")
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000", api_key="k")
    with patch.object(cli_main, "MashHostClient", return_value=client) as made, patch.object(
        cli_main, "load_config", return_value=existing
    ):
        assert cli_main.main(["policy", "put", "acme-ws", "--file", str(path)]) == 0
        assert cli_main.main(["policy", "get", "acme-ws"]) == 0
    client.put_policy.assert_called_once_with("acme-ws", {"sandbox": False})
    client.get_policy.assert_called_once_with("acme-ws")
    assert made.call_args.kwargs == {"api_key": "k"}
    printed = capsys.readouterr().out
    assert json.loads(printed.split("\n}\n")[0] + "\n}") == {"policy_id": "acme-ws", "sandbox": False}


def test_policy_put_refuses_a_file_that_is_not_an_object(tmp_path, capsys) -> None:
    from unittest.mock import MagicMock, patch

    from mash.cli import app as cli_main
    from mash.cli.config import CLIConfig

    client = MagicMock()
    path = tmp_path / "policy.json"
    path.write_text("[]", encoding="utf-8")
    existing = CLIConfig(api_base_url="http://127.0.0.1:8000")
    with patch.object(cli_main, "MashHostClient", return_value=client), patch.object(
        cli_main, "load_config", return_value=existing
    ):
        assert cli_main.main(["policy", "put", "acme-ws", "--file", str(path)]) == 1
    client.put_policy.assert_not_called()
    assert "JSON object" in capsys.readouterr().out
