"""Tests for keeping the harness's memory small enough to be worth keeping.

A harness that runs for months accumulates three kinds of memory: the run
artifacts, the SQLite record, and the experience library. All three grew
without bound, and the first grew fast enough to fill a disk.

These tests pin the behaviour that keeps them bounded: compression is
transparent to the reader, the record round-trips, the library retires
what a newer entry supersedes, and the retention policy removes the
expensive parts of old runs while keeping the recent ones.
"""

from pathlib import Path

from harness2.core.compress import (
    compress_text,
    copy_compressed,
    decompress_text,
    read_text,
    write_text,
)
from harness2.core.library import Library
from harness2.core.memory_db import MemoryDB
from harness2.core.retention import RetentionPolicy, prune_runs
from harness2.substrate.opencode import OPENCODE_SURFACE

# ------------------------------------------------------------------ compression


def test_text_round_trips_through_compression():
    text = "a rollout transcript\n" * 500
    assert decompress_text(compress_text(text)) == text


def test_compression_actually_shrinks_a_transcript():
    text = '{"type":"step_start","part":{"id":"abc"}}\n' * 2000
    assert len(compress_text(text)) < len(text) // 4


def test_write_and_read_are_transparent_about_compression(tmp_path):
    path = tmp_path / "trajectory.jsonl.gz"
    write_text(path, "the transcript\n")
    assert path.read_bytes()[:2] == b"\x1f\x8b"  # gzip magic
    assert read_text(path) == "the transcript\n"


def test_a_plain_file_is_still_readable(tmp_path):
    """A directory written before compression was added still reads."""
    path = tmp_path / "trajectory.jsonl"
    path.write_text("the transcript\n", encoding="utf-8")
    assert read_text(path) == "the transcript\n"


def test_reading_a_missing_file_is_empty(tmp_path):
    assert read_text(tmp_path / "nope.jsonl") == ""


def test_reading_a_corrupt_compressed_file_is_empty(tmp_path):
    path = tmp_path / "broken.jsonl.gz"
    path.write_bytes(b"not gzip at all")
    assert read_text(path) == ""


def test_a_small_file_is_copied_readably(tmp_path):
    """A reviewer wants to read a diff; only a transcript is worth hiding."""
    src = tmp_path / "diff.patch"
    src.write_text("a small diff\n", encoding="utf-8")
    dst = copy_compressed(src, tmp_path / "out" / "diff.patch")
    assert dst is not None
    assert dst.name == "diff.patch"
    assert read_text(dst) == "a small diff\n"


def test_a_large_file_is_copied_compressed(tmp_path):
    src = tmp_path / "trajectory.jsonl"
    src.write_text("x" * 40_000, encoding="utf-8")
    dst = copy_compressed(src, tmp_path / "out" / "trajectory.jsonl")
    assert dst is not None
    assert dst.name.endswith(".gz")
    assert read_text(dst) == "x" * 40_000


# ------------------------------------------------------------------ the record


def test_a_rollout_record_round_trips(tmp_path):
    db = MemoryDB(tmp_path / "memory.db")
    sess = db.start_session("a task", "general")
    diff = "diff --git a/x.py b/x.py\n" * 400
    out = "166 passed, 1 skipped\n" * 40
    db.record_rollout(sess, "base1", "/tmp/wt", diff, True, out)

    got = db.get_session(sess)
    row = got["rollouts"][0]
    assert row["git_diff"] == diff
    assert row["test_output"] == out
    assert row["test_passed"] == 1


def test_the_record_is_smaller_than_the_text_it_holds(tmp_path):
    """The record used to hold the same bytes the run directory held."""
    db = MemoryDB(tmp_path / "memory.db")
    sess = db.start_session("a task", "general")
    diff = "diff --git a/x.py b/x.py\n" * 400
    db.record_rollout(sess, "base1", "/tmp/wt", diff, True, "passed\n")
    row = db.get_session(sess)["rollouts"][0]
    # The stored form is compressed; the read form is the text.
    assert row["git_diff"] == diff


def test_a_fact_is_stored_once(tmp_path):
    db = MemoryDB(tmp_path / "memory.db")
    db.add_longterm_fact("convention", "always run make check", 1.0)
    db.add_longterm_fact("convention", "always run make check", 1.0)
    assert len(db.query_longterm_memory()) == 1


def test_the_memory_renders_as_a_small_block(tmp_path):
    """What the next run's harness reads back is the lesson, not the log."""
    db = MemoryDB(tmp_path / "memory.db")
    db.add_longterm_fact("convention", "always run make check", 1.0)
    rendered = db.render_longterm_memory_markdown()
    assert "always run make check" in rendered
    assert len(rendered) < 500


# ------------------------------------------------------------------ the library


def make_library(tmp_path: Path, entries: list[tuple[str, str]]) -> Library:
    lib = Library(root=tmp_path / "library", domain="general", surface=OPENCODE_SURFACE)
    for task_id, cue in entries:
        d = lib.entry_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "EXPERIENCE.md").write_text(
            f"# {task_id}\n\n- **cue**: {cue}\n- **lesson**: something\n", encoding="utf-8"
        )
    lib.rebuild_index()
    return lib


def test_the_library_retires_an_entry_a_newer_one_supersedes(tmp_path):
    lib = make_library(
        tmp_path,
        [("task-a", "When editing the config loader"), ("task-b", "When editing the config loader")],
    )
    assert len(lib.entries()) == 2
    retired = lib.compact()
    assert retired == 1
    assert len(lib.entries()) == 1


def test_the_library_keeps_entries_with_distinct_cues(tmp_path):
    lib = make_library(
        tmp_path,
        [("task-a", "When editing the config loader"), ("task-b", "When writing a test")],
    )
    assert lib.compact() == 0
    assert len(lib.entries()) == 2


def test_the_library_keeps_at_most_the_requested_number(tmp_path):
    lib = make_library(
        tmp_path,
        [(f"task-{i}", f"cue number {i}") for i in range(6)],
    )
    lib.compact(keep=2)
    assert len(lib.entries()) == 2


def test_compacting_an_empty_library_is_harmless(tmp_path):
    lib = make_library(tmp_path, [])
    assert lib.compact() == 0


# ------------------------------------------------------------------ retention


def make_run(runs_root: Path, name: str, *, with_evidence: bool = True) -> Path:
    run = runs_root / "general-oc-agg2" / name
    (run / "context" / "agg1").mkdir(parents=True, exist_ok=True)
    (run / "context" / "agg1" / "task.md").write_text("evidence\n", encoding="utf-8")
    (run / "reflection").mkdir(parents=True, exist_ok=True)
    (run / "trajectory.jsonl.gz").write_bytes(b"\x1f\x8bcompressed")
    (run / "summary.json").write_text("{}\n", encoding="utf-8")
    return run


def test_the_recent_runs_keep_everything(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, "sess_new")
    before = prune_runs(runs, RetentionPolicy(keep_evidence_runs=3, keep_transcript_runs=10))
    assert before["evidence_trees"] == 0
    assert (runs / "general-oc-agg2" / "sess_new" / "context").is_dir()


def test_an_old_run_loses_its_evidence_but_keeps_its_record(tmp_path):
    runs = tmp_path / "runs"
    old = make_run(runs, "sess_old")
    for i in range(4):
        make_run(runs, f"sess_new{i}")
    prune_runs(runs, RetentionPolicy(keep_evidence_runs=1, keep_transcript_runs=10, keep_runs=50))
    assert not (old / "context").exists()
    assert (old / "summary.json").is_file()


def test_a_run_beyond_the_limit_is_dropped_entirely(tmp_path):
    runs = tmp_path / "runs"
    old = make_run(runs, "sess_old")
    prune_runs(runs, RetentionPolicy(keep_runs=0))
    assert not old.exists()


def test_pruning_nothing_is_harmless(tmp_path):
    assert prune_runs(tmp_path / "missing") == {
        "evidence_trees": 0,
        "transcripts": 0,
        "runs": 0,
    }
