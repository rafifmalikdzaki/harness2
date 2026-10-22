"""End-to-end test of the general path through the real engine.

This exercises the whole loop -- Propose-Probe-Compose, the anonymised
selection, and the cross-task library -- with the general adapter, a scripted
proposer and a scripted solver. No model service is touched and no network is
reached: the solver is faked at the HTTP boundary, and everything above that
boundary is the production code path.

The load-bearing assertion is the one about the HARNESS: the improver's
proposal has to reach the solver, or every candidate runs the same
instructions and the candidates differ in nothing but luck.
"""

import json
import subprocess
from pathlib import Path

import pytest

from harness2.adapters.general import GeneralTaskAdapter
from harness2.core.proposer import Proposer, Reply
from harness2.core.schedule import run_domain

SOLVER_SYSTEM_MARK = "You are a careful software/research engineer"
CRITIC_SYSTEM_MARK = "You are an independent, strict reviewer"

# The harness the scripted proposer writes. Its text has to turn up in the
# solver's prompt.
HARNESS_MARKER = "the helper script exists; call it before touching app.py"


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


class ScriptedProposer(Proposer):
    """Edits the harness deterministically; no model service is used."""

    def call(self, mission, context_dir, label, **kwargs):
        context = Path(context_dir)
        workspace = None
        if label.startswith("select."):
            candidates = sorted((context / "candidates").glob("*.md"))
            chosen = next(
                (p.stem for p in candidates if "v2" in p.read_text(encoding="utf-8")),
                candidates[0].stem if candidates else "A",
            )
            text = f"<why>The rollout that changed app.py.</why>\n```choice\n{chosen}\n```"
        elif label.startswith("experience."):
            text = (
                "<edits>\ncomponent: scripts/summarize.py\ncandidates: inv1\n"
                "change: none\nobserved: the rollout succeeded\nverdict: helped\n</edits>\n\n"
                "<experience>\ncue: When editing app.py\n"
                "lesson: The rollout appended to app.py and that is what worked.\n"
                "evidence: the app.py row of the candidate table.\n"
                "confidence: medium for this observed contrast\n</experience>"
            )
        else:
            workspace = context / "harness"
            (workspace / "scripts").mkdir(parents=True, exist_ok=True)
            (workspace / "scripts" / "summarize.py").write_text(
                "print('helper')\n", encoding="utf-8"
            )
            (workspace / "guardrails.md").write_text(
                f"# Standing guardrails\n\n- {HARNESS_MARKER}\n", encoding="utf-8"
            )
            text = "<notes>Keep the helper script; the rollout that used it produced app.py with v2.</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


@pytest.fixture
def general_env(tmp_path, monkeypatch):
    return make_repo(tmp_path)


@pytest.fixture
def solver(scripted_llm):
    """A scripted solver and critic; records every prompt it was given."""
    import json as _json

    def reply(messages, **kwargs):
        system = messages[0].get("content", "")
        if SOLVER_SYSTEM_MARK in system:
            return _json.dumps(
                {
                    "plan": "append the second line",
                    "edits": [
                        {
                            "path": "app.py",
                            "action": "write",
                            "content": "print('v1')\nprint('v2')\n",
                        }
                    ],
                    "claims": [],
                }
            )
        if CRITIC_SYSTEM_MARK in system:
            return _json.dumps(
                {"score": 8, "goal_met": True, "issues": [], "unverified_claims": []}
            )
        return _json.dumps({"notes": "no edit", "edits": []})

    return scripted_llm(reply)


def _solver_prompts(calls) -> list[str]:
    """The user messages the solver was given."""
    return [
        m["messages"][1]["content"]
        for m in calls
        if SOLVER_SYSTEM_MARK in m["messages"][0].get("content", "")
    ]


def test_general_path_runs_the_real_engine(tmp_path, general_env, monkeypatch, solver):
    """Propose -> probe -> compose -> select, on a real repository."""
    repo = general_env
    adapter = GeneralTaskAdapter(repo_root=repo, goal="append to app.py", model="p/m")

    proposer = ScriptedProposer(adapter.spec.channels["proposer"])
    monkeypatch.setattr("harness2.core.proposer.Proposer.__post_init__", lambda self: None)

    result = run_domain(
        "general",
        adapter.tasks(),
        1,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=adapter.harness_dir / "library" / "k1",
        modes=("agg",),
    )

    assert result["failed"] == 0, result
    assert result["done"] == 1

    # the rollouts really ran the solver in a worktree
    run_dirs = list((adapter.spec.runs_root / adapter.session_id).iterdir())
    assert run_dirs, "no rollout directories were produced"
    diff = (run_dirs[0] / "diff.patch").read_text(encoding="utf-8")
    assert "app.py" in diff, diff
    assert "v2" in diff, diff

    # the harness the improver wrote is NOT in the deliverable
    assert "summarize.py" not in diff, diff

    # a selection was made and the task completed
    assert result["tasks"][0]["status"] in ("ok", "skipped")


def test_the_harness_reaches_the_solver(tmp_path, general_env, monkeypatch, solver):
    """The improver's proposal must reach the solver's prompt.

    This is the assertion the whole loop stands on. When the harness was only
    written to disk, every candidate ran the same instructions and the
    candidates differed in nothing but luck.
    """
    repo = general_env
    adapter = GeneralTaskAdapter(repo_root=repo, goal="append to app.py", model="p/m")

    proposer = ScriptedProposer(adapter.spec.channels["proposer"])
    monkeypatch.setattr("harness2.core.proposer.Proposer.__post_init__", lambda self: None)

    run_domain(
        "general",
        adapter.tasks(),
        1,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=adapter.harness_dir / "library" / "k1",
        modes=("agg",),
    )

    prompts = _solver_prompts(solver)
    assert prompts, "the solver was never called"
    with_harness = [p for p in prompts if "# HARNESS" in p]
    assert with_harness, "the proposed harness never reached the solver's prompt"
    assert any(HARNESS_MARKER in p for p in with_harness), (
        "the proposed harness never reached the solver's prompt"
    )
    # The harness rides ABOVE the goal: it is the operating instruction, and the
    # goal is the thing it is applied to.
    assert with_harness[0].index("# HARNESS") < with_harness[0].index("# Goal")


def test_the_candidate_records_the_harness_it_ran(tmp_path, general_env, monkeypatch, solver):
    """A candidate says which harness it ran, so a run can be audited after the fact."""
    repo = general_env
    adapter = GeneralTaskAdapter(repo_root=repo, goal="append to app.py", model="p/m")

    proposer = ScriptedProposer(adapter.spec.channels["proposer"])
    monkeypatch.setattr("harness2.core.proposer.Proposer.__post_init__", lambda self: None)

    run_domain(
        "general",
        adapter.tasks(),
        1,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=adapter.harness_dir / "library" / "k1",
        modes=("agg",),
    )

    seen = set()
    for path in (adapter.spec.runs_root / adapter.session_id).glob("*/candidate.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        harness = record.get("harness") or {}
        seen.add(harness.get("digest"))
        assert "applied" in harness, harness
    assert len(seen) > 1, f"every candidate ran the same harness: {seen}"


def test_general_path_writes_an_experience_entry(tmp_path, general_env, monkeypatch, solver):
    """The cross-task library records a lesson for the next run."""
    repo = general_env
    adapter = GeneralTaskAdapter(repo_root=repo, goal="append to app.py", model="p/m")
    proposer = ScriptedProposer(adapter.spec.channels["proposer"])
    monkeypatch.setattr("harness2.core.proposer.Proposer.__post_init__", lambda self: None)

    library_root = adapter.harness_dir / "library" / "k1"
    run_domain(
        "general",
        adapter.tasks(),
        1,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=library_root,
        modes=("agg",),
    )

    indexes = list(library_root.glob("**/general/INDEX.md"))
    assert indexes, f"no library index under {library_root}"
    experiences = list(library_root.glob("**/EXPERIENCE.md"))
    assert experiences, "no experience entry was written"
    body = experiences[0].read_text(encoding="utf-8")
    assert "**cue**" in body and "**lesson**" in body
    assert "app.py" in body
