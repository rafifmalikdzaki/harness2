"""The grader-verdict tripwire, and what it does when it fires.

On a benchmark the wire aborts the run: a leak there invalidates the
measurement. On a general task the evidence IS the user's own repository, so
the wire quarantines the file and the run continues -- because a run that
aborted on an ordinary JSON key is a run that produced nothing.

Both real runs of the general path died on this wire before it was scoped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness2.core import context
from harness2.core.spec import BenchPrompts, BenchSpec


def _spec(**kwargs) -> BenchSpec:
    defaults = dict(
        name="t",
        runs_root=Path("/tmp"),
        install_mode="workspace_agents_md",
        prompts=BenchPrompts(work="do the work"),
        channels={
            "proposer": __import__(
                "harness2.core.spec", fromlist=["ChannelSpec"]
            ).ChannelSpec("opencode", "p/m"),
            "selector": __import__(
                "harness2.core.spec", fromlist=["ChannelSpec"]
            ).ChannelSpec("opencode", "p/m"),
        },
    )
    defaults.update(kwargs)
    return BenchSpec(**defaults)


def test_the_wire_finds_a_marker_in_a_file(tmp_path):
    (tmp_path / "a.md").write_text('the report has "passed_count": 3\n', encoding="utf-8")
    hits = context.scan_for_grader_verdicts(tmp_path, context.GRADER_VERDICT_MARKERS)
    assert hits
    rel, marker, _where = hits[0]
    assert rel == "a.md"
    assert marker == '"passed_count"'


def test_the_wire_finds_a_marker_in_a_name(tmp_path):
    (tmp_path / "judge_scope_and_deterministic_status.md").write_text("x", encoding="utf-8")
    hits = context.scan_for_grader_verdicts(tmp_path, context.GRADER_VERDICT_MARKERS)
    assert hits


def test_the_wire_aborts_when_asked_to(tmp_path):
    (tmp_path / "a.md").write_text('"passed_count"', encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        context.assert_no_grader_verdicts(tmp_path, context.GRADER_VERDICT_MARKERS)
    assert "grader verdict" in str(exc.value)


def test_quarantine_moves_the_file_and_says_so(tmp_path):
    root = tmp_path / "ctx"
    root.mkdir()
    (root / "a.md").write_text('the report has "passed_count": 3\n', encoding="utf-8")
    (root / "b.md").write_text("nothing to see\n", encoding="utf-8")

    moved = context.quarantine_grader_verdicts(root, context.GRADER_VERDICT_MARKERS)
    assert moved == ["a.md"]
    assert not (root / "a.md").exists()
    assert (root / "b.md").exists(), "an innocent file was quarantined too"
    warnings = (root / "WARNINGS.md").read_text(encoding="utf-8")
    assert "a.md" in warnings
    assert (tmp_path / "quarantine" / "a.md").is_file()


def test_quarantine_is_a_no_op_on_a_clean_tree(tmp_path):
    root = tmp_path / "ctx"
    root.mkdir()
    (root / "b.md").write_text("nothing to see\n", encoding="utf-8")
    assert context.quarantine_grader_verdicts(root, context.GRADER_VERDICT_MARKERS) == []
    assert not (root / "WARNINGS.md").exists()


def test_no_markers_means_no_scanning(tmp_path):
    """The general path declares no markers, so ordinary code is never scanned."""
    (tmp_path / "a.py").write_text('x = {"passed_count": 3}\n', encoding="utf-8")
    assert context.scan_for_grader_verdicts(tmp_path, ()) == []
    assert context.quarantine_grader_verdicts(tmp_path, ()) == []


def test_the_spec_carries_the_policy():
    assert _spec().verdict_policy == "raise"
    assert _spec(verdict_policy="quarantine").verdict_policy == "quarantine"
    assert _spec(grader_verdict_markers=()).grader_verdict_markers == ()
    with pytest.raises(ValueError):
        _spec(verdict_policy="shrug")
