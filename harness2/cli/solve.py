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

"""CLI entrypoint to run recursive task solving on arbitrary repositories."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..adapters.general import GeneralTaskAdapter


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness2-solve",
        description=(
            "Run ONE candidate rollout on a repository, in an isolated git worktree, "
            "and print the diff. This is the single-shot path: it makes no harness "
            "proposal and composes nothing. For the full recursive loop use "
            "`harness2-general`."
        ),
    )
    parser.add_argument("goal", help="The engineering task or bugfix to solve")
    parser.add_argument("--workspace", "-w", default=".", help="Target repository root")
    parser.add_argument("--verify", "-v", default=None, help="Optional test command to run (e.g. 'pytest')")
    parser.add_argument("--domain", "-d", default="general", help="Domain category for experience reflection")
    args = parser.parse_args(argv)

    ws = Path(args.workspace).expanduser().resolve()
    if not (ws / ".git").exists():
        parser.error(f"--workspace {ws} is not a git repository")
    print(f"Initializing Harness² for: {args.goal}")
    print(f"Workspace: {ws}")

    adapter = GeneralTaskAdapter(
        repo_root=ws,
        goal=args.goal,
        domain=args.domain,
        verify_cmd=args.verify,
    )
    print(f"Created session: {adapter.session_id}")
    print("Executing probe rollout in isolated worktree...")
    run_dir = adapter.execute(task_id=adapter.session_id, harness_dir=None, run_id="probe_1")

    wt_path = (run_dir / "worktree_path.txt").read_text(encoding="utf-8")
    diff = adapter.deliverable_text(run_dir)
    print(f"\n[+] Rollout complete! Worktree active at:\n    {wt_path}")
    print(f"\n[+] Git Diff Summary:\n{diff if diff.strip() else 'No modified files recorded.'}")
    print(f"\nTo finish and delete worktrees:\n    python -m harness2.cli.finish {adapter.session_id} --workspace {ws} --action accept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
