import subprocess
from pathlib import Path
import pytest
from harness2.core.memory_db import MemoryDB
from harness2.core.evolution import SelfEvolutionEngine
from harness2.adapters.worktree import WorktreeManager
from harness2.mcp_server import harness2_apply_harness, harness2_finish


def test_sqlite_memory_lifecycle(tmp_path):
    db_path = tmp_path / "test_memory.db"
    db = MemoryDB(db_path)

    sess = db.start_session("Refactor database access", "backend")
    assert sess.startswith("sess_")

    r1 = db.record_rollout(sess, "probe1", "/tmp/wt1", "diff1", True, "4 tests passed")
    assert r1.startswith("roll_")

    db.add_longterm_fact("database", "Use SQLAlchemy async sessions", 1.0)
    facts = db.query_longterm_memory()
    assert len(facts) == 1
    assert facts[0]["category"] == "database"

    db.close_session(sess, "accepted", r1)
    session_data = db.get_session(sess)
    assert session_data["status"] == "accepted"
    assert session_data["best_rollout_id"] == r1


def test_worktree_isolation_and_no_leak(tmp_path):
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    wt_mgr = WorktreeManager(repo, base_temp_dir=tmp_path / "wts")
    sess_id = "sess_testwt"
    wt = wt_mgr.create(sess_id, "probe1")
    assert wt.is_dir()

    # Modify code in worktree
    (wt / "app.py").write_text("print('v2')\n", encoding="utf-8")
    diff = wt_mgr.get_diff(sess_id, "probe1")
    assert "print('v2')" in diff

    # Apply to repo
    wt_mgr.apply_to_repo(sess_id, "probe1")
    assert (repo / "app.py").read_text() == "print('v2')\n"

    # Cleanup
    cleaned = wt_mgr.cleanup_session(sess_id)
    assert len(cleaned) == 1
    assert not wt.exists()


def test_apply_harness_single_pass_merge(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# EXISTING RULES\n- Rule 1\n", encoding="utf-8")
    (ws / "opencode.json").write_text("{}", encoding="utf-8")

    harness_src = tmp_path / "harness"
    harness_src.mkdir()
    (harness_src / "systemprompt.md").write_text("Prompt content\n", encoding="utf-8")
    (harness_src / "guardrails.md").write_text("Guardrail content\n", encoding="utf-8")
    (harness_src / "LongTermMEMORY.md").write_text("Memory content\n", encoding="utf-8")

    res = harness2_apply_harness(str(harness_src), str(ws), dry_run=False, backup=True)
    assert res["status"] == "applied"
    assert len(res["backups_created"]) == 1

    content = (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "# EXISTING RULES" in content
    assert "Prompt content" in content
    assert "Guardrail content" in content
    assert "Memory content" in content


def test_self_evolution_engine(tmp_path):
    db_path = tmp_path / "mem.db"
    db = MemoryDB(db_path)
    sess = db.start_session("Build REST API with pytest and FastAPI", "web")
    db.record_rollout(sess, "probe1", "/tmp/wt1", "diff1", False, "pytest error: 404")
    db.record_rollout(sess, "probe2", "/tmp/wt2", "diff2", True, "pytest passed 6 tests")

    engine = SelfEvolutionEngine(tmp_path, db=db)
    res = engine.analyze_session(sess)
    assert res["status"] == "evolved"
    assert len(res["synthesized_facts"]) >= 2
    assert len(res["synthesized_skills"]) >= 1

    facts = db.query_longterm_memory()
    categories = [f["category"] for f in facts]
    assert "testing" in categories
    assert "architecture" in categories
