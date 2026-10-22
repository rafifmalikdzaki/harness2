"""The safety properties, and the crashes that hid them.

The sandbox is the harness's central safety claim. A claim that is not tested
is a claim that decays: the sandbox was applied to one of the two runtimes
and silently not to the other, the OpenCode binary's directory was bound
read-write though the docs said read-only, and the verification command ran
on the host outside the sandbox.
"""

from __future__ import annotations

import subprocess

from harness2.adapters import sandbox as sbx
from harness2.adapters.diffguard import analyze_patch, describe_destructive
from harness2.core import compress, evolution
from harness2.core.memory_db import MemoryDB
from harness2.substrate.opencode import opencode_command

# ----------------------------------------------------------------- the sandbox


def test_the_sandbox_wraps_the_source_runtime(tmp_path):
    """The pinned checkout is the default deployment, so it must be sandboxed too."""
    oc = tmp_path / "oc"
    (oc / "packages" / "opencode" / "src").mkdir(parents=True)
    (oc / "packages" / "opencode" / "src" / "index.ts").write_text("// stub\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()

    cmd = opencode_command(
        prompt="p",
        workspace=ws,
        model="p/m",
        opencode_dir=oc,
        bun="/bin/true",
        sandbox=True,
    )
    assert cmd.kind == "source"
    assert "bwrap" in cmd.argv[0], cmd.argv
    assert "--bind" in cmd.argv, cmd.argv
    # The process starts in the workspace, not in the checkout.
    assert cmd.cwd == ws, cmd.cwd
    # The operator's environment does not reach the rollout.
    assert "OPENAI_API_KEY" not in cmd.env
    # And the tool fence is present, not just the path fence.
    assert "OPENCODE_CONFIG_CONTENT" in cmd.env


def test_the_sandbox_wraps_the_binary_runtime(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    cmd = opencode_command(
        prompt="p", workspace=ws, model="p/m", binary=str(fake), sandbox=True
    )
    assert cmd.kind == "binary"
    assert "bwrap" in cmd.argv[0], cmd.argv
    assert cmd.cwd == ws


def test_the_opencode_binary_directory_is_read_only(tmp_path):
    """A rollout that could overwrite the binary would leave a change that
    outlives the sandbox and is executed by every later run."""
    ws = tmp_path / "ws"
    ws.mkdir()
    exe = tmp_path / "bin" / "opencode"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)

    argv = sbx.sandbox_argv(ws, executable=str(exe))
    joined = " ".join(argv)
    assert f"--ro-bind {exe.parent} {exe.parent}" in joined
    assert f"--bind {exe.parent} {exe.parent}" not in joined


def test_the_sandbox_keeps_the_workspace_writable(tmp_path):
    """A read-only mount that CONTAINS the workspace must come first."""
    ws = tmp_path / "project" / "work"
    ws.mkdir(parents=True)
    argv = sbx.sandbox_argv(ws, extra_readonly=[(tmp_path / "project", tmp_path / "project")])

    def index_of(flag: str, target: str) -> int:
        return next(
            i
            for i in range(len(argv) - 1)
            if argv[i] == flag and argv[i + 1] == str(target)
        )

    ro = index_of("--ro-bind", tmp_path / "project")
    rw = index_of("--bind", ws)
    assert ro < rw, argv


def test_the_sandbox_drops_the_secrets(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = sbx.sandbox_env(ws, env={"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x", "HOME": "/home/u"})
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


# ------------------------------------------------------------- the diff guard


def test_the_guard_fails_closed_on_a_patch_it_cannot_read():
    stats = analyze_patch("this is not a diff at all\n")
    assert not stats.parseable
    assert stats.destructive
    assert "could not be read" in describe_destructive(stats)


def test_the_guard_reads_the_path_not_a_line_inside_the_hunk():
    """A deleted file whose body contains '--- ' is not named after that line."""
    patch = (
        "diff --git a/notes.md b/notes.md\n"
        "deleted file mode 100644\n"
        "index 111..000\n"
        "--- a/notes.md\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-- a rule that starts with two dashes\n"
        "-another line\n"
    )
    stats = analyze_patch(patch)
    assert stats.deleted == ("notes.md",), stats


def test_the_guard_counts_a_deletion():
    patch = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x = 1\n"
        "-y = 2\n"
    )
    stats = analyze_patch(patch)
    assert stats.deleted == ("gone.py",)
    assert stats.bytes_removed == 10


# ------------------------------------------------------------- what is reviewed


def test_the_diff_shows_the_files_the_agent_created(tmp_path):
    """'git diff HEAD' never shows an untracked file.

    The selector read a diff that said '?? newpkg/' and nothing else, while
    the apply produced the real thing.
    """
    from harness2.adapters.worktree import WorktreeManager

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    mgr = WorktreeManager(repo, base_temp_dir=tmp_path / "wt")
    wt = mgr.create("sess", "cand")
    pkg = wt / "newpkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("VALUE = 42\n", encoding="utf-8")

    diff = mgr.get_diff("sess", "cand")
    assert "newpkg/mod.py" in diff, diff
    assert "VALUE = 42" in diff, diff


# ------------------------------------------------------------- the verdict


def test_a_candidate_whose_solver_never_ran_is_ungraded(tmp_path, scripted_llm):
    """A command that was never invoked is not a failing test."""
    from harness2.adapters.general import GeneralTaskAdapter, _verdict_from_output

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    # The solver is reachable but answers with something that is not JSON, so
    # it produces no code and the verifier is never invoked.
    scripted_llm(["I am afraid I cannot do that."])

    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", verify_cmd="true")
    run_dir = adapter.execute("t", None, "cand1")
    output = (run_dir / "test_output.txt").read_text(encoding="utf-8")
    assert output.startswith("VERIFICATION: NOT RUN"), output
    assert _verdict_from_output(output) is None
    assert adapter.score(run_dir, "t") is None


# ------------------------------------------------------------------- crashes


def test_a_corrupt_gzip_does_not_raise(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(b"\x1f\x8b" + b"\x00" * 40)
    assert compress.read_text(p) == ""


def test_a_corrupt_blob_does_not_take_the_record_with_it(tmp_path):
    db = MemoryDB(tmp_path / "m.db")
    session = db.start_session("x", "general")
    db.record_rollout(session, "c1", git_diff="gz1:" + "00" * 8, test_output="gz1:00")
    got = db.get_session(session)
    assert got is not None


def test_the_self_evolution_survives_a_null_diff(tmp_path):
    db = MemoryDB(tmp_path / "m.db")
    session = db.start_session("x", "general")
    db.record_rollout(session, "c1", git_diff=None, test_output=None, test_passed=1)
    out = evolution.SelfEvolutionEngine(tmp_path, db=db).analyze_session(session)
    assert out["status"] == "evolved", out


def test_the_retention_policy_prunes_the_transcripts(tmp_path):
    """The transcripts are FILES. Removing only directories never pruned them."""
    from harness2.core.retention import RetentionPolicy, prune_runs

    runs = tmp_path / "runs"
    for i in range(4):
        d = runs / f"ns{i}" / "task"
        d.mkdir(parents=True)
        (d / "summary.json").write_text("{}", encoding="utf-8")
        (d / "trajectory.jsonl").write_text("events\n", encoding="utf-8")
        (d / "context").mkdir()

    counts = prune_runs(runs, RetentionPolicy(keep_evidence_runs=1, keep_transcript_runs=1))
    assert counts["transcripts"] > 0, counts
    old = runs / "ns0" / "task" / "trajectory.jsonl"
    assert not old.exists(), "the transcript was not pruned"


def test_a_quoted_env_value_with_a_comment_keeps_its_value(tmp_path):
    from harness2 import config

    env = tmp_path / ".env"
    env.write_text(
        'A="quoted" # trailing comment\n'
        "B='plain'\n"
        "C=has # hash\n"
        'D="has # hash"\n',
        encoding="utf-8",
    )
    config.load_dotenv(env)
    import os

    assert os.environ["A"] == "quoted"
    assert os.environ["B"] == "plain"
    assert os.environ["C"] == "has"
    assert os.environ["D"] == "has # hash"


# ------------------------------------------------------------- the benchmarks


def test_the_jobbench_domain_lens_reaches_the_prompt():
    """The occupation is not a domain group.

    Passing the occupation where the group belonged made every lookup miss,
    and all seven hand-written DOMAIN LENS paragraphs were dead text.
    """
    from harness2.benchmarks.jobbench import (
        JOBBENCH_DOMAIN_OF_PROFESSION,
        JOBBENCH_DOMAINS,
        JOBBENCH_PROFESSIONS,
        jobbench_prompts,
    )

    for profession in JOBBENCH_PROFESSIONS:
        domain = JOBBENCH_DOMAIN_OF_PROFESSION.get(profession)
        assert domain in JOBBENCH_DOMAINS, (profession, domain)
        prompts = jobbench_prompts(domain=domain, profession=profession)
        assert "DOMAIN LENS" in prompts.domain_guidance, profession
        assert "PROFESSION LENS" in prompts.domain_guidance, profession
