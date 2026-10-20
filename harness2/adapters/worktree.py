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

"""Git worktree lifecycle management for isolated candidate harness execution.

Ensures candidate rollouts execute in safe, isolated branches without touching
the user's active branch. Worktrees stay alive for inspection and are cleaned up
only when the user or agent explicitly accepts or discards them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


class WorktreeError(RuntimeError):
    """Raised when git worktree operations fail."""


def _run_git(args: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise WorktreeError(f"Git command failed: {' '.join(cmd)}\nError: {res.stderr.strip()}")
    return res


class WorktreeManager:
    """Manages the creation, inspection, and deletion of git worktrees."""

    def __init__(self, repo_root: Path | str, base_temp_dir: Optional[Path | str] = None):
        self.repo_root = Path(repo_root).expanduser().resolve()
        if not (self.repo_root / ".git").exists():
            raise WorktreeError(f"{self.repo_root} is not a valid git repository.")
        self.base_temp_dir = (
            Path(base_temp_dir).expanduser().resolve()
            if base_temp_dir
            else Path("/tmp/harness2_worktrees")
        )
        self.base_temp_dir.mkdir(parents=True, exist_ok=True)

    def branch_name(self, session_id: str, label: str) -> str:
        clean_sess = session_id.replace("sess_", "")
        return f"harness2/{clean_sess}/{label}"

    def worktree_dir(self, session_id: str, label: str) -> Path:
        clean_sess = session_id.replace("sess_", "")
        return self.base_temp_dir / f"{clean_sess}_{label}"

    def create(self, session_id: str, label: str) -> Path:
        """Create a new worktree branched from current HEAD."""
        branch = self.branch_name(session_id, label)
        dest = self.worktree_dir(session_id, label)

        # Clean existing directory if stale
        if dest.exists():
            self.remove(session_id, label, force=True)

        # Create branch and worktree
        _run_git(["worktree", "add", "-b", branch, str(dest), "HEAD"], cwd=self.repo_root)
        return dest

    def get_diff(self, session_id: str, label: str) -> str:
        """Get the clean git diff of user/code changes, filtering out internal scaffolding."""
        dest = self.worktree_dir(session_id, label)
        if not dest.is_dir():
            return ""

        # Remove internal scaffolding before computing diff
        staged_internal = dest / ".harness_staged"
        if staged_internal.exists():
            shutil.rmtree(staged_internal, ignore_errors=True)

        diff_res = subprocess.run(
            ["git", "diff", "HEAD", "--", ":!.harness_staged", ":!.harness2"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        untracked_res = subprocess.run(
            ["git", "status", "--porcelain", "--", ":!.harness_staged", ":!.harness2"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        diff_text = diff_res.stdout
        clean_untracked = "\n".join(
            line
            for line in untracked_res.stdout.splitlines()
            if not line.endswith((".harness_staged/", ".harness2/"))
        ).strip()
        if clean_untracked:
            diff_text += f"\n# Untracked files:\n{clean_untracked}\n"
        return diff_text

    def remove(self, session_id: str, label: str, force: bool = True) -> None:
        """Remove one worktree and its branch."""
        dest = self.worktree_dir(session_id, label)
        branch = self.branch_name(session_id, label)

        if dest.exists():
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(dest))
            try:
                _run_git(args, cwd=self.repo_root)
            except WorktreeError:
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)

        subprocess.run(["git", "worktree", "prune"], cwd=self.repo_root, capture_output=True)
        subprocess.run(
            ["git", "branch", "-D", branch], cwd=self.repo_root, capture_output=True
        )

    def cleanup_session(self, session_id: str) -> List[str]:
        """Delete all active worktrees associated with a session."""
        cleaned: List[str] = []
        clean_sess = session_id.replace("sess_", "")
        prefix = f"{clean_sess}_"

        for p in self.base_temp_dir.iterdir():
            if p.is_dir() and p.name.startswith(prefix):
                label = p.name[len(prefix) :]
                self.remove(session_id, label, force=True)
                cleaned.append(str(p))

        subprocess.run(["git", "worktree", "prune"], cwd=self.repo_root, capture_output=True)
        return cleaned

    def apply_to_repo(self, session_id: str, label: str) -> None:
        """Apply genuine code changes from the worktree into the primary repo working directory."""
        dest = self.worktree_dir(session_id, label)
        if not dest.is_dir():
            raise WorktreeError(f"Worktree directory {dest} does not exist.")

        # Ensure internal scaffolding is completely removed from worktree before staging
        staged_internal = dest / ".harness_staged"
        if staged_internal.exists():
            shutil.rmtree(staged_internal, ignore_errors=True)

        # Stage untracked/modified files, explicitly ignoring internal directories
        subprocess.run(["git", "add", "-A", "--", ":!.harness_staged", ":!.harness2"], cwd=dest, capture_output=True)
        patch_res = subprocess.run(
            ["git", "diff", "--cached", "HEAD", "--", ":!.harness_staged", ":!.harness2"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        patch = patch_res.stdout
        if patch.strip():
            apply_res = subprocess.run(
                ["git", "apply", "--3way"],
                cwd=self.repo_root,
                input=patch,
                capture_output=True,
                text=True,
            )
            if apply_res.returncode != 0:
                raise WorktreeError(f"Failed to apply patch to repository:\n{apply_res.stderr}")
