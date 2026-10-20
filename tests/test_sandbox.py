"""Tests for the sandbox: what a rollout can and cannot reach.

The sandbox is the layer that makes the fence a boundary rather than a
request. A path fence tells OpenCode what the session may do; only a
filesystem sandbox changes what the *process* may do -- and a shell command
runs with the operator's privileges either way.

These tests build the sandbox command and inspect it. The behaviour itself
was measured against a live model and is recorded in
docs/GENERAL_HARNESS_EXPERIMENTS.md.
"""

from pathlib import Path

from harness2.adapters.sandbox import sandbox_argv, sandbox_env


def pairs(argv: list[str]) -> dict[str, str]:
    """The bind/ro-bind/tmpfs mounts, as source -> target."""
    out: dict[str, str] = {}
    for i, a in enumerate(argv):
        if a in ("--bind", "--ro-bind", "--tmpfs") and i + 1 < len(argv):
            out[argv[i + 1]] = a
    return out


def test_the_workspace_is_writable(tmp_path):
    argv = sandbox_argv(tmp_path)
    assert pairs(argv).get(str(tmp_path)) == "--bind"


def test_the_workspace_is_the_working_directory(tmp_path):
    argv = sandbox_argv(tmp_path)
    assert "--chdir" in argv
    assert argv[argv.index("--chdir") + 1] == str(tmp_path)


def test_the_operator_home_does_not_exist(tmp_path):
    """The agent cannot read the operator's keys, history or token store."""
    argv = sandbox_argv(tmp_path)
    assert pairs(argv).get(str(Path.home())) == "--tmpfs"


def test_the_system_is_read_only(tmp_path):
    argv = sandbox_argv(tmp_path)
    mounts = pairs(argv)
    for path in ("/usr", "/bin", "/etc"):
        assert mounts.get(path) == "--ro-bind", path


def test_tmp_is_private(tmp_path):
    """A scratch space of its own, and nothing another process left in /tmp."""
    argv = sandbox_argv(tmp_path)
    assert pairs(argv).get("/tmp") == "--tmpfs"


def test_the_network_is_not_unshared(tmp_path):
    """The rollout has to reach the model provider."""
    argv = sandbox_argv(tmp_path)
    assert "--unshare-net" not in argv


def test_the_opencode_binary_is_reachable(tmp_path):
    """Otherwise the sandboxed process cannot even start."""
    exe = Path.home() / ".opencode" / "bin" / "opencode"
    if not exe.is_file():
        return  # nothing to bind on this machine
    argv = sandbox_argv(tmp_path, executable=exe)
    assert str(exe.parent) in pairs(argv)


def test_the_state_directory_is_reachable(tmp_path):
    """OpenCode's credentials live here; without it, no model call."""
    state = Path.home() / ".local" / "share" / "opencode"
    if not state.is_dir():
        return
    argv = sandbox_argv(tmp_path)
    assert str(state) in pairs(argv)


def test_a_workspace_under_tmp_is_still_exposed(tmp_path):
    """The proposer works in a scratch directory under /tmp.

    The tmpfs mount on /tmp comes first, so the workspace has to be bound
    after it or it would disappear.
    """
    ws = Path("/tmp/harness2-prop-example")
    argv = sandbox_argv(ws)
    mounts = pairs(argv)
    assert mounts.get(str(ws)) == "--bind"
    assert argv.index("--bind") > argv.index("--tmpfs")


def test_the_environment_keeps_home_but_adds_a_path(tmp_path):
    env = sandbox_env(tmp_path)
    assert env["HOME"] == str(Path.home())
    assert "PATH" in env
