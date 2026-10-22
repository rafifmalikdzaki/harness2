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
from typing import List, Optional, Sequence

from .diffguard import analyze_patch, describe_destructive


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

    def resolve_worktree(
        self, session_id: str, label: str, worktree_path: str | Path | None = None
    ) -> Path:
        """Where a candidate's worktree is.

        The recorded path wins when there is one. A candidate's worktree is
        named after its RUN label, which carries the namespace and the session
        id, while the selection records the stage label (`base1`, `inv2`) --
        so computing the path from the stage label finds nothing.
        """
        if worktree_path:
            p = Path(worktree_path)
            if p.is_dir():
                return p
        return self.worktree_dir(session_id, label)

    def create(self, session_id: str, label: str) -> Path:
        """Create a new worktree branched from current HEAD."""
        dest = self.worktree_dir(session_id, label)

        # Clean existing directory if stale
        if dest.exists():
            self.remove(session_id, label, force=True)

        # Create detached worktree directly from HEAD (avoids polluting git branch list)
        _run_git(["worktree", "add", "--detach", str(dest), "HEAD"], cwd=self.repo_root)
        return dest

    def get_diff(
        self, session_id: str, label: str, exclude: Optional[Sequence[str]] = None
    ) -> str:
        """Get the clean git diff of user/code changes, filtering out internal scaffolding.

        `exclude` names paths the harness itself wrote into the worktree (the
        rendered AGENTS.md, .opencode/, scripts/). Those are the harness the
        improver edited, not the agent's work, so they must not read as
        changes the solver made.
        """
        dest = self.worktree_dir(session_id, label)
        if not dest.is_dir():
            return ""

        # Remove internal scaffolding before computing diff
        staged_internal = dest / ".harness_staged"
        if staged_internal.exists():
            shutil.rmtree(staged_internal, ignore_errors=True)

        pathspec = [
            ":!.harness_staged",
            ":!.harness2",
            ":!.harness_authored",
            ":!.harness_traj",
        ]
        for rel in exclude or []:
            rel = rel.strip()
            if rel:
                pathspec.append(f":(exclude){rel}")

        diff_res = subprocess.run(
            ["git", "diff", "HEAD", "--", *pathspec],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        untracked_res = subprocess.run(
            ["git", "status", "--porcelain", "--", *pathspec],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        diff_text = diff_res.stdout
        excluded = {e.strip() for e in (exclude or []) if e.strip()}
        # Only genuinely untracked paths belong in this section; modified files
        # are already in the diff above.
        clean_untracked = "\n".join(
            line
            for line in untracked_res.stdout.splitlines()
            if line.startswith("??")
            and not line.endswith((".harness_staged/", ".harness2/"))
            and line[3:].strip() not in excluded
        ).strip()
        if clean_untracked:
            diff_text += f"\n# Untracked files:\n{clean_untracked}\n"
        return diff_text

    def remove(self, session_id: str, label: str, force: bool = True) -> None:
        """Remove one worktree.

        The worktree is detached, so there is no branch to delete: creating
        one per candidate is what left the repository's branch list littered
        with `harness2/<session>/<label>` entries.
        """
        dest = self.worktree_dir(session_id, label)

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

    def apply_to_repo(
        self,
        session_id: str,
        label: str,
        *,
        allow_destructive: bool = False,
        worktree_path: str | Path | None = None,
    ) -> dict:
        """Apply genuine code changes from the worktree into the primary repo working directory.

        Returns what happened: ``{"applied": bool, "reason": str, "stats": {...}}``.
        A refusal is a normal outcome, not an exception -- the operator decides
        whether a patch that deletes files is one they want.

        Every git step is checked. An unchecked `git add` makes an empty patch,
        and an empty patch used to be reported as a successful apply: the
        changes were silently lost and the caller was told they were applied.
        """
        dest = self.resolve_worktree(session_id, label, worktree_path)
        if not dest.is_dir():
            raise WorktreeError(f"Worktree directory {dest} does not exist.")

        # Ensure internal scaffolding is completely removed from worktree before staging
        staged_internal = dest / ".harness_staged"
        if staged_internal.exists():
            shutil.rmtree(staged_internal, ignore_errors=True)

        # Stage untracked/modified files, explicitly ignoring internal directories & caches
        exclude_args = [
            ":(exclude).harness_staged",
            ":(exclude).harness2",
            ":(exclude)__pycache__",
            ":(exclude)*.pyc",
            ":(exclude).pytest_cache",
        ]
        add_res = subprocess.run(
            ["git", "add", "-A", "--", *exclude_args],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        if add_res.returncode != 0:
            raise WorktreeError(
                "could not stage the candidate's changes:\n"
                f"{(add_res.stderr or add_res.stdout or '').strip()}"
            )
        patch_res = subprocess.run(
            ["git", "diff", "--cached", "HEAD", "--binary", "--", *exclude_args],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        if patch_res.returncode != 0:
            raise WorktreeError(
                "could not read the candidate's changes:\n"
                f"{(patch_res.stderr or patch_res.stdout or '').strip()}"
            )
        patch = patch_res.stdout
        stats = analyze_patch(patch)

        if not patch.strip():
            return {
                "applied": False,
                "reason": "the candidate produced no changes to apply",
                "stats": stats.to_dict(),
                "patch_bytes": 0,
            }

        if stats.destructive and not allow_destructive:
            return {
                "applied": False,
                "reason": (
                    f"refused: {describe_destructive(stats)}. Applying it would remove "
                    f"files from the repository. Re-run with allow_destructive to apply "
                    f"it anyway."
                ),
                "stats": stats.to_dict(),
                "patch_bytes": len(patch),
            }

        apply_res = subprocess.run(
            ["git", "apply", "--3way"],
            cwd=self.repo_root,
            input=patch,
            capture_output=True,
            text=True,
        )
        if apply_res.returncode != 0:
            raise WorktreeError(f"Failed to apply patch to repository:\n{apply_res.stderr}")
        return {
            "applied": True,
            "reason": "",
            "stats": stats.to_dict(),
            "patch_bytes": len(patch),
        }
