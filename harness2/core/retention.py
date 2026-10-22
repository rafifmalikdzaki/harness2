"""Keeping the harness's on-disk memory from growing without bound.

The run directory is the harness's largest memory: every rollout leaves a
transcript, a diff, a verifier log, and an evidence tree, and the evidence
trees are copied again by the selection and reflection stages. Over months
of runs that is unbounded, and it is bounded by nothing but the disk.

This module is the policy: **keep the recent runs in full, keep the
conclusions of the old ones, and drop the transcripts.**

Three things are pruned, in order of how much they cost:

1. the **evidence trees** of runs that are no longer recent -- these are
   the bulk, and they are pure evidence: what they hold has already been
   distilled into the library and the memory database;
2. the **transcripts** of runs that are no longer recent -- compressed,
   but still the largest per-run file;
3. the **runs themselves**, once they are old enough that neither the
   transcript nor the evidence is worth keeping.

What is never pruned: the experience library (it is the harness's actual
memory), the memory database, and the report of the most recent run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = ["RetentionPolicy", "prune_runs"]


@dataclass(frozen=True)
class RetentionPolicy:
    """How much of the run history to keep.

    The defaults are deliberately generous with the recent past -- a run
    being looked at today should have its transcript -- and stingy with
    the old one.
    """

    #: How many recent runs keep their evidence trees in full.
    keep_evidence_runs: int = 3
    #: How many recent runs keep their transcripts.
    keep_transcript_runs: int = 10
    #: How many runs are kept at all.
    keep_runs: int = 50
    #: Runs older than this are dropped regardless of count.
    max_age_days: int = 90

    def describe(self) -> str:
        return (
            f"evidence for {self.keep_evidence_runs} runs, "
            f"transcripts for {self.keep_transcript_runs}, "
            f"{self.keep_runs} runs total, "
            f"none older than {self.max_age_days} days"
        )


def _is_candidate_dir(p: Path) -> bool:
    """A directory holding one candidate's artifacts."""
    return (p / "candidate.json").is_file() or (p / "diff.patch").is_file()


def _is_session_dir(p: Path) -> bool:
    """A directory holding one session's candidate artifacts.

    The general path keeps a session's candidates in a sibling subtree of the
    namespace directory that holds the session's summary. Without recognising
    it, the candidates are never pruned: the policy reports that it ran and
    leaves the bulk of the disk behind.
    """
    try:
        children = list(p.iterdir())
    except OSError:
        return False
    return any(c.is_dir() and _is_candidate_dir(c) for c in children)


def _run_dirs(runs_root: Path) -> list[Path]:
    """The session directories under a runs root, oldest first."""
    if not runs_root.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    for p in runs_root.iterdir():
        if not p.is_dir():
            continue
        # A namespace directory holds one directory per session.
        if any((c / "summary.json").is_file() for c in p.iterdir() if c.is_dir()):
            for c in p.iterdir():
                if c.is_dir() and (c / "summary.json").is_file():
                    found.append((c.stat().st_mtime, c))
        elif (p / "summary.json").is_file():
            found.append((p.stat().st_mtime, p))
        elif _is_session_dir(p):
            found.append((p.stat().st_mtime, p))
    return [p for _, p in sorted(found)]


def _prune_tree(root: Path, names: tuple[str, ...]) -> int:
    """Remove the named subdirectories wherever they appear under root."""
    removed = 0
    for name in names:
        for p in root.rglob(name):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    return removed


def prune_runs(runs_root: Path, policy: RetentionPolicy | None = None) -> dict[str, int]:
    """Apply the retention policy. Returns what was removed."""
    policy = policy or RetentionPolicy()
    runs = _run_dirs(runs_root)
    counts = {"evidence_trees": 0, "transcripts": 0, "runs": 0}

    recent_evidence = set(runs[-policy.keep_evidence_runs :]) if policy.keep_evidence_runs else set()
    recent_transcript = (
        set(runs[-policy.keep_transcript_runs :]) if policy.keep_transcript_runs else set()
    )
    keep = set(runs[-policy.keep_runs :]) if policy.keep_runs else set()

    import time

    cutoff = time.time() - policy.max_age_days * 86400

    for run in runs:
        old_enough = run.stat().st_mtime < cutoff
        if old_enough or run not in keep:
            # Old enough to go entirely.
            shutil.rmtree(run, ignore_errors=True)
            counts["runs"] += 1
            continue

        if run not in recent_evidence:
            counts["evidence_trees"] += _prune_tree(run, ("context", "reflection", "selection"))
        if run not in recent_transcript:
            counts["transcripts"] += _prune_tree(
                run, ("trajectory.jsonl.gz", "trajectory.jsonl")
            )
    return counts
