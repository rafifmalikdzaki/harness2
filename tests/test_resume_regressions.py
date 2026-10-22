"""A finished run must stay finished, and a repeated run must not pay twice.

These are the bugs where the harness reported one thing and did another: a
pruned selection that made the accept command apply an arbitrary rollout, a
late exception that threw away a mode's whole record, one failing stage that
discarded its siblings, and a session id that was minted afresh every time
the harness was invoked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness2.core import schedule
from harness2.core.retention import RetentionPolicy, prune_runs


def _task_dir(runs: Path, ns: str, task: str) -> Path:
    d = runs / ns / task
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def env(tmp_path):
    """The offline demo adapter, its scripted proposer, and the temp root."""
    from harness2.demo import DemoAdapter, DemoProposer

    adapter = DemoAdapter(tmp_path)
    proposer = DemoProposer(adapter.spec.channels["proposer"])
    return adapter, proposer, tmp_path


# ------------------------------------------------------------- the retention


def test_the_selection_record_survives_the_policy(tmp_path):
    """The selection record names the recommended candidate.

    Deleting it made `harness2-finish` fall back to an arbitrary rollout --
    the base execution, in practice.
    """
    runs = tmp_path / "runs"
    for i in range(6):
        d = _task_dir(runs, f"ns{i}", "task")
        (d / "summary.json").write_text("{}", encoding="utf-8")
        sel = d / "selection" / "agg1"
        sel.mkdir(parents=True)
        (sel / "selection.json").write_text('{"chosen_label": "inv1"}', encoding="utf-8")
        (sel / "candidates").mkdir()
        (sel / "candidates" / "A.md").write_text("evidence", encoding="utf-8")

    prune_runs(runs, RetentionPolicy(keep_evidence_runs=1, keep_transcript_runs=1))

    for i in range(6):
        record = runs / f"ns{i}" / "task" / "selection" / "agg1" / "selection.json"
        assert record.is_file(), f"the selection record for ns{i} was deleted"


def test_the_policy_still_removes_the_evidence(tmp_path):
    runs = tmp_path / "runs"
    for i in range(6):
        d = _task_dir(runs, f"ns{i}", "task")
        (d / "summary.json").write_text("{}", encoding="utf-8")
        (d / "context").mkdir()
        (d / "context" / "x.md").write_text("evidence", encoding="utf-8")

    prune_runs(runs, RetentionPolicy(keep_evidence_runs=1, keep_transcript_runs=1))
    assert not (runs / "ns0" / "task" / "context").exists()
    assert (runs / "ns5" / "task" / "context").exists()


def test_the_policy_does_not_crash_when_the_run_is_gone(tmp_path):
    """Two processes pruning at once used to raise FileNotFoundError."""
    runs = tmp_path / "runs"
    d = _task_dir(runs, "ns", "task")
    (d / "summary.json").write_text("{}", encoding="utf-8")
    counts = prune_runs(runs)
    assert "runs" in counts


def test_the_sequential_namespace_is_pruned(tmp_path):
    """The summary for every mode lives in the parallel namespace.

    The sequential namespace's task directory has no summary.json, so it was
    invisible to the policy and its evidence was never pruned.
    """
    runs = tmp_path / "runs"
    # More runs than the recent window keeps, so the older ones fall out of it.
    for i in range(5):
        seq = _task_dir(runs, "ns-seq1", f"task{i}")
        (seq / "harnesses" / "inv1").mkdir(parents=True)
        (seq / "context").mkdir()
        (seq / "context" / "x.md").write_text("evidence", encoding="utf-8")

    prune_runs(runs)
    assert not (runs / "ns-seq1" / "task0" / "context").exists(), (
        "the sequential mode's evidence was never pruned"
    )
    # And the most recent ones are kept.
    assert (runs / "ns-seq1" / "task4" / "context").exists()


# --------------------------------------------------------------- the record


def test_a_late_failure_does_not_destroy_a_modes_record(env):
    """The record holds the stages, the rollouts and the ledger of work done.

    Replacing it with a failure note made the mode unresumable, so every
    later sweep re-paid the whole task.
    """

    adapter, proposer, tmp_path = env

    class BadLibrary:
        def add(self, *a, **kw):
            raise ValueError("the library write failed")

        def context_dir(self):
            return None

        def has_experience(self, *a, **kw):
            return True

    class GoodProposer:
        def __init__(self, inner):
            self.inner = inner
            self.spec = inner.spec
            self.audit_dir = None

        def call(self, *a, **kw):
            return self.inner.call(*a, **kw)

    result = schedule.run_task(
        "ledger-1",
        1,
        adapter=adapter,
        prop=GoodProposer(proposer),
        runs_root=adapter.spec.runs_root,
        modes=("agg",),
        library=BadLibrary(),
    )
    # The mode record survived and the failure is a warning, not an error.
    assert result.modes["agg"].get("ok"), result.modes
    assert any("library" in w for w in result.warnings), result.warnings


def test_a_half_finished_task_does_not_publish_ok(env):
    """The summary is written after EVERY mode.

    Without the expected mode set, a task that had finished its first mode
    published "ok" -- and a judge reads the selections without looking at the
    status.
    """
    adapter, proposer, tmp_path = env
    result = schedule.run_task(
        "ledger-1",
        1,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        modes=("agg",),
    )
    assert result.status == "ok", result.status

    # A task that was asked for two modes and only finished one.
    from harness2.core.schedule import _merged_status

    merged = {
        "modes": {"agg": {"ok": True}},
        "selections": {"agg": {"ok": True, "chosen_label": "agg1"}},
        "errors": [],
    }
    assert _merged_status(merged, ("agg", "seq")) == "partial"
    assert _merged_status(merged, ("agg",)) == "ok"


# ------------------------------------------------------------ the siblings


def test_one_failing_stage_keeps_its_siblings(env):
    """`pool_x.map` re-raises the first exception, discarding every sibling.

    The siblings ran, and were paid for.
    """

    adapter, proposer, tmp_path = env

    class OneBadStage:
        def __init__(self, inner):
            self.inner = inner
            self.spec = inner.spec
            self.audit_dir = None

        def call(self, mission, context_dir, label, **kwargs):
            if label.startswith("dom"):
                raise OSError("the disk is full")
            return self.inner.call(mission, context_dir, label, **kwargs)

    result = schedule.run_task(
        "ledger-1",
        1,
        adapter=adapter,
        prop=OneBadStage(proposer),
        runs_root=adapter.spec.runs_root,
        modes=("agg",),
    )
    mode = result.modes["agg"]
    labels = [s["label"] for s in mode.get("stages", [])]
    assert len(labels) > 1, mode
    assert any(s.get("fell_back") for s in mode["stages"]), mode


# ------------------------------------------------------------- the session


def test_a_restarted_run_resumes_the_open_session(tmp_path):
    """A new session id on every invocation made the general path unresumable."""
    from harness2.adapters.general import GeneralTaskAdapter

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    first = GeneralTaskAdapter(repo_root=repo, goal="do a thing", domain="general")
    second = GeneralTaskAdapter(repo_root=repo, goal="do a thing", domain="general")
    assert first.session_id == second.session_id, "the restart did not resume the session"

    # A different goal is a different session.
    other = GeneralTaskAdapter(repo_root=repo, goal="do another thing", domain="general")
    assert other.session_id != first.session_id

    # And --new-session asks for a fresh one.
    fresh = GeneralTaskAdapter(
        repo_root=repo, goal="do a thing", domain="general", new_session=True
    )
    assert fresh.session_id != first.session_id


# ---------------------------------------------------------------- the record


def test_the_rollout_row_is_written_after_the_artifacts(tmp_path):
    """A row that points at a diff.patch that does not exist is a candidate
    the finish command would pick and then fail to apply."""
    from harness2.adapters.general import GeneralTaskAdapter

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    adapter = GeneralTaskAdapter(repo_root=repo, goal="x")
    run_dir = adapter.execute("t", None, "cand1")
    session = adapter.db.get_session(adapter.session_id)
    row = session["rollouts"][0]
    recorded = Path(row["worktree_path"])
    assert recorded.is_dir()
    # And the artifacts the row refers to exist.
    assert (run_dir / "diff.patch").is_file()
    assert (run_dir / "candidate.json").is_file()
