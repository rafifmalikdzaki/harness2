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
from pathlib import Path

from ..adapters.worktree import WorktreeManager
from ..core.memory_db import MemoryDB


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", help="Session ID to finalize")
    parser.add_argument("--workspace", "-w", default=".", help="Target repository root")
    parser.add_argument("--action", "-a", choices=("accept", "discard"), default="accept", help="Action to perform")
    args = parser.parse_args(argv)

    ws = Path(args.workspace).expanduser().resolve()
    db = MemoryDB(ws / ".harness2" / "memory.db")
    wt_mgr = WorktreeManager(ws)

    session = db.get_session(args.session_id)
    if not session:
        print(f"Error: Session {args.session_id} not found in {ws}/.harness2/memory.db")
        return 1

    if args.action == "accept":
        rollouts = session.get("rollouts", [])
        if rollouts:
            best_label = rollouts[-1]["label"]
            wt_mgr.apply_to_repo(args.session_id, best_label)
            print("[+] Successfully applied changes to repository.")
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
