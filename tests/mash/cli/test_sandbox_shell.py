"""``mash sandbox shell`` keeps the sandbox token out of the shell."""

from __future__ import annotations

from mash.cli.sandbox import _shell_argv


def test_shell_drops_the_sandbox_token_from_its_environment() -> None:
    argv = _shell_argv("mash-sandbox-abc", "builder", root=False)
    assert argv[-5:] == ["mash-sandbox-abc", "env", "-u", "MASH_SANDBOX_TOKEN", "/bin/bash"]
    assert argv[argv.index("-u") + 1] == "agent"
    assert argv[argv.index("-w") + 1] == "/home/agent/workspaces/builder"


def test_root_shell_starts_in_the_workspaces_root() -> None:
    argv = _shell_argv("mash-sandbox-abc", None, root=True)
    assert argv[argv.index("-u") + 1] == "root"
    assert argv[argv.index("-w") + 1] == "/home/agent/workspaces"
