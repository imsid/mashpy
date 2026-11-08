"""``mash sandbox shell`` keeps the sandbox token out of the shell."""

from __future__ import annotations

from argparse import Namespace

from mash.cli.sandbox import _run_shell, _shell_argv, agent_uid_from_map


def test_shell_drops_the_sandbox_token_and_runs_as_the_agents_uid() -> None:
    argv = _shell_argv("mash-sandbox-abc", "builder", uid=20001)
    assert argv[-5:] == ["mash-sandbox-abc", "env", "-u", "MASH_SANDBOX_TOKEN", "/bin/bash"]
    assert argv[argv.index("-u") + 1] == "20001:20001"
    assert argv[argv.index("-w") + 1] == "/home/agent/workspaces/builder"


def test_root_shell_starts_in_the_workspaces_root() -> None:
    argv = _shell_argv("mash-sandbox-abc", None, uid=None)
    assert argv[argv.index("-u") + 1] == "root"
    assert argv[argv.index("-w") + 1] == "/home/agent/workspaces"


def test_the_agents_uid_comes_from_the_map_on_the_volume() -> None:
    text = '{"builder": 20001, "pilot": 20000}'
    assert agent_uid_from_map(text, "builder") == 20001
    assert agent_uid_from_map(text, "writer") is None
    assert agent_uid_from_map("", "builder") is None


def test_a_shell_needs_an_agent_or_root(capsys) -> None:
    code = _run_shell(Namespace(user_id="u-1", agent=None, root=False, namespace=None))
    assert code == 1
    assert "--agent" in capsys.readouterr().out + capsys.readouterr().err
