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
from typing import Dict, List, Optional, Sequence

from ..core import tree
from ..core.compress import read_text
from ..core.memory_db import MemoryDB
from ..core.spec import BenchPrompts, BenchSpec, ChannelSpec
from ..substrate.base import Install, RunRequest
from ..substrate.opencode import OpencodeSubstrate
from .general_runner import run_opencode_in_workspace, stage_harness
from .sandbox import sandbox_argv, sandbox_available, sandbox_env
from .worktree import WorktreeManager

__all__ = ["GeneralTaskAdapter"]


# The source room the improver reads: the repository's tracked text files,
# bounded. A repository under research can hold a dataset the size of a
# disk, and the evidence tree is built once per stage.
_SOURCE_ROOM_BYTES = 32_000_000
_SOURCE_ROOM_FILE_BYTES = 500_000


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
        selector_model: str = "",
        timeout: int = 1800,
        allow_mcp: bool = False,
        sandbox: bool = True,
    ):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.goal = goal.strip()
        self.domain = domain.strip()
        self.verify_cmd = verify_cmd.strip() if verify_cmd else None
        self.model = model.strip()
        # The proposer and the selector may run on a different model from the
        # solver. The channel spec has to name a real model: the scheduler
        # builds the selector's proposer from it, and a placeholder there
        # fails the selection session with "Model unavailable".
        self.selector_model = (selector_model or model).strip()
        # The verification verdict per rollout label, recorded as it ran.
        self._verdicts: dict[str, bool] = {}
        self.timeout = int(timeout)
        # A rollout does not inherit the operator's MCP servers by default:
        # they may hold credentials, and they are a route out of the
        # workspace that a path fence does not close.
        self.allow_mcp = bool(allow_mcp)
        # And it runs in a filesystem sandbox where the operator's home does
        # not exist, so a shell command cannot write outside the workspace or
        # read the operator's keys.
        self.sandbox = bool(sandbox) and sandbox_available()

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
        channel = ChannelSpec("opencode", self.selector_model)
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
        """The source room the improver reads.

        For a general task the material *is* the repository -- but the
        repository can hold a dataset the size of a disk, and the evidence
        tree is built once per stage. So the room is prepared once: the
        repository's tracked text files, bounded.
        """
        return self._source_room()

    def _source_room(self) -> Optional[Path]:
        """A bounded copy of the repository's tracked text files.

        The rollout works in the repository itself, so this is not the
        agent's material -- it is the improver's, so that a proposal can be
        grounded in the code the rollout was working on rather than only in
        the transcript.
        """
        room = self.harness_dir / "source_room"
        marker = room / ".complete"
        if marker.is_file():
            return room if any(room.rglob("*")) else None
        shutil.rmtree(room, ignore_errors=True)
        room.mkdir(parents=True, exist_ok=True)
        total = 0
        for rel in self._tracked_text_files():
            src = self.repo_root / rel
            try:
                size = src.stat().st_size
            except OSError:
                continue
            if total + size > _SOURCE_ROOM_BYTES:
                break
            dst = room / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            total += size
        marker.write_text(f"{total}\n", encoding="utf-8")
        return room if total else None

    def _tracked_text_files(self) -> list[str]:
        """The repository's tracked files that are text and small enough."""
        try:
            res = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception:
            return []
        if res.returncode != 0:
            return []
        out: list[str] = []
        for rel in res.stdout.split("\0"):
            if not rel or rel.startswith(".harness"):
                continue
            p = self.repo_root / rel
            try:
                if not p.is_file() or p.stat().st_size > _SOURCE_ROOM_FILE_BYTES:
                    continue
                head = p.read_bytes()[:1024]
            except OSError:
                continue
            if b"\0" in head:
                continue  # binary
            out.append(rel)
        return out

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
            # The sandbox mounts the project's virtualenv at the path a
            # worktree expects to find it, and bwrap creates that mount
            # point on the real filesystem. It is scaffolding, like the
            # harness files, so it must not read as the agent's work.
            venv = worktree / ".venv"
            if venv.is_dir():
                authored.append(".venv")
            (worktree / ".harness_authored").write_text(
                "\n".join(sorted(authored)), encoding="utf-8"
            )

            rollout = run_opencode_in_workspace(
                workspace=worktree,
                prompt=self.task_prompt(req.task_ref),
                model=self.model,
                title=f"general | {req.task_ref}",
                timeout=timeout,
                allow_mcp=self.allow_mcp,
                sandbox=self.sandbox,
                # The project's own checkout, read-only: the rollout needs
                # its virtualenv and its installed tooling to run the
                # project's tests. Writing to the checkout is what the
                # sandbox forbids -- the worktree is where changes go.
                sandbox_readonly=self._sandbox_readonly(worktree),
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
        before = self._tree_state()

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

        # The fence is a boundary on paths, not a guarantee: OpenCode's own
        # session and worktree tools can relocate a session or register a
        # worktree of another repository, and the shell runs with the
        # operator's authority. So the rollout is also checked afterwards,
        # and anything it left outside its worktree is reported rather than
        # discovered later.
        leaks = self._detect_leaks(wt_path, before)

        test_passed = None
        test_output = ""
        if self.verify_cmd:
            try:
                if self.sandbox:
                    # Verify in the same filesystem view as the rollout.
                    # In particular, the project's venv is mounted at
                    # worktree/.venv only inside bwrap; running pytest on the
                    # host would see an empty mountpoint and falsely fail.
                    cmd = sandbox_argv(
                        wt_path,
                        extra_readonly=self._sandbox_readonly(wt_path),
                        include_opencode_state=False,
                    ) + ["/bin/sh", "-c", self.verify_cmd]
                    proc = subprocess.run(
                        cmd,
                        cwd=wt_path,
                        env=sandbox_env(wt_path),
                        capture_output=True,
                        text=True,
                        timeout=min(wall, 300),
                    )
                else:
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
            # The verdict goes into the output the selector reads. A test
            # runner's own summary line is its own business -- with `-q` it
            # may print none -- but the exit status is not ambiguous, and a
            # selector shown only a diff cannot tell a passing candidate
            # from a failing one.
            test_output = (
                f"VERIFICATION: {'PASSED' if test_passed else 'FAILED'} "
                f"(exit status {0 if test_passed else 1})\n\n{test_output}"
            )

        self._verdicts[label] = bool(test_passed)

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
        if leaks:
            (run_dir / "leaks.txt").write_text("\n".join(leaks) + "\n", encoding="utf-8")
        traj = wt_path / ".harness_traj" / "trajectory.jsonl.gz"
        if traj.is_file():
            (run_dir / "trajectory.jsonl.gz").write_bytes(traj.read_bytes())
        return run_dir

    def _sandbox_readonly(self, worktree: Path) -> list[tuple[Path, Path]]:
        """What the rollout may read but not write, and where it may read it.

        The project's own checkout is readable: a rollout working on a
        project needs to see it. Its virtualenv is presented at the path a
        worktree expects to find it, because a worktree has no environment
        of its own.

        The interpreter behind that virtualenv is usually *not* in the
        project -- a uv-managed CPython, a pyenv build, a conda prefix --
        and it is reached through a symlink. The sandbox hides the
        operator's home, so the target of that symlink is bound in as well;
        without it the rollout's ``.venv/bin/python`` is a dangling link.
        """
        pairs: list[tuple[Path, Path]] = [(self.repo_root, self.repo_root)]
        venv = self.repo_root / ".venv"
        if not venv.is_dir():
            return pairs
        # bwrap's target for --ro-bind must exist. A git worktree has no
        # copy of the ignored virtualenv, so create an empty mountpoint; it
        # is ignored by the repository and the source venv is only mounted
        # over it inside the sandbox.
        venv_target = worktree / ".venv"
        venv_target.mkdir(parents=True, exist_ok=True)
        pairs.append((venv, venv_target))
        for interpreter in self._interpreters(venv):
            if interpreter.is_dir() and interpreter not in {self.repo_root}:
                pairs.append((interpreter, interpreter))
        return pairs

    def _interpreters(self, venv: Path) -> list[Path]:
        """The real directories behind a virtualenv's python executables.

        A uv-managed or pyenv interpreter is a whole installation, not a
        single binary: it resolves its standard library relative to the
        executable, so the prefix around it has to be bound in as well.
        """
        found: list[Path] = []
        for name in ("python", "python3"):
            link = venv / "bin" / name
            if not link.is_symlink():
                continue
            try:
                real = link.resolve(strict=True)
            except OSError:
                continue
            if not real.is_file():
                continue
            # <prefix>/bin/python -> <prefix>
            prefix = real.parent.parent if real.parent.name == "bin" else real.parent
            if prefix not in found:
                found.append(prefix)
        return found

    def _authored_paths(self, worktree: Path) -> list[str]:
        """The harness-authored paths in this worktree, from the launcher's record."""
        marker = worktree / ".harness_authored"
        if not marker.is_file():
            return []
        return [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _detect_leaks(self, worktree: Path, before: set[str]) -> list[str]:
        """What the rollout left outside its own worktree.

        The worktree is a git worktree of the user's repository, so a write
        outside it is either in the user's tree or in some other repository
        entirely -- the case that produced a stray ``fix-calc/`` directory
        inside the harness's own checkout.

        Two things are checked:

        * the **worktree registry** of the user's repository, for entries
          that appeared during the rollout;
        * the **user's own working tree**, for files that appeared during
          the rollout. The state before the rollout is subtracted, so the
          harness's own housekeeping (the ``.gitignore`` entry it adds, for
          instance) is not mistaken for a leak.

        The check is a report, not a prevention: the fence has already
        denied the paths it can see. This catches the paths it cannot.
        """
        leaks: list[str] = []
        try:
            leaks.extend(self._leaked_worktrees(worktree))
        except Exception:
            pass
        try:
            leaks.extend(self._leaked_files(before))
        except Exception:
            pass
        return leaks

    def _tree_state(self) -> set[str]:
        """The paths the user's working tree currently reports as changed."""
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            return set()
        return {ln[3:].strip() for ln in res.stdout.splitlines() if len(ln) >= 4}

    def _leaked_worktrees(self, worktree: Path) -> list[str]:
        """Worktrees registered against the user's repository, outside ours."""
        out: list[str] = []
        try:
            res = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            return out
        ours = str(worktree.resolve())
        for line in res.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            path = line[len("worktree ") :].strip()
            if path == ours or str(self.repo_root) == path:
                continue
            # A worktree of the user's repo that is not ours, and that is not
            # one of the harness's own rollouts.
            if path.startswith(str(self.harness_dir.resolve())):
                continue
            if "harness2" in path or "harness2-prop" in path:
                continue
            out.append(f"worktree registered outside the rollout: {path}")
        return out

    def _leaked_files(self, before: set[str]) -> list[str]:
        """Files in the user's working tree that the rollout added."""
        return [
            f"file in the user's tree: {name}"
            for name in sorted(self._tree_state() - before)
            if not name.startswith(".harness")
        ]

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
        for name in ("trajectory.jsonl.gz", "trajectory.jsonl"):
            traj = run_dir / name
            if traj.is_file():
                text = read_text(traj).strip()
                if text:
                    return text[: max(1, obs_trunc)]
                break
        diff = (run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace")
        out = (run_dir / "test_output.txt").read_text(encoding="utf-8", errors="replace")
        return f"[DIFF]\n{diff}\n[VERIFICATION]\n{out}"

    def deliverable_text(self, run_dir: Path) -> str:
        """The agent's actual changes to the repository."""
        return (run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace")

    def deliverable_assets(self, run_dir: Path) -> list[Path]:
        """The verifier's verdict on this candidate, as evidence for the selector.

        A verification command is only useful if the selector can see it: a
        candidate that passes and a candidate that fails look identical in
        a diff. So the verdict travels with the candidate.
        """
        run_dir = Path(run_dir)
        if not self.verify_cmd:
            return []
        out = run_dir / "test_output.txt"
        return [out] if out.is_file() else []

    def deliverable_assets_note(self, names: Sequence[str]) -> str:
        """Say what the asset is, so the selector reads it as a verdict."""
        return (
            "<!-- the verification command's output for this candidate: "
            + ", ".join(names)
            + ". Read it before judging: a candidate whose verification failed "
            "is not a candidate that solved the work. -->\n"
        )

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

        The verdict is the verification command's **exit status**, recorded
        when the rollout ran -- not a guess made from its output. A test
        runner's output format is its own business; its exit status is not.
        """
        if not self.verify_cmd:
            return None
        passed = (run_dir / "test_output.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        ok = self._verdicts.get(Path(run_dir).name)
        if ok is None:
            # No recorded status: the command never ran. That is not a pass.
            ok = False
        return {"n_passed": 1 if ok else 0, "n_criteria": 1, "detail": passed[:2000]}
