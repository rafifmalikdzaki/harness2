"""Running the OpenCode solver for the general-task adapter.

The general path has no benchmark harness to lean on, so this module owns the
three things a real rollout needs:

1. **Staging** the editable harness into the agent's workspace in the shape
   OpenCode actually loads (``AGENTS.md``, ``.opencode/skills``, ``.opencode/
   agent``, ``.opencode/plugin``, ``scripts/``).
2. **Running** the solver in that workspace, fenced by ``OPENCODE_PERMISSION``
   so the session can only touch the workspace it was given.
3. **Reading back** the OpenCode event stream as the rollout's trajectory.

The launcher contract is ``(RunRequest, Install) -> list[Path]``; the adapter
builds one per rollout so the worktree is captured per call rather than
shared across concurrent rollouts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..substrate.opencode import OUTPUT_TOKEN_MAX
from ..substrate.render import render_opencode_tree

__all__ = ["GeneralRollout", "run_opencode_in_workspace", "stage_harness"]

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


def _permission(workspace: Path, extra: list[str] | None = None) -> str:
    """Deny everything outside the workspace, and the workspace itself."""
    allow = {"*": "deny", str(workspace): "allow", f"{workspace}/**": "allow"}
    for root in extra or []:
        allow[str(root)] = "allow"
        allow[f"{root}/**"] = "allow"
    return json.dumps({"external_directory": allow})


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
    for name in ("AGENTS.md",):
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
    extra_allowed: list[str] | None = None,
) -> GeneralRollout:
    """Run one OpenCode session in ``workspace`` and capture its event stream."""
    oc_dir = Path(opencode_dir or config.OPENCODE_DIR)
    bun_path = Path(bun or config.BUN)
    if not (oc_dir / "packages" / "opencode" / "src" / "index.ts").is_file():
        raise RuntimeError(
            f"OpenCode checkout missing at {oc_dir} (no packages/opencode/src/index.ts): "
            "run scripts/setup_benchmark.py to fetch the pinned checkout."
        )

    traj_dir = workspace / ".harness_traj"
    traj_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = traj_dir / "stdout.jsonl"
    stderr_path = traj_dir / "stderr.log"

    argv = [
        str(bun_path),
        "run",
        "--cwd",
        str(oc_dir / "packages" / "opencode"),
        "--conditions=browser",
        "src/index.ts",
        "run",
        prompt,
        "--dir",
        str(workspace),
        "--model",
        model,
        "--title",
        title,
        "--format",
        "json",
    ]

    env = {
        **os.environ,
        "OPENCODE_PERMISSION": _permission(workspace, extra_allowed),
        "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": str(int(OUTPUT_TOKEN_MAX)),
    }

    code = 1
    with open(stdout_path, "w", encoding="utf-8") as out, open(
        stderr_path, "w", encoding="utf-8"
    ) as err:
        try:
            code = subprocess.run(
                argv,
                cwd=oc_dir,
                env=env,
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
