"""GeneralTaskAdapter: run the real Harness² loop on any repository and goal.

This is the general-task counterpart of the benchmark adapters. It satisfies
the same :class:`harness2.core.spec.BenchmarkAdapter` protocol, so the
Propose-Probe-Compose engine, the anonymised selection step and the
cross-task library all work unchanged:

    schedule.run_domain(domain, adapter.tasks(), k, adapter=adapter, prop=proposer, ...)

A rollout happens in an isolated git worktree. The solver is OpenCode, fenced
to that worktree. The harness the improver edits is rendered into the
workspace in the shape OpenCode loads, and the paths it authored are kept out
of the deliverable: they are scaffolding, not the agent's work.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..core import tree
from ..core.memory_db import MemoryDB
from ..core.spec import BenchPrompts, BenchSpec, ChannelSpec
from ..substrate.base import Install, RunRequest
from ..substrate.opencode import OpencodeSubstrate
from .general_runner import run_opencode_in_workspace, stage_harness
from .worktree import WorktreeManager

__all__ = ["GeneralTaskAdapter"]


def _ensure_gitignore_has(repo: Path, entry: str) -> None:
    gi = repo / ".gitignore"
    if gi.is_file():
        content = gi.read_text(encoding="utf-8", errors="replace")
        if entry not in content:
            gi.write_text(content.rstrip() + f"\n{entry}\n", encoding="utf-8")
    else:
        gi.write_text(f"{entry}\n", encoding="utf-8")


class GeneralTaskAdapter:
    """A benchmark adapter for arbitrary repositories, user goals and tests."""

    def __init__(
        self,
        repo_root: Path | str,
        goal: str,
        domain: str = "general",
        session_id: Optional[str] = None,
        verify_cmd: Optional[str] = None,
        runs_root: Optional[Path | str] = None,
        model: str = "",
        timeout: int = 1800,
    ):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.goal = goal.strip()
        self.domain = domain.strip()
        self.verify_cmd = verify_cmd.strip() if verify_cmd else None
        self.model = model.strip()
        self.timeout = int(timeout)

        # Internal state lives in .harness2 inside the repo; keep it ignored.
        self.harness_dir = self.repo_root / ".harness2"
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        _ensure_gitignore_has(self.repo_root, ".harness2/")
        _ensure_gitignore_has(self.repo_root, ".harness_staged/")

        self.db = MemoryDB(self.harness_dir / "memory.db")
        self.session_id = session_id or self.db.start_session(self.goal, self.domain)

        # Per-repo worktree root: the general path never shares state through
        # a global temp dir, and it lands next to the .harness2 state.
        wt_root = self.harness_dir / "worktrees"
        wt_root.mkdir(parents=True, exist_ok=True)
        self.wt_mgr = WorktreeManager(self.repo_root, base_temp_dir=wt_root)
        self.substrate = OpencodeSubstrate(launcher=self._unused_launcher)

        actual_runs_root = (
            Path(runs_root).expanduser().resolve()
            if runs_root
            else self.harness_dir / "runs"
        )
        channel = ChannelSpec("opencode", "general/solver")
        self.spec = BenchSpec(
            name="general",
            runs_root=actual_runs_root,
            install_mode="workspace_agents_md",
            prompts=BenchPrompts(work=self.goal),
            channels={"proposer": channel, "selector": channel},
        )
        self._ensure_base_harness()

    # ------------------------------------------------------------------
    # harness
    # ------------------------------------------------------------------

    def _ensure_base_harness(self) -> Path:
        base_dir = self.harness_dir / "base"
        base_dir.mkdir(parents=True, exist_ok=True)

        surface = self.substrate.surface()
        base_components = {name: "" for name in surface.always_on}

        # c3: the project's accumulated long-term memory.
        base_components["LongTermMEMORY.md"] = self.db.render_longterm_memory_markdown()
        # c1: the task goal.
        base_components["systemprompt.md"] = f"Task Goal:\n{self.goal}\n"

        tree.write_tree(surface, base_components, base_dir, stubs=False)
        return base_dir

    def _render_ctx(self) -> dict:
        """Template slots for the harness body, describing this repository."""
        return {
            "env_description": (
                "A git working copy of the user's own repository. Edit files in place; "
                "the working tree is a disposable sandbox and only your changes are kept."
            ),
            "domain_focus": self.domain,
            "date": datetime.now(timezone.utc).date().isoformat(),
            "working_directory": "the repository root; edit files in place",
        }

    # ------------------------------------------------------------------
    # protocol
    # ------------------------------------------------------------------

    def tasks(self, split: str = "all") -> List[str]:
        return [self.session_id]

    def domain_of(self, task_id: str) -> str:
        return self.domain

    def task_prompt(self, task_id: str) -> str:
        prompt = f"Goal: {self.goal}\n"
        if self.verify_cmd:
            prompt += f"Verification Command: `{self.verify_cmd}`\n"
        return prompt

    def task_files(self, task_id: str) -> Optional[Path]:
        return self.repo_root

    def base_harness(self) -> Path:
        return self.harness_dir / "base"

    def validate_tree(self, harness: Dict[str, str]) -> None:
        return None

    # ------------------------------------------------------------------
    # rollout
    # ------------------------------------------------------------------

    def _unused_launcher(self, req: RunRequest, install: Install) -> list[Path]:
        raise RuntimeError(
            "the general adapter builds a per-rollout launcher; substrate.execute() "
            "must not be called on the shared instance"
        )

    def _launcher(self, worktree: Path, timeout: int):
        """A launcher bound to one worktree, so concurrent rollouts stay isolated."""

        def launch(req: RunRequest, install: Install) -> list[Path]:
            authored = stage_harness(install.staging_dir, worktree, self._render_ctx())
            (worktree / ".harness_authored").write_text(
                "\n".join(sorted(authored)), encoding="utf-8"
            )

            rollout = run_opencode_in_workspace(
                workspace=worktree,
                prompt=self.task_prompt(req.task_ref),
                model=self.model,
                title=f"general | {req.task_ref}",
                timeout=timeout,
            )
            return [rollout.trajectory.parent]

        return launch

    def execute(
        self,
        task_id: str,
        harness_dir: Optional[Path],
        run_id: str,
        *,
        timeout: int | None = None,
    ) -> Path:
        """Execute one rollout: an isolated worktree, the solver, and the evidence."""
        label = run_id.replace("/", "__")
        wt_path = self.wt_mgr.create(self.session_id, label)
        wall = int(timeout or self.timeout)

        active = harness_dir or self.base_harness()
        install = self.substrate.materialise(active, self.spec, {})
        try:
            substrate = OpencodeSubstrate(launcher=self._launcher(wt_path, wall))
            substrate.execute(task_id, install, [run_id], wall)
        finally:
            shutil.rmtree(install.staging_dir, ignore_errors=True)

        # The harness's own files are scaffolding, not the agent's work.
        authored = self._authored_paths(wt_path)
        git_diff = self.wt_mgr.get_diff(self.session_id, label, exclude=authored)

        test_passed = None
        test_output = ""
        if self.verify_cmd:
            try:
                proc = subprocess.run(
                    self.verify_cmd,
                    cwd=wt_path,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=min(wall, 300),
                )
                test_passed = proc.returncode == 0
                test_output = (proc.stdout + "\n" + proc.stderr).strip()
            except Exception as e:
                test_passed = False
                test_output = f"Execution error: {e}"

        self.db.record_rollout(
            session_id=self.session_id,
            label=label,
            worktree_path=str(wt_path),
            git_diff=git_diff,
            test_passed=test_passed,
            test_output=test_output,
        )

        run_dir = self.spec.runs_root / self.session_id / label
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "diff.patch").write_text(git_diff, encoding="utf-8")
        (run_dir / "test_output.txt").write_text(test_output, encoding="utf-8")
        (run_dir / "worktree_path.txt").write_text(str(wt_path), encoding="utf-8")
        traj = wt_path / ".harness_traj" / "trajectory.jsonl"
        if traj.is_file():
            (run_dir / "trajectory.jsonl").write_text(
                traj.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
            )
        return run_dir

    def _authored_paths(self, worktree: Path) -> list[str]:
        """The harness-authored paths in this worktree, from the launcher's record."""
        marker = worktree / ".harness_authored"
        if not marker.is_file():
            return []
        return [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ------------------------------------------------------------------
    # evidence
    # ------------------------------------------------------------------

    def delivered(self, run_dir: Path) -> bool:
        """Did the rollout produce anything the selector can look at?"""
        diff = run_dir / "diff.patch"
        if not diff.is_file():
            return False
        return bool(diff.read_text(encoding="utf-8", errors="replace").strip())

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        """The solver's event stream, rendered in the shared grammar."""
        traj = run_dir / "trajectory.jsonl"
        if traj.is_file():
            text = traj.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text[: max(1, obs_trunc)]
        diff = (run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace")
        out = (run_dir / "test_output.txt").read_text(encoding="utf-8", errors="replace")
        return f"[DIFF]\n{diff}\n[VERIFICATION]\n{out}"

    def deliverable_text(self, run_dir: Path) -> str:
        """The agent's actual changes to the repository."""
        return (run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace")

    def deliverable_files(self, run_dir: Path) -> Dict[str, str]:
        return {
            "diff.patch": self.deliverable_text(run_dir),
            "test_output.txt": (run_dir / "test_output.txt").read_text(
                encoding="utf-8", errors="replace"
            ),
        }

    def score(self, run_dir: Path, task_id: str) -> Optional[dict]:
        """Grade a rollout with the verification command, when one was given.

        No verifier means no grade: the judge path treats ``None`` as
        "unjudged", never as a zero.
        """
        if not self.verify_cmd:
            return None
        passed = (run_dir / "test_output.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        ok = "(no failures)" in passed or passed.strip() == ""
        return {"n_passed": 1 if ok else 0, "n_criteria": 1, "detail": passed[:2000]}
