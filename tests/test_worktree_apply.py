"""Applying a candidate's diff to somebody's repository.

This is the one irreversible thing the harness does. The tests cover the
three ways it can go wrong: silently doing nothing, silently doing something
destructive, and failing to say which happened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness2.adapters.diffguard import analyze_patch, describe_destructive
from harness2.adapters.worktree import WorktreeError, WorktreeManager


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _worktree(repo: Path, mgr: WorktreeManager, label: str = "cand") -> Path:
    wt = mgr.create("sess_test", label)
    (wt / "app.py").write_text("print('v1')\nprint('v2')\n", encoding="utf-8")
    return wt


# ---------------------------------------------------------------- the guard


def test_the_guard_reads_a_patch():
    patch = (
        "diff --git a/keep.py b/keep.py\n"
        "index 111..222 100644\n"
        "--- a/keep.py\n"
        "+++ b/keep.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "index 333..000\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x = 1\n"
        "-y = 2\n"
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "index 000..444\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+z = 3\n"
    )
    stats = analyze_patch(patch)
    assert stats.modified == ("keep.py",)
    assert stats.deleted == ("gone.py",)
    assert stats.added == ("new.py",)
    assert stats.destructive
    assert "gone.py" in describe_destructive(stats)


def test_an_empty_patch_is_not_destructive():
    stats = analyze_patch("")
    assert not stats.destructive
    assert stats.files_changed == 0


# ---------------------------------------------------------------- applying


def test_a_change_is_applied_and_reported(tmp_path):
    repo = make_repo(tmp_path)
    mgr = WorktreeManager(repo, base_temp_dir=tmp_path / "wt")
    _worktree(repo, mgr)

    result = mgr.apply_to_repo("sess_test", "cand")
    assert result["applied"], result
    assert result["stats"]["modified"] == ["app.py"]
    assert "v2" in (repo / "app.py").read_text(encoding="utf-8")


def test_a_candidate_with_no_changes_says_so(tmp_path):
    """An empty patch must not read as a successful apply.

    The changes were silently lost this way: `git add` failed, the patch came
    out empty, and the caller was told the changes had been applied.
    """
    repo = make_repo(tmp_path)
    mgr = WorktreeManager(repo, base_temp_dir=tmp_path / "wt")
    wt = mgr.create("sess_test", "cand")

    result = mgr.apply_to_repo("sess_test", "cand")
    assert not result["applied"]
    assert "no changes" in result["reason"]
    assert "v1" in (repo / "app.py").read_text(encoding="utf-8")
    assert wt.is_dir()


def test_a_deletion_is_refused_until_it_is_asked_for(tmp_path):
    """A candidate that deletes files must not delete them by accident.

    A candidate once passed its verifier after removing 151 of the project's
    files. A passing verifier does not make that safe.
    """
    repo = make_repo(tmp_path)
    mgr = WorktreeManager(repo, base_temp_dir=tmp_path / "wt")
    wt = mgr.create("sess_test", "cand")
    (wt / "app.py").unlink()

    result = mgr.apply_to_repo("sess_test", "cand")
    assert not result["applied"]
    assert "delet" in result["reason"].lower()
    assert result["stats"]["deleted"] == ["app.py"]
    assert (repo / "app.py").is_file(), "the repository's file was removed anyway"

    # With the operator's explicit consent, it goes through.
    result = mgr.apply_to_repo("sess_test", "cand", allow_destructive=True)
    assert result["applied"], result
    assert not (repo / "app.py").exists()


def test_a_broken_repository_raises_rather_than_reporting_success(tmp_path):
    repo = make_repo(tmp_path)
    mgr = WorktreeManager(repo, base_temp_dir=tmp_path / "wt")
    _worktree(repo, mgr)
    # Break git's view of the worktree: the diff cannot be produced.
    wt = mgr.worktree_dir("sess_test", "cand")
    subprocess.run(["git", "rm", "-r", "--cached", "."], cwd=wt, check=False, capture_output=True)
    (wt / ".git").write_text("not a gitfile\n", encoding="utf-8")

    with pytest.raises(WorktreeError):
        mgr.apply_to_repo("sess_test", "cand")
