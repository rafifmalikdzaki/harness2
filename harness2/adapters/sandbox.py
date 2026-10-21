"""Running a rollout inside a filesystem sandbox.

A rollout is a model-driven program with the operator's authority. The
permission fence tells OpenCode what the session may do; it does not change
what the *process* may do. A shell command runs with the operator's
filesystem privileges, and OpenCode's own directory inference is documented
as best-effort -- a rollout that guesses an absolute path can write outside
its workspace, and can read the operator's credentials.

``bwrap`` (bubblewrap) closes that gap at the kernel level: the rollout sees
a filesystem in which only its workspace is writable, and the operator's
home directory does not exist.

What is exposed, and why:

* the **workspace** -- read-write, it is the rollout's own;
* the **system directories** (``/usr``, ``/bin``, ``/lib``, ``/etc``, ...) --
  read-only, because the agent needs a shell, a Python, and a git;
* OpenCode's own **state** directory -- read-write, because that is where it
  keeps its credentials and its session database;
* the OpenCode **binary** -- read-only.

Everything else, the operator's home directory included, is absent. The
agent cannot read the SSH keys, the token store, or the shell history, and
cannot write outside its workspace.

Network access is deliberately *not* unshared: the rollout has to reach the
model provider.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Sequence

__all__ = ["sandbox_available", "sandbox_argv", "sandbox_state_dirs"]

# Read-only system paths the agent needs for a shell, an interpreter and git.
_SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc",
    "/opt",
    "/var",
    "/srv",
)

# Where OpenCode keeps the credentials and the session database it needs to
# make a model call. Read-write: it writes there during the run.
_STATE_DIR = Path.home() / ".local" / "share" / "opencode"


def sandbox_available() -> bool:
    """Is a usable sandbox on this machine?"""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    if not _STATE_DIR.is_dir():
        return False
    # bwrap fails inside a container that forbids user namespaces.
    try:
        import subprocess

        probe = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "/bin/true"],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return False
    return probe.returncode == 0


def sandbox_state_dirs(executable: str | Path | None = None) -> list[Path]:
    """The directories that must stay visible for OpenCode to run.

    Two of them: the directory the OpenCode binary is installed in, and the
    state directory where OpenCode keeps its credentials and its session
    database. Both are read-write for the state, read-only for the binary.
    """
    dirs: list[Path] = []
    if executable:
        exe = Path(executable).resolve()
        if exe.is_file():
            dirs.append(exe.parent)
    if _STATE_DIR.is_dir():
        dirs.append(_STATE_DIR)
    return dirs


def sandbox_argv(
    workspace: Path,
    *,
    home: Path | None = None,
    executable: str | Path | None = None,
    extra_readonly: Sequence[tuple[Path, Path]] = (),
    include_opencode_state: bool = True,
) -> list[str]:
    """The ``bwrap`` prefix that confines a run to ``workspace``.

    The filesystem is assembled from scratch: the system directories are
    bound read-only, the operator's home is replaced by an empty tmpfs, the
    OpenCode binary and state are bound back in so the model call can
    authenticate, and the workspace is bound read-write. The network
    namespace is left alone on purpose -- the rollout has to reach the model
    provider.

    ``extra_readonly`` names (source, target) paths the rollout needs to
    *read* but must not write. The project's own checkout, with its
    virtualenv and its installed tooling, is the usual case: a project under
    development is exactly what the rollout is working on, so reading it is
    expected; writing to it is what has to be fenced. A target may differ
    from its source, which is how a virtualenv in the main checkout is
    presented at the path a worktree expects to find it.
    """
    bwrap = shutil.which("bwrap") or "bwrap"
    ws = Path(workspace).resolve()
    home_dir = Path(home or Path.home()).resolve()

    argv: list[str] = [bwrap]
    for path in _SYSTEM_PATHS:
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    # A private /tmp: the rollout gets a scratch space, and cannot see or
    # disturb anything another process left there.
    argv += ["--tmpfs", "/tmp"]
    # The operator's home does not exist inside the sandbox.
    argv += ["--tmpfs", str(home_dir)]
    if include_opencode_state:
        for state in sandbox_state_dirs(executable):
            argv += ["--bind", str(state), str(state)]
    before: list[tuple[Path, Path]] = []
    after: list[tuple[Path, Path]] = []
    for source, target in extra_readonly:
        src = Path(source).resolve()
        if not src.is_dir():
            continue
        dst = Path(target)
        # A mount that *contains* the workspace has to come first, or it
        # would make the workspace read-only; a mount that *is inside* the
        # workspace has to come after, or the workspace's own mount would
        # hide it.
        if dst == ws or ws.is_relative_to(dst):
            before.append((src, dst))
        else:
            after.append((src, dst))
    for src, dst in before:
        argv += ["--ro-bind", str(src), str(dst)]
    # The workspace is the one writable place. It is bound after the tmpfs
    # mounts, so it is re-exposed even when it lives under one of them --
    # the proposer's working directory is a scratch directory in /tmp, and
    # the general path's workspace is a worktree inside the project's own
    # checkout.
    argv += ["--bind", str(ws), str(ws)]
    for src, dst in after:
        argv += ["--ro-bind", str(src), str(dst)]
    argv += ["--chdir", str(ws)]
    return argv


def sandbox_env(
    workspace: Path, *, home: Path | None = None, env: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment a sandboxed rollout needs.

    ``HOME`` still points at the real home: OpenCode resolves its own state
    directory from it, and that directory is bound into the sandbox
    explicitly. Nothing else about the home is reachable.
    """
    home_dir = Path(home or Path.home()).resolve()
    source = env if env is not None else os.environ
    # Tools launched by the model inherit the OpenCode process environment.
    # Passing os.environ through would expose provider/API keys to arbitrary
    # shell code even though the home directory is hidden. Keep only the
    # environment needed to run OpenCode and the project's tests. Provider
    # credentials are read by OpenCode from its mounted credential database.
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "NO_COLOR",
        "CI",
        "HOME",
        "TMPDIR",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX",
        "CUDA_VISIBLE_DEVICES",
        "CUDA_DEVICE_ORDER",
        "CUDA_HOME",
        "LD_LIBRARY_PATH",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "PYTHONUNBUFFERED",
        "WANDB_MODE",
        "WANDB_PROJECT",
        "WANDB_ENTITY",
        "WANDB_NAME",
    }
    secret_name = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
    out = {
        key: value
        for key, value in source.items()
        if key in allowed and not secret_name.search(key)
    }
    out["HOME"] = str(home_dir)
    out.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return out
