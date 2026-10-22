"""The MCP tools: what one tool writes and another tool reads.

The tools are a contract between the harness and whatever agent is on the
other end of the MCP connection. A key that one tool writes and another does
not read is a silent failure: the second tool falls back to something else and
the caller never finds out.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness2 import mcp_server


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


# --------------------------------------------------------------- the schema


def test_the_selection_record_uses_the_schema_the_readers_expect(tmp_path):
    """harness2_solve writes it; harness2_finish and the CLI read it.

    `recommended` was the only key it wrote, and nothing read that.
    """
    from harness2.adapters.general import recommended_label, recommended_run_label

    repo = make_repo(tmp_path)
    session = "sess_schema"
    runs = repo / ".harness2" / "runs" / session
    runs.mkdir(parents=True)
    (runs / "selection.json").write_text(
        json.dumps(
            {
                "chosen_label": "cand1_minimal",
                "chosen_run_id": "cand1_minimal",
                "recommended": "cand1_minimal",
                "ranking": ["cand1_minimal"],
            }
        ),
        encoding="utf-8",
    )
    assert recommended_label(repo, session) == "cand1_minimal"
    assert recommended_run_label(repo, session) == "cand1_minimal"


def test_a_stage_label_resolves_to_the_run_label(tmp_path):
    """The selection records a stage label; the worktree is named after the run."""
    from harness2.adapters.general import recommended_run_label

    repo = make_repo(tmp_path)
    session = "sess_stage"
    target = (
        repo / ".harness2" / "runs" / "general-oc-agg1" / session / "selection" / "agg1"
    )
    target.mkdir(parents=True)
    (target / "selection.json").write_text(
        json.dumps(
            {
                "chosen_label": "base1",
                "chosen_run_id": f"general-oc-agg1/{session}/base1",
            }
        ),
        encoding="utf-8",
    )
    assert recommended_run_label(repo, session) == f"general-oc-agg1__{session}__base1"


# --------------------------------------------------------------- robustness


def test_finish_on_a_non_git_workspace_is_an_error_not_a_crash(tmp_path):
    out = mcp_server.harness2_finish(session_id="sess_x", workspace=str(tmp_path))
    assert out["status"] == "error"
    assert "not a git repository" in out["message"]
    # And it did not create state in a directory that is not a repository.
    assert not (tmp_path / ".harness2").exists()


def test_finish_on_an_unknown_session_is_an_error(tmp_path):
    repo = make_repo(tmp_path)
    out = mcp_server.harness2_finish(session_id="sess_nope", workspace=str(repo))
    assert out["status"] == "error"
    assert "not found" in out["message"]


def test_finish_on_a_session_with_no_rollouts_is_an_error(tmp_path):
    repo = make_repo(tmp_path)
    from harness2.core.memory_db import MemoryDB

    db = MemoryDB(repo / ".harness2" / "memory.db")
    session = db.start_session("x", "general")
    out = mcp_server.harness2_finish(session_id=session, workspace=str(repo))
    assert out["status"] == "error"
    assert "no rollouts" in out["message"]


def test_a_critic_that_returns_strings_does_not_kill_the_tool():
    """The critic is a model. Its issues are dicts by contract, not by law."""
    candidate = {
        "label": "c1",
        "test_passed": None,
        "has_changes": False,
        "review": {"score": None, "issues": ["the diff is wrong"], "unverified_claims": []},
    }
    # The old code called `.get` on the string.
    ranked = sorted([candidate], key=mcp_server._rank_key, reverse=True)
    assert ranked[0]["label"] == "c1"


def test_the_library_inspection_refuses_a_path_like_domain(tmp_path):
    out = mcp_server.harness2_inspect_library(domain="..", runs_dir=str(tmp_path))
    assert out["status"] == "error"
    assert "not a path" in out["message"]


def test_the_library_inspection_finds_the_general_path_library(tmp_path):
    """harness2_recursion writes the library under the workspace."""
    from harness2.core.library import Library
    from harness2.demo import DemoAdapter

    repo = make_repo(tmp_path)
    adapter = DemoAdapter(repo)
    surface = adapter.substrate.surface()
    lib = Library(
        root=repo / ".harness2" / "library" / "k1" / "agg",
        domain="accounting",
        surface=surface,
    )
    lib.add(
        "ledger-1",
        {"systemprompt.md": "a"},
        why="w",
        label="l",
        run_id="r",
        experience={"cue": "c", "lesson": "l", "evidence": "e"},
    )
    out = mcp_server.harness2_inspect_library(
        domain="accounting", workspace=str(repo)
    )
    assert out["status"] == "ok"
    assert out["found_libraries"] >= 1, out
    assert out["libraries"][0]["entries"], out


def test_the_config_status_does_not_spend_a_call_by_default():
    out = mcp_server.harness2_config_status()
    assert out["status"] in ("ok", "error")
    assert out["probed"] is False


def test_the_success_responses_all_carry_a_status(tmp_path):
    """A caller that checks `result["status"]` must not get a KeyError."""
    repo = make_repo(tmp_path)
    for out in (
        mcp_server.harness2_list_sessions(workspace=str(repo)),
        mcp_server.harness2_query_memory(workspace=str(repo)),
        mcp_server.harness2_inspect_library(domain="accounting", runs_dir=str(tmp_path)),
        mcp_server.harness2_config_status(),
    ):
        assert "status" in out, out
