"""Running the OpenCode solver for the general-task adapter.

The general path has no benchmark harness to lean on, so this module owns the
three things a real rollout needs:

1. **Staging** the editable harness into the agent's workspace in the shape
   OpenCode actually loads (``AGENTS.md``, ``.opencode/skills``, ``.opencode/
   agent``, ``.opencode/plugin``, ``scripts/``).
2. **Running** the solver in that workspace, fenced so the session can only
   touch the workspace it was given.
3. **Reading back** the OpenCode event stream as the rollout's trajectory.

Two runtimes are supported:

* the **pinned source checkout** (``dependencies/opencode``) run through bun --
  what the paper's numbers were produced with, and what the benchmarks use;
* an **installed ``opencode`` binary** on PATH -- the pragmatic choice when
  the checkout has not been fetched. It is a different build, so the fence
  is expressed the way V2 expects (a config document) rather than the V1
  environment variable the pinned version takes.

The launcher contract is ``(RunRequest, Install) -> list[Path]``; the adapter
builds one per rollout so the worktree is captured per call rather than
shared across concurrent rollouts.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..substrate.opencode import opencode_command
from ..substrate.render import render_opencode_tree

__all__ = [
    "GeneralRollout",
    "OpenCodeRuntime",
    "resolve_runtime",
    "run_opencode_in_workspace",
    "stage_harness",
]

EVENT_PREFIX = '{"type":'


@dataclass
class GeneralRollout:
    """What one solver run left behind in the workspace."""

    worktree: Path
    trajectory: Path
    stdout_path: Path
    stderr_path: Path
    exit_code: int
    harness_paths: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class OpenCodeRuntime:
    """How to invoke OpenCode: from source, or from an installed binary."""

    kind: str  # "source" | "binary"
    argv: list[str]
    cwd: Path
    env: dict[str, str]


def resolve_runtime(
    *,
    workspace: Path,
    prompt: str,
    model: str,
    title: str,
    opencode_dir: Path | None = None,
    bun: Path | None = None,
    opencode_bin: str | None = None,
    extra_allowed: list[str] | None = None,
    allow_mcp: bool = False,
    sandbox: bool = False,
) -> OpenCodeRuntime:
    """Pick the OpenCode runtime and build the argv for it.

    The pinned source checkout wins when it is present: it is the build the
    paper's numbers came from. Otherwise an installed binary is used, which
    is what a developer already has.
    """
    command = opencode_command(
        prompt=prompt,
        workspace=workspace,
        model=model,
        title=title,
        opencode_dir=opencode_dir,
        bun=bun,
        binary=opencode_bin,
        allow_mcp=allow_mcp,
        sandbox=sandbox,
    )
    return OpenCodeRuntime(
        kind=command.kind,
        argv=command.argv,
        cwd=command.cwd,
        env=command.env,
    )


def stage_harness(staged: Path, workspace: Path, ctx: dict) -> list[str]:
    """Render the harness into the workspace and return the paths it authored.

    The workspace is the agent's sandbox, so the harness files have to sit at
    its root for OpenCode to load them. They are recorded so the caller can
    keep them out of the deliverable diff: they are scaffolding, not the
    agent's work.
    """
    render_dir = workspace / ".harness_render"
    render_opencode_tree(staged, render_dir, ctx)

    authored: list[str] = []
    name = "AGENTS.md"
    if (render_dir / name).is_file():
        shutil.copy(render_dir / name, workspace / name)
        authored.append(name)
    for sub in (".opencode", "scripts"):
        src = render_dir / sub
        if not src.is_dir():
            continue
        dst = workspace / sub
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        for p in sorted(dst.rglob("*")):
            if p.is_file():
                authored.append(str(p.relative_to(workspace)))
    shutil.rmtree(render_dir, ignore_errors=True)
    return authored


def run_opencode_in_workspace(
    *,
    workspace: Path,
    prompt: str,
    model: str,
    title: str,
    timeout: int,
    opencode_dir: Path | None = None,
    bun: Path | None = None,
    opencode_bin: str | None = None,
    extra_allowed: list[str] | None = None,
    allow_mcp: bool = False,
    sandbox: bool = False,
) -> GeneralRollout:
    """Run one OpenCode session in ``workspace`` and capture its event stream."""
    runtime = resolve_runtime(
        workspace=workspace,
        prompt=prompt,
        model=model,
        title=title,
        opencode_dir=opencode_dir,
        bun=bun,
        opencode_bin=opencode_bin,
        extra_allowed=extra_allowed,
        allow_mcp=allow_mcp,
        sandbox=sandbox,
    )

    traj_dir = workspace / ".harness_traj"
    traj_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = traj_dir / "stdout.jsonl"
    stderr_path = traj_dir / "stderr.log"

    code = 1
    with open(stdout_path, "w", encoding="utf-8") as out, open(
        stderr_path, "w", encoding="utf-8"
    ) as err:
        try:
            code = subprocess.run(
                runtime.argv,
                cwd=runtime.cwd,
                env=runtime.env,
                stdout=out,
                stderr=err,
                timeout=timeout,
            ).returncode
        except subprocess.TimeoutExpired:
            err.write(f"\n[general] TIMEOUT after {timeout}s\n")

    # The trajectory is the event stream, not the raw stdout: keep only the
    # JSON event lines so the reader sees actions and observations.
    with open(stdout_path, encoding="utf-8", errors="replace") as src:
        events = "".join(ln for ln in src if ln.startswith(EVENT_PREFIX))
    trajectory = traj_dir / "trajectory.jsonl"
    trajectory.write_text(events, encoding="utf-8")

    return GeneralRollout(
        worktree=workspace,
        trajectory=trajectory,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        exit_code=code,
    )
