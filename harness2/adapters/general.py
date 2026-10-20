# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GeneralTaskAdapter: Adapts arbitrary user tasks on real repositories for Harness²."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ..core import tree
from ..core.memory_db import MemoryDB
from ..core.spec import BenchPrompts, BenchSpec, ChannelSpec
from ..substrate.opencode import OpencodeSubstrate
from .worktree import WorktreeManager


class GeneralTaskAdapter:
    """BenchmarkAdapter implementation for arbitrary repositories, user goals, and test verification."""

    def __init__(
        self,
        repo_root: Path | str,
        goal: str,
        domain: str = "general",
        session_id: Optional[str] = None,
        verify_cmd: Optional[str] = None,
        runs_root: Optional[Path | str] = None,
    ):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.goal = goal.strip()
        self.domain = domain.strip()
        self.verify_cmd = verify_cmd.strip() if verify_cmd else None

        self.harness_dir = self.repo_root / ".harness2"
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        self.db = MemoryDB(self.harness_dir / "memory.db")
        self.session_id = session_id or self.db.start_session(self.goal, self.domain)

        self.wt_mgr = WorktreeManager(self.repo_root)
        self.substrate = OpencodeSubstrate()

        actual_runs_root = (
            Path(runs_root).expanduser().resolve()
            if runs_root
            else self.harness_dir / "runs"
        )
        channel = ChannelSpec("opencode", "general/solver")
        self.spec = BenchSpec(
            name="general",
            runs_root=actual_runs_root,
            install_mode="workspace_config",
            prompts=BenchPrompts(work=self.goal),
            channels={"proposer": channel, "selector": channel},
        )
        self._ensure_base_harness()

    def _ensure_base_harness(self) -> Path:
        base_dir = self.harness_dir / "base"
        base_dir.mkdir(parents=True, exist_ok=True)

        surface = self.substrate.surface()
        base_components = {name: "" for name in surface.always_on}

        # Inject long-term project memory from SQLite into component c3
        base_components["LongTermMEMORY.md"] = self.db.render_longterm_memory_markdown()
        base_components["systemprompt.md"] = f"Task Goal:\n{self.goal}\n"

        tree.write_tree(surface, base_components, base_dir, stubs=False)
        return base_dir

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
        pass

    def execute(
        self,
        task_id: str,
        harness_dir: Optional[Path],
        run_id: str,
        *,
        timeout: int | None = None,
    ) -> Path:
        """Execute one probe rollout in an isolated worktree."""
        label = run_id.replace("/", "__")
        wt_path = self.wt_mgr.create(self.session_id, label)

        # Stage harness components into the worktree
        active_harness = harness_dir or self.base_harness()
        staged_dir = wt_path / ".harness_staged"
        if active_harness.is_dir():
            shutil.copytree(active_harness, staged_dir, dirs_exist_ok=True)

        # Run verification check if specified
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
                    timeout=timeout or 60,
                )
                test_passed = proc.returncode == 0
                test_output = (proc.stdout + "\n" + proc.stderr).strip()
            except Exception as e:
                test_passed = False
                test_output = f"Execution error: {e}"

        git_diff = self.wt_mgr.get_diff(self.session_id, label)

        # Record rollout in SQLite
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

        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        return (run_dir / "diff.patch").is_file()

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        diff = (run_dir / "diff.patch").read_text(encoding="utf-8", errors="replace")
        test_out = (run_dir / "test_output.txt").read_text(encoding="utf-8", errors="replace")
        return f"[ACTION] Candidate Rollout Execution\n[DIFF]\n{diff}\n[VERIFICATION]\n{test_out}"

    def deliverable_text(self, run_dir: Path) -> str:
        return (run_dir / "diff.patch").read_text(encoding="utf-8")

    def deliverable_files(self, run_dir: Path) -> Dict[str, str]:
        return {
            "diff.patch": self.deliverable_text(run_dir),
            "test_output.txt": (run_dir / "test_output.txt").read_text(encoding="utf-8"),
        }
