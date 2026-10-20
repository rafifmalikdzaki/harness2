"""End-to-end test of the general path through the real engine.

This exercises the whole loop -- Propose-Probe-Compose, the anonymised
selection, and the cross-task library -- with the general adapter and a
scripted proposer. The solver is faked at the process boundary: a stub
`bun` stands in for the OpenCode checkout, so no model call is made and no
network is touched, but everything above that boundary is the production
code path.
"""

import subprocess
from pathlib import Path

import pytest

from harness2.adapters.general import GeneralTaskAdapter
from harness2.core.proposer import Proposer, Reply
from harness2.core.schedule import run_domain


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


def make_fake_opencode(tmp_path: Path) -> tuple[Path, Path]:
    """A stub OpenCode checkout and a stub `bun` that runs it.

    The stub writes a file into the workspace and emits one OpenCode-shaped
    event line, which is all the trajectory reader needs.
    """
    oc = tmp_path / "opencode"
    (oc / "packages" / "opencode" / "src").mkdir(parents=True)
    (oc / "packages" / "opencode" / "src" / "index.ts").write_text("// stub\n", encoding="utf-8")

    bun = tmp_path / "bun"
    bun.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "ws = argv[argv.index('--dir') + 1]\n"
        "open(f'{ws}/app.py', 'a').write('print(\"v2\")\\n')\n"
        "print(json.dumps({'type': 'text', 'part': {'text': 'edited app.py'}}))\n",
        encoding="utf-8",
    )
    bun.chmod(0o755)
    return oc, bun


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
            text = "<notes>Keep the helper script; the rollout that used it produced app.py with v2.</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


@pytest.fixture
def general_env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    oc, bun = make_fake_opencode(tmp_path)
    monkeypatch.setattr("harness2.config.OPENCODE_DIR", oc)
    monkeypatch.setattr("harness2.config.BUN", bun)
    return repo


def test_general_path_runs_the_real_engine(tmp_path, general_env, monkeypatch):
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


def test_general_path_writes_an_experience_entry(tmp_path, general_env, monkeypatch):
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
