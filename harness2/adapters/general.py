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
from . import agent
from .. import llm
from .worktree import WorktreeManager

__all__ = ["GeneralTaskAdapter"]


# The source room the improver reads: the repository's tracked text files,
# bounded. A repository under research can hold a dataset the size of a
# disk, and the evidence tree is built once per stage.
_SOURCE_ROOM_BYTES = 32_000_000
_SOURCE_ROOM_FILE_BYTES = 500_000


def _ensure_excluded(repo: Path, entry: str) -> None:
    """Ignore Harness² state in both .gitignore and .git/info/exclude."""
    gi = repo / ".gitignore"
    if gi.is_file():
        content = gi.read_text(encoding="utf-8", errors="replace")
        if entry not in content:
            gi.write_text(content.rstrip() + f"\n{entry}\n", encoding="utf-8")
    else:
        gi.write_text(f"{entry}\n", encoding="utf-8")

    git_dir = repo / ".git"
    if git_dir.is_file():  # worktree/submodule: "gitdir: <path>"
        git_dir = Path(git_dir.read_text().split(":", 1)[1].strip())
    ex = git_dir / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    content = ex.read_text(encoding="utf-8", errors="replace") if ex.is_file() else ""
    if entry not in content.splitlines():
        ex.write_text(content.rstrip() + ("\n" if content else "") + f"{entry}\n", encoding="utf-8")


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
        _ensure_excluded(self.repo_root, ".harness2/")
        _ensure_excluded(self.repo_root, ".harness_staged/")

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

    def _run_verify(self, wt_path: Path, timeout: int) -> tuple:
        if not self.verify_cmd:
            return None, ""
        try:
            proc = subprocess.run(
                self.verify_cmd, cwd=wt_path, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return proc.returncode == 0, (proc.stdout + "\n" + proc.stderr).strip()
        except Exception as e:  # timeout, missing binary, ...
            return False, f"Execution error: {e}"

    def execute(
        self,
        task_id: str,
        harness_dir: Optional[Path],
        run_id: str,
        *,
        timeout: int | None = None,
        strategy: str = "minimal",
        repair_rounds: int = 1,
        extra_files: Optional[List[str]] = None,
    ) -> Path:
        """One candidate: isolated worktree -> LLM edits -> verify -> repair -> independent critique."""
        label = run_id.replace("/", "__")
        wt_path = self.wt_mgr.create(self.session_id, label)
        timeout = timeout or 600
        strength = agent.verification_strength(self.verify_cmd)
        log: Dict[str, object] = {"strategy": strategy, "verification_strength": strength}

        provider = llm.resolve_provider()
        claims: List[str] = []
        if provider is None:
            log["llm"] = "not configured: verify-only run (no code was generated)"
            test_passed, test_output = self._run_verify(wt_path, timeout)
        else:
            log["llm"] = provider.public()
            memory_md = self.db.render_longterm_memory_markdown()
            feedback = ""
            test_passed, test_output = None, ""
            for attempt in range(1 + max(0, repair_rounds)):
                try:
                    res = agent.solve(wt_path, self.goal, strategy, memory_md, feedback, extra_files)
                except (llm.LLMError, ValueError) as e:
                    log.setdefault("errors", []).append(f"attempt {attempt}: {e}")
                    break
                claims = res["claims"] or claims
                log.setdefault("attempts", []).append({"plan": res["plan"], "touched": res["touched"]})
                test_passed, test_output = self._run_verify(wt_path, timeout)
                if test_passed is not False:
                    break
                feedback = f"Verification command `{self.verify_cmd}` failed:\n{test_output[-4000:]}"

        git_diff = self.wt_mgr.get_diff(self.session_id, label)
        review = (
            agent.critique(self.goal, git_diff, test_output, claims, strength)
            if provider is not None and git_diff.strip()
            else {"score": None, "issues": [], "unverified_claims": []}
        )
        log["review"] = review

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
        (run_dir / "candidate.json").write_text(
            __import__("json").dumps({**log, "claims": claims, "test_passed": test_passed}, indent=2),
            encoding="utf-8",
        )
        return run_dir

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
