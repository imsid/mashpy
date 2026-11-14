"""``App``: an app's own client, built from the ``mash`` command line."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mash.cli import App, Command
from mash.cli import main as cli_main
from mash.cli.config import CLIConfig


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("MASH_API_BASE_URL", "MASH_API_KEY", "MASH_USER_ID", "FUDGE_API_BASE_URL",
                 "FUDGE_USER_ID"):
        monkeypatch.delenv(name, raising=False)


def _main(app: App, argv: list[str], saved: CLIConfig | None = None):
    client, shell = MagicMock(), MagicMock()
    with patch.object(cli_main, "MashHostClient", return_value=client) as made, patch.object(
        cli_main, "load_config", return_value=saved
    ), patch.object(cli_main, "Repl", shell):
        code = app.main(argv)
    return code, shell, made


def test_an_app_opens_its_repl_on_its_own_target_user_and_look() -> None:
    hello = Command(name="hello", help="Say hello", handler=lambda ctx, args: None)
    lines = {"search": lambda args: "searching"}
    app = App(name="fudge", default_host="fudge", default_base_url="http://127.0.0.1:8001",
              default_user=lambda: "sam", theme={"accent": "green"}, commands=[hello],
              tool_lines=lines, workflow_renderers={"export": print})
    code, shell, made = _main(app, [])
    assert code == 0
    assert made.call_args.args == ("http://127.0.0.1:8001",)
    target = shell.call_args.args[1]
    assert (target.host_id, target.agent_id, target.user_id) == ("fudge", "companion", "sam")
    assert shell.call_args.kwargs == {"debug": False, "app_name": "fudge",
                                      "theme": {"accent": "green"}, "tool_lines": lines}
    shell.return_value.command_registry.register.assert_called_once_with(hello)
    shell.return_value.register_workflow_result_renderer.assert_called_once_with("export", print)


def test_a_connection_saved_for_another_deployment_is_not_used() -> None:
    app = App(name="fudge", default_agent="fudge", default_base_url="http://127.0.0.1:8001")
    other = CLIConfig(api_base_url="http://127.0.0.1:8000", api_key="k", host_id="h", user_id="u-1")
    code, shell, made = _main(app, ["repl"], other)
    assert code == 1  # no user: the saved one belongs to the other deployment
    assert made.call_args.kwargs == {"api_key": None}
    same = CLIConfig(api_base_url="http://127.0.0.1:8001/", api_key="k", host_id="h", user_id="u-1")
    code, shell, made = _main(app, ["repl"], same)
    assert code == 0
    assert made.call_args.kwargs == {"api_key": "k"}
    target = shell.call_args.args[1]
    assert (target.agent_id, target.host_id, target.user_id) == ("fudge", None, "u-1")


def test_the_apps_environment_prefix_wins_over_mash(monkeypatch) -> None:
    app = App(name="fudge", default_agent="fudge", env_prefix="FUDGE")
    monkeypatch.setenv("MASH_API_BASE_URL", "http://mash:8000")
    monkeypatch.setenv("FUDGE_API_BASE_URL", "http://fudge:8001")
    monkeypatch.setenv("MASH_USER_ID", "u-mash")
    code, shell, made = _main(app, ["repl"])
    assert code == 0
    assert made.call_args.args == ("http://fudge:8001",)
    assert shell.call_args.args[1].user_id == "u-mash"


def test_an_app_has_no_operator_commands_and_mash_does(capsys) -> None:
    app = App(name="fudge", default_host="fudge")
    for command in (["compose", "--host", "x"], ["host", "serve"], ["sandbox", "ls"]):
        with pytest.raises(SystemExit):
            cli_main.build_parser(app).parse_args(command)
    capsys.readouterr()
    assert cli_main.build_parser().parse_args(["compose", "--host", "x"]).command == "compose"
    assert cli_main.build_parser(app).prog == "fudge"


def test_an_app_takes_one_default_target() -> None:
    with pytest.raises(ValueError, match="not both"):
        App(name="fudge", default_host="a", default_agent="b")
