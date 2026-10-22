"""Tests for the evidence a candidate carries into the selection.

A verification command is only useful if the selector can see what it
said. A candidate that passes and a candidate that fails look identical in
a diff, so the verdict has to travel with the candidate -- otherwise the
selection is choosing between diffs while ignoring the only objective
signal available.
"""

from pathlib import Path

from harness2.adapters.general import GeneralTaskAdapter


def make_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_the_verifier_output_is_offered_as_evidence(tmp_path):
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", verify_cmd="true")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "test_output.txt").write_text("166 passed, 1 skipped\n", encoding="utf-8")

    assets = adapter.deliverable_assets(run_dir)
    assert [p.name for p in assets] == ["test_output.txt"]
    assert assets[0].is_file()


def test_without_a_verifier_there_is_no_evidence(tmp_path):
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "test_output.txt").write_text("whatever\n", encoding="utf-8")
    assert adapter.deliverable_assets(run_dir) == []


def test_the_asset_note_says_it_is_a_verdict(tmp_path):
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", verify_cmd="true")
    note = adapter.deliverable_assets_note(["test_output.txt"])
    assert "test_output.txt" in note
    assert "verification" in note.lower()


def test_the_score_reads_the_recorded_exit_status(tmp_path):
    """A test runner's output format is its business; its exit status is not.

    The score used to look for the string ``(no failures)``, which no test
    runner prints -- so every rollout scored zero, whatever it did.
    """
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", verify_cmd="true")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "test_output.txt").write_text("166 passed, 1 skipped in 22.36s\n", encoding="utf-8")

    # No recorded status and no verdict header: the command never ran. That is
    # neither a pass nor a failure -- it is ungraded, and ungraded is not zero.
    assert adapter.score(run_dir, "t") is None

    adapter._verdicts["run"] = True
    graded = adapter.score(run_dir, "t")
    assert graded["n_passed"] == 1
    assert graded["n_criteria"] == 1

    adapter._verdicts["run"] = False
    assert adapter.score(run_dir, "t")["n_passed"] == 0


def test_the_score_reads_the_verdict_back_out_of_the_artifact(tmp_path):
    """A judge in another process sees the verdict the rollout recorded."""
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", verify_cmd="true")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "test_output.txt").write_text(
        "VERIFICATION: PASSED (exit status 0)\n\n17 passed in 2.26s\n", encoding="utf-8"
    )
    assert adapter.score(run_dir, "t")["n_passed"] == 1

    (run_dir / "test_output.txt").write_text(
        "VERIFICATION: FAILED (exit status 1)\n\n3 failed\n", encoding="utf-8"
    )
    assert adapter.score(run_dir, "t")["n_passed"] == 0


def test_no_verifier_means_no_grade(tmp_path):
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert adapter.score(run_dir, "t") is None
