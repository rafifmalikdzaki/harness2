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

"""CLI entrypoint to finalize a session, apply/discard changes, and delete worktrees."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..adapters.general import recommended_label, recommended_run_label
from ..adapters.worktree import WorktreeError, WorktreeManager
from ..core.memory_db import MemoryDB


def _recommended_label(ws: Path, session_id: str, rollouts: list) -> str | None:
    """The rollout the run recommended, or the first one that passed.

    The LAST rollout is not the recommendation: it is whatever happened to be
    recorded last, which is how this command used to pick what to apply to
    somebody's repository.
    """
    label = recommended_label(ws, session_id)
    if label:
        return label
    passed = [r for r in rollouts if r.get("test_passed") == 1]
    if passed:
        return str(passed[0].get("label"))
    if rollouts:
        return str(rollouts[0].get("label"))
    return None


def _worktree_path(ws: Path, session_id: str, label: str) -> str | None:
    """The recommended candidate's worktree, from the selection's run id.

    A candidate's worktree is named after its run id, not after the stage
    label the selection records, so the stage label alone finds nothing.
    """
    run_label = recommended_run_label(ws, session_id)
    if not run_label:
        return None
    root = ws / ".harness2" / "worktrees"
    clean = session_id.replace("sess_", "")
    candidate = root / f"{clean}_{run_label}"
    return str(candidate) if candidate.is_dir() else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness2-finish",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("session_id", help="Session ID to finalize")
    parser.add_argument("--workspace", "-w", default=".", help="Target repository root")
    parser.add_argument(
        "--action", "-a", choices=("accept", "discard"), default="accept", help="Action to perform"
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Rollout to apply (default: the one the run recommended)",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Apply a candidate whose diff deletes files (refused by default)",
    )
    args = parser.parse_args(argv)

    ws = Path(args.workspace).expanduser().resolve()
    if not (ws / ".git").exists():
        print(f"Error: {ws} is not a git repository.", file=sys.stderr)
        return 1
    db = MemoryDB(ws / ".harness2" / "memory.db")
    # The worktrees live next to the session's state, not in a global temp dir.
    # Looking in the wrong place is how this command reported "deleted 0
    # worktrees" while leaving every one of them behind.
    wt_root = ws / ".harness2" / "worktrees"
    wt_mgr = WorktreeManager(ws, base_temp_dir=wt_root if wt_root.is_dir() else None)

    session = db.get_session(args.session_id)
    if not session:
        print(
            f"Error: Session {args.session_id} not found in {ws}/.harness2/memory.db",
            file=sys.stderr,
        )
        return 1

    if args.action == "accept":
        rollouts = session.get("rollouts", [])
        if not rollouts:
            print(f"Error: Session {args.session_id} has no rollouts to apply.", file=sys.stderr)
            return 1
        best_label = args.label or _recommended_label(ws, args.session_id, rollouts)
        if best_label is None:
            print("Error: no rollout was selected to apply.", file=sys.stderr)
            return 1
        # The worktree is named after the candidate's RUN label, so the
        # recorded path is the only reliable way to find it.
        worktree_path = next(
            (
                r.get("worktree_path")
                for r in rollouts
                if r.get("label") == best_label or str(r.get("label", "")).endswith(best_label)
            ),
            None,
        )
        if worktree_path is None:
            worktree_path = _worktree_path(ws, args.session_id, best_label)
        try:
            result = wt_mgr.apply_to_repo(
                args.session_id,
                best_label,
                allow_destructive=args.allow_destructive,
                worktree_path=worktree_path,
            )
        except WorktreeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not result.get("applied"):
            print(f"[!] Nothing applied: {result.get('reason', 'unknown reason')}", file=sys.stderr)
            print(
                "    The session and its worktrees were left in place so you can look.",
                file=sys.stderr,
            )
            return 1
        stats = result.get("stats") or {}
        print(f"[+] Applied {best_label} to the repository.")
        print(
            f"    {stats.get('files_changed', 0)} file(s) changed: "
            f"+{stats.get('bytes_added', 0):,} / -{stats.get('bytes_removed', 0):,} bytes"
        )
        print("    The change is in the working tree, unstaged. Review it with `git diff`.")
        # Self-evolution: the lessons and the procedural skills are what make
        # the NEXT run better than this one. The README describes this path as
        # producing them, and it did not.
        try:
            from ..core.evolution import SelfEvolutionEngine

            evolution = SelfEvolutionEngine(ws, db=db).analyze_session(args.session_id)
            skills = evolution.get("synthesized_skills") or []
            print(
                f"    self-evolution: {evolution.get('status', 'skipped')}"
                + (f", {len(skills)} skill(s) synthesized" if skills else "")
            )
        except Exception as exc:
            print(f"    self-evolution: skipped ({exc})", file=sys.stderr)
        db.close_session(args.session_id, status="accepted")
    else:
        db.close_session(args.session_id, status="discarded")
        print("[-] Changes discarded.")

    cleaned = wt_mgr.cleanup_session(args.session_id)
    print(f"[+] Cleaned and deleted {len(cleaned)} worktrees:")
    for path in cleaned:
        print(f"    - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
