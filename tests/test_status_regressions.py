"""Regressions for the status, resume and bookkeeping bugs.

Every test here is about a case where the harness reported one thing and did
another: a run that could not be resumed, a task that was re-paid forever, a
mode that was "ok" although nothing had been learned, a selection error
recorded twice, a stage's work thrown away by a later failure.

They are written against the demo adapter, which is deterministic and offline.
"""

from __future__ import annotations

import json

import pytest

from harness2.core import context, progress, runmark, schedule
from harness2.core.library import Library
from harness2.core.proposer import ProposerError
from harness2.demo import DemoAdapter, DemoProposer


@pytest.fixture
def env(tmp_path):
    adapter = DemoAdapter(tmp_path)
    proposer = DemoProposer(adapter.spec.channels["proposer"])
    return adapter, proposer, tmp_path


def _run(adapter, proposer, tmp_path, k=1, modes=("agg",), pool_policy="finals"):
    return schedule.run_domain(
        "accounting",
        adapter.tasks(),
        k,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=tmp_path / "library" / str(k),
        modes=modes,
        pool_policy=pool_policy,
    )


def _summary(adapter, tmp_path, k=1, mode="agg", task_id="ledger-1"):
    return json.loads(
        (
            adapter.spec.runs_root / f"demo-oc-{mode}{k}" / task_id / "summary.json"
        ).read_text()
    )


# --------------------------------------------------------------------- S1


def test_a_selection_that_awards_the_work_to_the_base_is_resumable(env):
    """The base rollouts share ONE harness tree.

    A selection that awards the work to a base has no tree of its own, so
    looking for `harnesses/base1` found nothing -- and the task could never be
    resumed, so every later sweep re-paid it in full.
    """
    from harness2.core import tree as harness_tree

    adapter, proposer, tmp_path = env
    _run(adapter, proposer, tmp_path, k=1, pool_policy="finals+base")

    # Point the recorded selection at the reference execution, the way a
    # selector that prefers the base would.
    summary_path = (
        adapter.spec.runs_root / "demo-oc-agg1" / "ledger-1" / "summary.json"
    )
    summary = json.loads(summary_path.read_text())
    base_run_dir = next(
        p for p in (tmp_path / "executions").rglob("base1") if p.is_dir()
    )
    summary["selections"]["agg"]["chosen_label"] = "base1"
    summary["selections"]["agg"]["chosen_run_id"] = "demo-oc-agg1/ledger-1/base1"
    summary["selections"]["agg"]["chosen_run_dir"] = str(base_run_dir)
    # The base's harness tree is the base tree, not a tree of its own.
    base_dir = harness_tree.run_harness_dir(
        adapter.spec.runs_root / "demo-oc-agg1" / "ledger-1", "base1"
    )
    assert base_dir.name == "base", base_dir
    assert base_dir.is_dir(), base_dir
    summary["selections"]["agg"]["harness_fingerprint"] = runmark.fingerprint(base_dir)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # And the task resumes rather than being re-paid.
    before = adapter.execution_count
    _run(adapter, proposer, tmp_path, k=1, pool_policy="finals+base")
    assert adapter.execution_count == before, "the task was re-paid instead of resumed"


# --------------------------------------------------------------------- S2


def test_a_failed_reflection_does_not_make_the_task_partial(env):
    """A lesson is a bonus on top of a selection that already happened.

    Recording the failure as an error made the task 'partial', which made it
    unresumable, which re-paid the whole task on every later sweep.
    """
    adapter, proposer, tmp_path = env

    class BadReflection(DemoProposer):
        def call(self, mission, context_dir, label, **kwargs):
            if label.startswith("experience."):
                raise ProposerError("the lesson session failed", attempts=1)
            return super().call(mission, context_dir, label, **kwargs)

    _run(adapter, BadReflection(adapter.spec.channels["proposer"]), tmp_path, k=1)
    summary = _summary(adapter, tmp_path)
    assert summary["status"] == "ok", summary
    assert summary["selections"]["agg"]["ok"], summary
    assert any("reflection failed" in w for w in summary.get("warnings", [])), summary


# --------------------------------------------------------------------- S3


def test_a_mode_that_learned_nothing_is_not_ok(env):
    """Every stage falling back means the improver produced nothing usable.

    One stage falling back is normal -- that is what the retry is for.
    """
    adapter, proposer, tmp_path = env

    class DeadImprover(DemoProposer):
        def call(self, mission, context_dir, label, **kwargs):
            if label.startswith("select.") or label.startswith("experience."):
                return super().call(mission, context_dir, label, **kwargs)
            raise ProposerError("the improver is down", attempts=1)

    _run(adapter, DeadImprover(adapter.spec.channels["proposer"]), tmp_path, k=1)
    summary = _summary(adapter, tmp_path)
    assert summary["modes"]["agg"]["learned_nothing"] is True, summary
    assert summary["status"] != "ok", summary
    assert any("fell back" in e for e in summary["errors"]), summary


# --------------------------------------------------------------------- S4


def test_a_selection_failure_is_recorded_once(env):
    adapter, proposer, tmp_path = env

    class DeadSelector(DemoProposer):
        def call(self, mission, context_dir, label, **kwargs):
            if label.startswith("select."):
                raise ProposerError("the selector is down", attempts=1)
            return super().call(mission, context_dir, label, **kwargs)

    _run(adapter, DeadSelector(adapter.spec.channels["proposer"]), tmp_path, k=1)
    summary = _summary(adapter, tmp_path)
    selection_errors = [e for e in summary["errors"] if "selection failed" in e]
    assert len(selection_errors) == len(set(selection_errors)), summary["errors"]


# --------------------------------------------------------------------- S5


def test_the_reported_status_is_the_status_on_disk(env):
    adapter, proposer, tmp_path = env
    result = _run(adapter, proposer, tmp_path, k=1)
    summary = _summary(adapter, tmp_path)
    assert result["tasks"][0]["status"] == summary["status"], (result, summary)


# --------------------------------------------------------------------- S6


def test_a_failure_in_the_compose_phase_keeps_the_earlier_record(env, monkeypatch):
    """The stages that already ran cost money. Their record must survive.

    The compose phase is the last thing to run, and it is the first thing
    that sees every candidate at once, so it is where a failure is most
    likely -- and where losing the earlier record hurts most.
    """
    from harness2.core import prompts

    adapter, proposer, tmp_path = env

    def boom(*_a, **_kw):
        raise RuntimeError("the compose prompt could not be built")

    monkeypatch.setattr(prompts, "aggregate", boom)

    _run(adapter, proposer, tmp_path, k=1)
    summary = _summary(adapter, tmp_path)
    mode = summary["modes"]["agg"]
    # The INV/DOM stages and their rollouts are still on the record.
    labels = [s["label"] for s in mode.get("stages", [])]
    assert labels, mode
    assert any("composition failed" in e for e in mode.get("errors", [])), mode
    assert mode["ok"] is False, mode


# --------------------------------------------------------------------- S8


def test_the_progress_counters_reset_between_tasks():
    reporter = progress.Progress(fmt="none")
    reporter.begin_task(total_stages=3, total_rollouts=4)
    reporter.event("stage", label="inv1")
    reporter.event("rollout", label="base1")
    reporter.begin_task(total_stages=3, total_rollouts=4)
    reporter.event("stage", label="inv1")
    stages = [e for e in reporter.events if e["kind"] == "stage"]
    assert stages[-1]["index"] == 1, stages
    assert stages[-1]["total"] == 3, stages


# --------------------------------------------------------------------- S9


def test_compacting_the_library_keeps_the_entries_with_no_cue(tmp_path):
    """An entry with no cue supersedes nothing, so nothing replaces it."""
    surface = DemoAdapter(tmp_path).substrate.surface()
    lib = Library(root=tmp_path / "lib", domain="accounting", surface=surface)
    exp = {"cue": "when adding a line", "lesson": "run the check", "evidence": "the diff"}
    lib.add("t1", {"systemprompt.md": "a"}, why="w", label="l", run_id="r", experience=exp)
    lib.add("t2", {"systemprompt.md": "b"}, why="w", label="l", run_id="r", experience=exp)
    lib.add("t3", {"systemprompt.md": "c"}, why="w", label="l", run_id="r", experience=exp)
    assert (lib.dir / "harnesses" / "t3" / "EXPERIENCE.md").is_file()

    # Strip the cue from one entry.
    exp_file = lib.dir / "harnesses" / "t3" / "EXPERIENCE.md"
    exp_file.write_text("**lesson**\n\nsomething\n", encoding="utf-8")

    lib.compact(keep=1)
    assert (lib.dir / "harnesses" / "t3").is_dir(), "a cue-less entry was deleted"


# --------------------------------------------------------------------- S10


def test_the_layout_describes_the_deliverable_that_was_written(env):
    """A single-file deliverable is not a directory, and the mission must say so."""
    adapter, proposer, tmp_path = env
    ctx = tmp_path / "ctx"
    rolls = []
    for i in range(2):
        rd = tmp_path / f"run{i}"
        rd.mkdir()
        (rd / "artifact.txt").write_text("x", encoding="utf-8")
        rolls.append(
            type(
                "R",
                (),
                {
                    "label": f"cand{i}",
                    "run_id": f"cand{i}",
                    "run_dir": rd,
                    "ok": True,
                    "delivered": True,
                },
            )()
        )

    # The demo adapter delivers one file per rollout.
    for i in range(2):
        (tmp_path / f"run{i}" / "answer.txt").write_text("x", encoding="utf-8")
        (tmp_path / f"run{i}" / "trajectory.md").write_text("[ACTION] x\n", encoding="utf-8")
    tree = context.build(ctx, adapter, "ledger-1", rolls)
    for label in tree.deliverable_dirs:
        assert (ctx / "deliverables" / label).is_dir(), label
    for label in tree.deliverable_files:
        assert (ctx / "deliverables" / f"{label}.md").is_file(), label
    # The layout points at what exists.
    assert "deliverables" in tree.layout


# --------------------------------------------------------------------- S12


def test_a_general_rollout_is_idempotent_by_run_id(tmp_path):
    """A rollout that finished is not paid for twice."""
    import subprocess

    from harness2.adapters.general import GeneralTaskAdapter

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", verify_cmd=None)
    # No provider is configured in a test, so the rollout only verifies.
    run_dir = adapter.execute("t", None, "cand1")
    assert (run_dir / "candidate.json").is_file()
    assert (run_dir / ".adapt_complete").is_file(), "the rollout was not marked complete"

    # The marker records the run it belongs to.
    marker = json.loads((run_dir / ".adapt_complete").read_text())
    assert marker["run_id"] == "cand1"

    # A second call returns the same directory without re-running.
    again = adapter.execute("t", None, "cand1")
    assert again == run_dir
