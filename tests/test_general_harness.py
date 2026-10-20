"""Tests for the general-task harness: the path that runs the real engine.

No model calls are made here. The tests cover the contract the engine
depends on -- the adapter satisfying the benchmark protocol, the harness
staging into a workspace, the scaffolding staying out of the deliverable,
and the CLI/MCP entry points refusing bad input -- so a regression in the
general path fails here rather than after a paid run.
"""

import subprocess
from pathlib import Path

import pytest

from harness2.adapters.general import GeneralTaskAdapter
from harness2.adapters.general_runner import stage_harness
from harness2.core import tree


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


def make_adapter(tmp_path: Path, **kw) -> GeneralTaskAdapter:
    repo = make_repo(tmp_path)
    return GeneralTaskAdapter(repo_root=repo, goal="make it better", **kw)


# ---------------------------------------------------------------- protocol


def test_adapter_satisfies_the_benchmark_protocol(tmp_path):
    adapter = make_adapter(tmp_path)
    assert adapter.tasks() == [adapter.session_id]
    assert adapter.domain_of(adapter.session_id) == "general"
    assert "make it better" in adapter.task_prompt(adapter.session_id)
    assert adapter.task_files(adapter.session_id) == adapter.repo_root
    assert adapter.base_harness().is_dir()
    adapter.validate_tree({})


def test_base_harness_validates_against_the_opencode_surface(tmp_path):
    adapter = make_adapter(tmp_path)
    surface = adapter.substrate.surface()
    harness = tree.read_tree(surface, adapter.base_harness())
    tree.validate(surface, harness)
    assert "systemprompt.md" in harness
    assert "make it better" in harness["systemprompt.md"]


def test_long_term_memory_reaches_the_base_harness(tmp_path):
    """Memory from an earlier session must reach the next run's harness.

    The base harness is written when the adapter is built, so a fact
    recorded beforehand is what the next run starts with.
    """
    repo = make_repo(tmp_path)
    from harness2.core.memory_db import MemoryDB

    earlier = MemoryDB(repo / ".harness2" / "memory.db")
    earlier.add_longterm_fact("convention", "always run make check", 1.0)

    adapter = GeneralTaskAdapter(repo_root=repo, goal="x")
    surface = adapter.substrate.surface()
    harness = tree.read_tree(surface, adapter.base_harness())
    assert "always run make check" in harness["LongTermMEMORY.md"]


def test_internal_state_is_gitignored(tmp_path):
    repo = make_repo(tmp_path)
    GeneralTaskAdapter(repo_root=repo, goal="x")
    ignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".harness2/" in ignore
    assert ".harness_staged/" in ignore


# ---------------------------------------------------------------- staging


def test_stage_harness_places_files_where_opencode_loads_them(tmp_path):
    adapter = make_adapter(tmp_path)
    surface = adapter.substrate.surface()
    staged = tmp_path / "staged"
    tree.write_tree(
        surface,
        {
            "systemprompt.md": "Do the thing.\n",
            "guardrails.md": "- Never guess.\n",
            "LongTermMEMORY.md": "",
            "tool_descriptions/tool_guidance.md": "",
        },
        staged,
        stubs=False,
    )
    (staged / "scripts").mkdir(exist_ok=True)
    (staged / "scripts" / "helper.py").write_text("print('hi')\n", encoding="utf-8")
    (staged / "skills" / "review").mkdir(parents=True, exist_ok=True)
    (staged / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n---\n", encoding="utf-8"
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    authored = stage_harness(staged, workspace, adapter._render_ctx())

    assert (workspace / "AGENTS.md").is_file()
    assert "Do the thing." in (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert (workspace / "scripts" / "helper.py").is_file()
    assert (workspace / ".opencode" / "skills" / "review" / "SKILL.md").is_file()
    assert not (workspace / ".harness_render").exists()
    assert "AGENTS.md" in authored
    assert "scripts/helper.py" in authored
    assert ".opencode/skills/review/SKILL.md" in authored


def test_harness_files_stay_out_of_the_deliverable(tmp_path):
    """The harness the improver edited is scaffolding, not the agent's work."""
    repo = make_repo(tmp_path)
    adapter = GeneralTaskAdapter(repo_root=repo, goal="x", model="p/m")
    wt = adapter.wt_mgr.create(adapter.session_id, "probe_1")

    # The launcher stages the harness into the worktree...
    surface = adapter.substrate.surface()
    staged = tmp_path / "staged"
    tree.write_tree(
        surface,
        {
            "systemprompt.md": "Do the thing.\n",
            "guardrails.md": "",
            "LongTermMEMORY.md": "",
            "tool_descriptions/tool_guidance.md": "",
        },
        staged,
        stubs=False,
    )
    (staged / "scripts").mkdir(exist_ok=True)
    (staged / "scripts" / "helper.py").write_text("print('h')\n", encoding="utf-8")
    authored = stage_harness(staged, wt, adapter._render_ctx())

    # ...and the agent changes something of its own.
    (wt / "app.py").write_text("print('v2')\n", encoding="utf-8")

    diff = adapter.wt_mgr.get_diff(adapter.session_id, "probe_1", exclude=authored)
    assert "app.py" in diff
    assert "helper.py" not in diff
    assert "AGENTS.md" not in diff


# ---------------------------------------------------------------- CLI / MCP


def test_cli_rejects_a_non_git_workspace():
    from harness2.cli.general import main

    with pytest.raises(SystemExit):
        main(["--workspace", "/tmp", "--goal", "x"])


def test_cli_requires_a_solver_model(tmp_path, monkeypatch):
    from harness2 import config
    from harness2.cli.general import main

    repo = make_repo(tmp_path)
    monkeypatch.setattr(config, "SOLVER_MODEL_OC", "")
    with pytest.raises(SystemExit):
        main(["--workspace", str(repo), "--goal", "x"])


def test_mcp_recursion_rejects_a_non_git_workspace():
    from harness2.mcp_server import harness2_recursion

    out = harness2_recursion(goal="x", workspace="/tmp")
    assert out["status"] == "error"
    assert "not a git repository" in out["message"]


def test_mcp_recursion_reports_missing_models(tmp_path, monkeypatch):
    from harness2 import config
    from harness2.mcp_server import harness2_recursion

    repo = make_repo(tmp_path)
    monkeypatch.setattr(config, "SOLVER_MODEL_OC", "")
    monkeypatch.setattr(config, "IMPROVER_MODEL", "")
    out = harness2_recursion(goal="x", workspace=str(repo))
    assert out["status"] == "error"
    assert "HARNESS2_SOLVER_MODEL" in out["message"]


# ---------------------------------------------------------------- evolution


def test_evolution_lessons_name_the_files_that_changed(tmp_path):
    """A lesson must be derived from the run, not a template with the goal in it."""
    from harness2.core.evolution import SelfEvolutionEngine

    repo = make_repo(tmp_path)
    engine = SelfEvolutionEngine(repo)
    session = engine.db.start_session("add a greeting", "general")
    engine.db.record_rollout(
        session, "good", "/tmp/wt1", "diff --git a/greet.py b/greet.py\n+++ b/greet.py\n", True, ""
    )
    engine.db.record_rollout(
        session, "bad", "/tmp/wt2", "diff --git a/wrong.py b/wrong.py\n+++ b/wrong.py\n", False, ""
    )
    out = engine.analyze_session(session)
    lesson = out["evolutions"][0]["lesson"]
    assert "greet.py" in lesson
    assert "good" in lesson
    # the skill names the file it verified against
    skill = out["synthesized_skills"][0]["content"]
    assert "greet.py" in skill


def test_adapter_channels_name_a_real_model(tmp_path):
    """The scheduler builds the selector's proposer from this channel.

    A placeholder there (the old "general/solver") makes the selection
    session fail with "Model unavailable", so the channel must name a model
    the provider can actually serve.
    """
    adapter = make_adapter(tmp_path, model="mistral/mistral-large-latest")
    for name in ("proposer", "selector"):
        channel = adapter.spec.channels[name]
        assert channel.model == "mistral/mistral-large-latest"
        assert "/" in channel.model, "the opencode channel needs provider/model"


def test_selector_model_may_differ_from_the_solver(tmp_path):
    adapter = make_adapter(
        tmp_path,
        model="mistral/mistral-large-latest",
        selector_model="mistral/mistral-small-latest",
    )
    assert adapter.spec.channels["selector"].model == "mistral/mistral-small-latest"
