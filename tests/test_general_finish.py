"""Accepting a candidate: which one, and what it would do to the repository.

The bug this file is here for: a candidate's worktree is named after its RUN
label (`general-oc-agg1/sess_x/base1`), while the selection records the STAGE
label (`base1`). Applying by the stage label found no worktree, so a run could
produce candidates and still not be acceptable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness2.adapters.general import (
    recommended_label,
    recommended_run_label,
    selection_file,
)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _write_selection(repo: Path, session_id: str, layout: str) -> None:
    runs = repo / ".harness2" / "runs"
    if layout == "solve":
        target = runs / session_id / "selection.json"
    else:
        target = runs / "general-oc-agg1" / session_id / "selection" / "agg1" / "selection.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "chosen_label": "base1",
                "chosen_run_id": f"general-oc-agg1/{session_id}/base1",
                "ok": True,
            }
        ),
        encoding="utf-8",
    )


def test_the_selection_is_found_in_both_layouts(tmp_path):
    repo = make_repo(tmp_path)
    session = "sess_abc123"

    _write_selection(repo, session, "solve")
    assert selection_file(repo, session) is not None
    assert recommended_label(repo, session) == "base1"

    repo2 = make_repo(tmp_path / "other")
    _write_selection(repo2, session, "recursion")
    assert selection_file(repo2, session) is not None
    assert recommended_label(repo2, session) == "base1"


def test_the_run_label_is_what_the_worktree_is_named_after(tmp_path):
    repo = make_repo(tmp_path)
    session = "sess_abc123"
    _write_selection(repo, session, "recursion")

    # The stage label is "base1"; the worktree is named after the run id.
    assert recommended_label(repo, session) == "base1"
    assert recommended_run_label(repo, session) == "general-oc-agg1__sess_abc123__base1"


def test_no_selection_means_no_recommendation(tmp_path):
    repo = make_repo(tmp_path)
    assert selection_file(repo, "sess_nothing") is None
    assert recommended_label(repo, "sess_nothing") is None
    assert recommended_run_label(repo, "sess_nothing") is None


def test_finish_applies_the_recommended_candidate(tmp_path):
    """The whole accept path, on a real repository."""
    from harness2.adapters.worktree import WorktreeManager
    from harness2.cli.finish import main
    from harness2.core.memory_db import MemoryDB

    repo = make_repo(tmp_path)
    db = MemoryDB(repo / ".harness2" / "memory.db")
    session = db.start_session("x", "general")

    mgr = WorktreeManager(repo, base_temp_dir=repo / ".harness2" / "worktrees")
    run_label = f"general-oc-agg1__{session}__base1"
    wt = mgr.create(session, run_label)
    (wt / "app.py").write_text("print('v1')\nprint('v2')\n", encoding="utf-8")

    db.record_rollout(session, run_label, worktree_path=str(wt))
    _write_selection(repo, session, "recursion")

    code = main([session, "--workspace", str(repo)])
    assert code == 0
    assert "v2" in (repo / "app.py").read_text(encoding="utf-8")


def test_finish_refuses_a_deletion_and_keeps_the_worktree(tmp_path):
    from harness2.adapters.worktree import WorktreeManager
    from harness2.cli.finish import main
    from harness2.core.memory_db import MemoryDB

    repo = make_repo(tmp_path)
    db = MemoryDB(repo / ".harness2" / "memory.db")
    session = db.start_session("x", "general")

    mgr = WorktreeManager(repo, base_temp_dir=repo / ".harness2" / "worktrees")
    run_label = f"general-oc-agg1__{session}__base1"
    wt = mgr.create(session, run_label)
    (wt / "app.py").unlink()

    db.record_rollout(session, run_label, worktree_path=str(wt))
    _write_selection(repo, session, "recursion")

    code = main([session, "--workspace", str(repo)])
    assert code == 1
    assert (repo / "app.py").is_file(), "the file was deleted anyway"
    assert wt.is_dir(), "the worktree was cleaned up despite the refusal"
