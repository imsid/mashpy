"""``App``: an app's own client, built from the ``mash`` command line."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mash.cli import App, Command
from mash.cli import app as cli_main
from mash.cli.config import CLIConfig


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "MASH_API_BASE_URL",
        "MASH_API_KEY",
        "MASH_APP_ID",
        "MASH_POLICY_ID",
        "FUDGE_API_BASE_URL",
        "FUDGE_APP_ID",
        "FUDGE_POLICY_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _main(app: App, argv: list[str], saved: CLIConfig | None = None):
    client, shell = MagicMock(), MagicMock()
    with (
        patch.object(cli_main, "MashHostClient", return_value=client) as made,
        patch.object(cli_main, "load_config", return_value=saved),
        patch.object(cli_main, "Repl", shell),
    ):
        code = app.main(argv)
    return code, shell, made


def test_an_app_opens_its_repl_on_its_own_target_identity_and_look() -> None:
    hello = Command(name="hello", help="Say hello", handler=lambda ctx, args: None)
    lines = {"search": lambda args: "searching"}
    app = App(
        name="fudge",
        default_host="fudge",
        default_base_url="http://127.0.0.1:8001",
        default_app=lambda: "fudge-app",
        default_policy="fudge-policy",
        theme={"accent": "green"},
        commands=[hello],
        tool_lines=lines,
        workflow_renderers={"export": print},
    )
    code, shell, made = _main(app, [])
    assert code == 0
    assert made.call_args.args == ("http://127.0.0.1:8001",)
    target = shell.call_args.args[1]
    assert (target.host_id, target.agent_id, target.app_id, target.policy_id) == (
        "fudge",
        "companion",
        "fudge-app",
        "fudge-policy",
    )
    assert shell.call_args.kwargs == {
        "debug": False,
        "app_name": "fudge",
        "theme": {"accent": "green"},
        "tool_lines": lines,
    }
    shell.return_value.command_registry.register.assert_called_once_with(hello)
    shell.return_value.register_workflow_result_renderer.assert_called_once_with(
        "export", print
    )


def test_a_connection_saved_for_another_deployment_is_not_used() -> None:
    app = App(
        name="fudge", default_agent="fudge", default_base_url="http://127.0.0.1:8001"
    )
    other = CLIConfig(
        api_base_url="http://127.0.0.1:8000",
        api_key="k",
        host_id="h",
        app_id="app-1",
        policy_id="p-1",
    )
    code, shell, made = _main(app, ["repl"], other)
    assert code == 1  # no app or policy: the saved ones belong to the other deployment
    assert made.call_args.kwargs == {"api_key": None}
    same = CLIConfig(
        api_base_url="http://127.0.0.1:8001/",
        api_key="k",
        host_id="h",
        app_id="app-1",
        policy_id="p-1",
    )
    code, shell, made = _main(app, ["repl"], same)
    assert code == 0
    assert made.call_args.kwargs == {"api_key": "k"}
    target = shell.call_args.args[1]
    assert (target.agent_id, target.host_id, target.app_id, target.policy_id) == (
        "fudge",
        None,
        "app-1",
        "p-1",
    )


def test_the_apps_environment_prefix_wins_over_mash(monkeypatch) -> None:
    app = App(name="fudge", default_agent="fudge", env_prefix="FUDGE")
    monkeypatch.setenv("MASH_API_BASE_URL", "http://mash:8000")
    monkeypatch.setenv("FUDGE_API_BASE_URL", "http://fudge:8001")
    monkeypatch.setenv("MASH_APP_ID", "app-mash")
    monkeypatch.setenv("MASH_POLICY_ID", "p-mash")
    monkeypatch.setenv("FUDGE_POLICY_ID", "p-fudge")
    code, shell, made = _main(app, ["repl"])
    assert code == 0
    assert made.call_args.args == ("http://fudge:8001",)
    target = shell.call_args.args[1]
    assert (target.app_id, target.policy_id) == ("app-mash", "p-fudge")


def test_an_app_has_no_operator_commands_and_mash_does(capsys) -> None:
    app = App(name="fudge", default_host="fudge")
    for command in (["compose", "--host", "x"], ["host", "serve"], ["sandbox", "ls"]):
        with pytest.raises(SystemExit):
            cli_main.build_parser(app).parse_args(command)
    capsys.readouterr()
    assert (
        cli_main.build_parser().parse_args(["compose", "--host", "x"]).command
        == "compose"
    )
    assert cli_main.build_parser(app).prog == "fudge"


def test_an_app_takes_one_default_target() -> None:
    with pytest.raises(ValueError, match="not both"):
        App(name="fudge", default_host="a", default_agent="b")


def test_an_app_without_a_default_target_still_opens_the_repl() -> None:
    app = App(
        name="fudge",
        default_base_url="http://localhost:8001",
        default_app="sam",
        default_policy="p-1",
    )
    with patch.object(cli_main, "_resolve_target", return_value=("only-agent", None)):
        code, shell, _ = _main(app, [])
    assert code == 0
    assert shell.call_args.args[1].agent_id == "only-agent"


@pytest.mark.parametrize("override", ["flag", "env"])
def test_a_url_override_does_not_reuse_another_deployments_credentials(
    override, monkeypatch
) -> None:
    app = App(name="fudge", default_agent="fudge", default_app="sam", default_policy="p-1")
    args = ["repl"]
    if override == "flag":
        args += ["--api-base-url", "http://new:8000"]
    else:
        monkeypatch.setenv("MASH_API_BASE_URL", "http://new:8000")
    saved = CLIConfig(
        api_base_url="http://old:8000", api_key="old-key", app_id="old-app", policy_id="old-p"
    )
    code, shell, made = _main(app, args, saved)
    assert code == 0
    assert made.call_args.kwargs == {"api_key": None}
    assert (shell.call_args.args[1].app_id, shell.call_args.args[1].policy_id) == ("sam", "p-1")


def test_connect_uses_the_apps_identity_and_does_not_copy_a_foreign_one() -> None:
    app = App(
        name="fudge",
        default_base_url="http://new:8000",
        default_app=lambda: "sam",
        default_policy="p-1",
    )
    saved = CLIConfig(
        api_base_url="http://old:8000", api_key="old-key", app_id="old-app", policy_id="old-p"
    )
    with patch.object(cli_main, "save_config") as save:
        code, _, _ = _main(app, ["connect"], saved)
    assert code == 0
    config = save.call_args.args[0]
    assert (config.api_base_url, config.api_key, config.app_id, config.policy_id) == (
        "http://new:8000",
        None,
        "sam",
        "p-1",
    )


def test_explicit_flags_override_app_defaults_and_environment(monkeypatch) -> None:
    monkeypatch.setenv("FUDGE_APP_ID", "env-app")
    monkeypatch.setenv("FUDGE_POLICY_ID", "env-policy")
    monkeypatch.setenv("FUDGE_API_KEY", "env-key")
    app = App(
        name="fudge",
        default_host="fudge",
        default_base_url="http://default:8000",
        default_app="default-app",
        default_policy="default-policy",
        env_prefix="FUDGE",
    )
    code, shell, made = _main(
        app,
        [
            "repl",
            "--agent",
            "other",
            "--app-id",
            "flag-app",
            "--policy-id",
            "flag-policy",
            "--api-key",
            "flag-key",
        ],
    )
    assert code == 0
    assert made.call_args.kwargs == {"api_key": "flag-key"}
    target = shell.call_args.args[1]
    assert (target.agent_id, target.host_id, target.app_id, target.policy_id) == (
        "other",
        None,
        "flag-app",
        "flag-policy",
    )
