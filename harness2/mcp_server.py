# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Harness² Model Context Protocol (MCP) Server.

Exposes Harness² recursive agent harnessing capabilities, SQLite memory engine,
controlled worktree lifecycle management, and general task solving to Claude Code,
Codex, OpenCode, and Antigravity (agy).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from harness2 import config
from harness2.adapters.general import GeneralTaskAdapter
from harness2.adapters.worktree import WorktreeManager
from harness2.core.evolution import SelfEvolutionEngine
from harness2.core.memory_db import MemoryDB

server = MCPServer(
    name="harness2",
    instructions=(
        "Harness² MCP Server: Automates recursive agent harnessing (Propose-Probe-Compose), "
        "SQLite-backed persistent session & long-term memory, and isolated worktree execution."
    ),
    version="0.5.0",
)


@server.tool()
def harness2_run_demo(
    k: int = 2,
    mode: str = "both",
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a local offline Harness² recursion demonstration.

    Executes tasks in the demo 'accounting' domain, proving the Propose-Probe-Compose
    workflow and persistent cross-task library updates without making external model calls.

    Args:
        k: Task-level recursion budget (default: 2).
        mode: Recursion mode ('parallel', 'sequential', or 'both').
        output_dir: Directory where run artifacts will be stored (defaults to runs/demo).
    """
    out = Path(output_dir).expanduser().resolve() if output_dir else config.RUNS / "demo"
    cmd = [
        sys.executable,
        "-m",
        "harness2.cli.demo",
        "--output",
        str(out),
        "--k",
        str(k),
        "--mode",
        mode,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    report_file = out / "demo.json"
    result: Dict[str, Any] = {
        "status": "success" if proc.returncode == 0 else "failed",
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }
    if report_file.is_file():
        try:
            result["report"] = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A torn report is worth saying out loud: a silent omission reads
            # as "there was no report".
            result["report_error"] = f"{type(exc).__name__}: {exc}"
    return result


@server.tool()
def harness2_inspect_library(
    domain: str = "accounting",
    runs_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect learned domain experiences, cues, lessons, and synthesized components.

    Harness² stores accepted skills, scripts, and lessons in per-domain libraries.
    Use this to see what solutions and rules earlier tasks discovered.

    Args:
        domain: Domain name (e.g., 'accounting', 'antitrust-competition', etc.).
        runs_dir: The root runs directory (defaults to config.RUNS).
    """
    root = Path(runs_dir).expanduser().resolve() if runs_dir else config.RUNS
    libraries = []
    for index_path in root.glob(f"**/library/**/{domain}/INDEX.md"):
        domain_root = index_path.parent
        index_content = index_path.read_text(encoding="utf-8", errors="replace")

        entries = []
        for exp_file in domain_root.glob("harnesses/*/EXPERIENCE.md"):
            task_name = exp_file.parent.name
            harness_path = exp_file.parent / "harness"
            components = [
                str(p.relative_to(harness_path))
                for p in harness_path.glob("**/*")
                if p.is_file()
            ]
            entries.append(
                {
                    "task": task_name,
                    "experience": exp_file.read_text(encoding="utf-8", errors="replace"),
                    "components": components,
                    "harness_path": str(harness_path),
                }
            )

        libraries.append(
            {
                "path": str(domain_root),
                "index": index_content,
                "entries": entries,
            }
        )

    return {
        "domain": domain,
        "found_libraries": len(libraries),
        "libraries": libraries,
    }


def _detect_workspace_format(dest: Path) -> str:
    """Accurately detect the target agent framework convention."""
    if (dest / ".opencode").is_dir() or (dest / "opencode.json").is_file():
        return "opencode"
    if (dest / ".gemini").is_dir():
        return "agy"
    if (dest / ".agents").is_dir() or (dest / "AGENTS.md").is_file():
        return "codex"
    if (dest / ".claude").is_dir() or (dest / ".claude.json").is_file() or (dest / "CLAUDE.md").is_file():
        return "claude"
    return "opencode"


@server.tool()
def harness2_apply_harness(
    harness_path: str,
    target_workspace: str,
    format: str = "auto",
    dry_run: bool = True,
    backup: bool = True,
) -> Dict[str, Any]:
    """Export and apply a synthesized harness tree to a workspace with collision and backup safety.

    Concatenates always-on instructions (systemprompt.md, guardrails.md, LongTermMEMORY.md)
    into the client's single instruction file (AGENTS.md or CLAUDE.md) in a single pass,
    creating a timestamped backup before touching existing project rules.

    Args:
        harness_path: Absolute or relative path to the harness source directory.
        target_workspace: Root directory of the client project workspace.
        format: Target format ('opencode', 'agy', 'codex', 'claude', or 'auto').
        dry_run: When True (default), simulates the copying process without modifying any files.
        backup: When True, creates a timestamped .bak copy of original files.
    """
    src = Path(harness_path).expanduser().resolve()
    dest = Path(target_workspace).expanduser().resolve()

    if not src.is_dir():
        return {"status": "error", "message": f"Harness path does not exist: {src}"}
    if not dest.is_dir():
        return {"status": "error", "message": f"Target workspace does not exist: {dest}"}

    chosen_format = _detect_workspace_format(dest) if format == "auto" else format.lower()
    planned_operations: List[Dict[str, Any]] = []

    # Accumulate always-on instructions to avoid overwrite collision
    always_on_contents: Dict[str, str] = {}

    for root_dir, _, files in os.walk(src):
        for file in sorted(files):
            file_path = Path(root_dir) / file
            rel_path = file_path.relative_to(src)
            parts = rel_path.parts

            if rel_path.name in ("systemprompt.md", "guardrails.md", "LongTermMEMORY.md"):
                always_on_contents[rel_path.name] = file_path.read_text(encoding="utf-8", errors="replace")
                continue

            if "skills" in parts:
                skills_idx = parts.index("skills")
                subpath = Path(*parts[skills_idx + 1 :]) if len(parts) > skills_idx + 1 else Path(file)
                if chosen_format == "agy":
                    target_file = dest / ".gemini" / "skills" / subpath
                elif chosen_format == "codex":
                    target_file = dest / ".agents" / "skills" / subpath
                elif chosen_format == "claude":
                    target_file = dest / ".claude" / "skills" / subpath
                else:
                    target_file = dest / ".opencode" / "skills" / subpath
            elif "scripts" in parts:
                scripts_idx = parts.index("scripts")
                subpath = Path(*parts[scripts_idx + 1 :]) if len(parts) > scripts_idx + 1 else Path(file)
                target_file = dest / "scripts" / subpath
            else:
                target_file = dest / rel_path

            planned_operations.append(
                {
                    "source": str(rel_path),
                    "destination": str(target_file.relative_to(dest)),
                    "target_exists": target_file.is_file(),
                }
            )

    # Plan single combined instruction file
    combined_instruction_file = None
    if always_on_contents:
        if chosen_format == "agy":
            # agy supports multiple files in .gemini/rules/
            for name, content in always_on_contents.items():
                tgt = dest / ".gemini" / "rules" / f"{Path(name).stem}.md"
                planned_operations.append({
                    "source": name,
                    "destination": str(tgt.relative_to(dest)),
                    "target_exists": tgt.is_file(),
                })
        else:
            combined_instruction_file = (
                dest / "CLAUDE.md" if chosen_format == "claude" else dest / "AGENTS.md"
            )
            planned_operations.append({
                "source": "combined_instructions (" + ", ".join(always_on_contents.keys()) + ")",
                "destination": str(combined_instruction_file.relative_to(dest)),
                "target_exists": combined_instruction_file.is_file(),
            })

    if dry_run:
        return {
            "status": "dry_run",
            "format": chosen_format,
            "message": "Previewing operations. Set dry_run=False to apply.",
            "operations_count": len(planned_operations),
            "operations": planned_operations,
        }

    # Execute operations with backup
    executed_files: List[str] = []
    backups_created: List[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # 1. Write non-instruction files
    for op in planned_operations:
        if op["source"].startswith("combined_instructions"):
            continue
        if op["source"] in always_on_contents and chosen_format != "agy":
            continue

        tgt_file = dest / op["destination"]
        tgt_file.parent.mkdir(parents=True, exist_ok=True)
        if tgt_file.is_file() and backup:
            bak_path = tgt_file.with_name(f"{tgt_file.name}.{timestamp}.bak")
            shutil.copy2(tgt_file, bak_path)
            backups_created.append(str(bak_path.relative_to(dest)))

        src_file = src / op["source"]
        shutil.copy2(src_file, tgt_file)
        executed_files.append(op["destination"])

    # 2. Append/merge combined instructions cleanly
    if combined_instruction_file:
        combined_instruction_file.parent.mkdir(parents=True, exist_ok=True)
        existing_text = ""
        if combined_instruction_file.is_file():
            if backup:
                bak_path = combined_instruction_file.with_name(f"{combined_instruction_file.name}.{timestamp}.bak")
                shutil.copy2(combined_instruction_file, bak_path)
                backups_created.append(str(bak_path.relative_to(dest)))
            existing_text = combined_instruction_file.read_text(encoding="utf-8", errors="replace").strip() + "\n\n"

        new_sections = []
        for name in ("systemprompt.md", "guardrails.md", "LongTermMEMORY.md"):
            if name in always_on_contents and always_on_contents[name].strip():
                new_sections.append(f"<!-- [Harness2 {name}] -->\n" + always_on_contents[name].strip())

        full_content = existing_text + "\n\n".join(new_sections) + "\n"
        combined_instruction_file.write_text(full_content, encoding="utf-8")
        executed_files.append(str(combined_instruction_file.relative_to(dest)))

    return {
        "status": "applied",
        "format": chosen_format,
        "files_written": executed_files,
        "backups_created": backups_created,
    }


def _rank_key(c: Dict[str, Any]) -> tuple:
    review = c.get("review") or {}
    high = sum(1 for i in review.get("issues", []) if str(i.get("severity")).lower() == "high")
    score = review.get("score")
    return (
        1 if c.get("test_passed") is True else (0 if c.get("test_passed") is None else -1),
        1 if c.get("has_changes") else 0,
        -high,
        score if isinstance(score, (int, float)) else -1,
    )


def _candidate_summary(label: str, run_dir: Path) -> Dict[str, Any]:
    meta = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    diff = (run_dir / "diff.patch").read_text(encoding="utf-8")
    return {
        "label": label,
        "strategy": meta.get("strategy"),
        "test_passed": meta.get("test_passed"),
        "has_changes": bool(diff.strip()),
        "review": meta.get("review"),
        "claims": meta.get("claims", []),
        "errors": meta.get("errors", []),
        "worktree_path": (run_dir / "worktree_path.txt").read_text(encoding="utf-8"),
        "diff": diff,
        "verification_output": (run_dir / "test_output.txt").read_text(encoding="utf-8")[-4000:],
    }


@server.tool()
def harness2_solve(
    goal: str,
    workspace: str,
    verify_cmd: Optional[str] = None,
    domain: str = "general",
    k: int = 2,
    repair_rounds: int = 1,
    context_files: Optional[List[str]] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    """Generate k candidate solutions with the configured LLM, each in its own git worktree.

    Each candidate: LLM edits (distinct strategy) -> verify_cmd -> one repair round on failure
    -> independent LLM critique of the diff (numbers, citations, leakage, bugs). Candidates are
    ranked (tests passed > has changes > fewest high-severity issues > critic score) and a
    recommendation is ALWAYS returned with every candidate for review. Nothing touches the
    main working tree until harness2_finish(action='accept').

    verify_cmd runs via the shell inside the worktree with your environment. Existence/grep
    checks are reported as verification_strength='weak' and never count as proof.

    Args:
        goal: The task to accomplish.
        workspace: Path to the target git repository.
        verify_cmd: Command that exits 0 on success (e.g. 'pytest -q tests/test_x.py').
        domain: Category used for memory/lessons.
        k: Number of candidates (1-3; strategies: minimal, skeptical, thorough).
        repair_rounds: Extra LLM attempts per candidate after a failed verification.
        context_files: Repo-relative files the solver must read.
        timeout: Seconds for each verification run.
    """
    ws = Path(workspace).expanduser().resolve()
    if not (ws / ".git").exists():
        return {"status": "error", "message": f"{ws} is not a git repository."}
    from harness2 import llm
    from harness2.adapters import agent

    provider = llm.resolve_provider()
    adapter = GeneralTaskAdapter(repo_root=ws, goal=goal, domain=domain, verify_cmd=verify_cmd)
    sess_id = adapter.session_id
    strategies = ["minimal", "skeptical", "thorough"][: max(1, min(3, k))]

    candidates: List[Dict[str, Any]] = []
    for i, strat in enumerate(strategies, 1):
        label = f"cand{i}_{strat}"
        try:
            run_dir = adapter.execute(
                sess_id, None, label, timeout=timeout, strategy=strat,
                repair_rounds=repair_rounds, extra_files=context_files,
            )
            candidates.append(_candidate_summary(label, run_dir))
        except Exception as e:  # one failing candidate must not sink the session
            candidates.append({"label": label, "strategy": strat, "errors": [str(e)], "has_changes": False})

    ranked = sorted(candidates, key=_rank_key, reverse=True)
    best = ranked[0]
    (adapter.spec.runs_root / sess_id).mkdir(parents=True, exist_ok=True)
    (adapter.spec.runs_root / sess_id / "selection.json").write_text(
        json.dumps({"recommended": best["label"], "ranking": [c["label"] for c in ranked]}, indent=2),
        encoding="utf-8",
    )

    strength = agent.verification_strength(verify_cmd)
    warnings: List[str] = []
    if provider is None:
        warnings.append("No LLM configured (set INCEPTION_API_KEY in harness2/.env): no code was generated.")
    if strength != "executable":
        warnings.append(f"Verification is {strength}: a pass does not establish correctness.")
    high = [i for i in (best.get("review") or {}).get("issues", []) if str(i.get("severity")).lower() == "high"]
    if high:
        warnings.append(f"Critic found {len(high)} high-severity issue(s) in the recommended candidate.")
    if (best.get("review") or {}).get("unverified_claims"):
        warnings.append("Recommended candidate has unverified claims; check them before accepting.")
    if not best.get("has_changes"):
        warnings.append("Recommended candidate made no changes.")

    return {
        "status": "needs_human_review" if warnings else "ready_for_review",
        "session_id": sess_id,
        "llm": provider.public() if provider else None,
        "verification_strength": strength,
        "recommended": best["label"],
        "warnings": warnings,
        "candidates": ranked,
        "next": (
            f"harness2_finish(session_id='{sess_id}', workspace='{ws}', action='accept'|'discard', "
            f"rollout_label='<optional override>')"
        ),
    }


@server.tool()
def harness2_finish(
    session_id: str,
    workspace: str,
    action: str = "accept",
    rollout_label: Optional[str] = None,
    learned_facts: Optional[List[Dict[str, str]]] = None,
    keep_worktrees: bool = False,
    allow_destructive: bool = False,
) -> Dict[str, Any]:
    """Accept (apply one candidate's diff to the working tree, unstaged-safe) or discard a session.

    Applies the recommended candidate from harness2_solve unless rollout_label overrides it,
    records learned facts, runs self-evolution, and deletes the session's worktrees/branches.

    A candidate whose diff DELETES files is refused by default: applying it would remove
    files from the repository, and a verification command that passed does not make that
    safe. Re-run with allow_destructive=True to apply it anyway.

    Args:
        session_id: Session ID from harness2_solve.
        workspace: Path to the target git repository.
        action: 'accept' or 'discard'.
        rollout_label: Candidate label to apply instead of the recommendation.
        learned_facts: [{'category': ..., 'fact': ..., 'evidence': ...}] to store in memory.
        keep_worktrees: Keep worktrees for further inspection instead of deleting them.
        allow_destructive: Apply a patch that deletes files.
    """
    ws = Path(workspace).expanduser().resolve()
    db = MemoryDB(ws / ".harness2" / "memory.db")
    wt_root = ws / ".harness2" / "worktrees"
    wt_mgr = WorktreeManager(ws, base_temp_dir=wt_root if wt_root.is_dir() else None)
    session = db.get_session(session_id)
    if not session:
        return {"status": "error", "message": f"Session {session_id} not found."}
    if action not in ("accept", "discard"):
        return {"status": "error", "message": "action must be 'accept' or 'discard'."}

    chosen = None
    applied = False
    apply_result: Dict[str, Any] = {}
    evolution_result: Dict[str, Any] = {"status": "skipped"}
    if action == "accept":
        labels = [r["label"] for r in session.get("rollouts", [])]
        sel_file = ws / ".harness2" / "runs" / session_id / "selection.json"
        if rollout_label:
            chosen = rollout_label
        elif sel_file.is_file():
            chosen = json.loads(sel_file.read_text(encoding="utf-8")).get("recommended")
        else:
            passed = [r for r in session.get("rollouts", []) if r.get("test_passed") == 1]
            chosen = (passed or session.get("rollouts", [{}]))[0].get("label")
        if chosen not in labels:
            return {"status": "error", "message": f"Unknown rollout {chosen!r}; choose from {labels}."}
        try:
            apply_result = wt_mgr.apply_to_repo(
                session_id, chosen, allow_destructive=allow_destructive
            )
        except Exception as e:
            return {"status": "error", "message": f"Apply failed, nothing cleaned up: {e}"}
        applied = bool(apply_result.get("applied"))
        if not applied:
            # Nothing was applied, so the session stays open and the worktrees
            # stay where they are: the operator may want to look, or to apply
            # with allow_destructive.
            return {
                "status": "not_applied",
                "action": action,
                "chosen_rollout": chosen,
                "applied_to_repository": False,
                "reason": apply_result.get("reason", "the candidate could not be applied"),
                "stats": apply_result.get("stats", {}),
                "worktrees_kept": True,
                "next": (
                    f"harness2_finish(session_id='{session_id}', workspace='{ws}', "
                    f"rollout_label='{chosen}', allow_destructive=True) to apply it anyway"
                ),
            }
        for item in learned_facts or []:
            if item.get("category") and item.get("fact"):
                db.add_longterm_fact(
                    item["category"], item["fact"], float(item.get("confidence", 1.0)), item.get("evidence")
                )
        evolution_result = SelfEvolutionEngine(ws, db=db).analyze_session(session_id)
        db.close_session(session_id, status="accepted", best_rollout_id=chosen)
    else:
        db.close_session(session_id, status="discarded")

    cleaned = [] if keep_worktrees else wt_mgr.cleanup_session(session_id)
    return {
        "status": "finished",
        "action": action,
        "chosen_rollout": chosen,
        "applied_to_repository": applied,
        "applied_stats": apply_result.get("stats", {}),
        "deleted_worktrees": cleaned,
        "self_evolution": evolution_result,
        "note": "Changes are applied to the working tree only; review `git diff` and commit yourself.",
    }


@server.tool()
def harness2_config_status(probe: bool = False) -> Dict[str, Any]:
    """Report which LLM provider/model the MCP server will use, and whether it is reachable.

    The provider is resolved locally and reported without a model call. Pass
    probe=True to spend one tiny call and confirm the endpoint answers -- a
    status check that costs money and can block for three minutes is a status
    check nobody runs.

    Args:
        probe: Make one live call to confirm the endpoint answers.
    """
    from harness2 import llm

    p = llm.resolve_provider()
    if p is None:
        return {
            "configured": False,
            "hint": (
                "Set INCEPTION_API_KEY in harness2/.env, or HARNESS2_LLM_BASE_URL, "
                "HARNESS2_LLM_API_KEY and HARNESS2_LLM_MODEL"
            ),
            "probed": False,
        }
    out: Dict[str, Any] = {"configured": True, **p.public(), "probed": False}
    if not probe:
        return out
    try:
        reply = llm.chat([{"role": "user", "content": "Reply with: OK"}], max_tokens=512, retries=1)
        out["reachable"] = True
        out["sample"] = reply.strip()[:40]
    except llm.LLMError as e:
        out["reachable"] = False
        out["error"] = str(e)[:400]
    out["probed"] = True
    return out


@server.tool()
def harness2_list_sessions(workspace: str, status: Optional[str] = None) -> Dict[str, Any]:
    """List Harness² sessions in a workspace (optionally filter by status: active/accepted/discarded)."""
    ws = Path(workspace).expanduser().resolve()
    db_file = ws / ".harness2" / "memory.db"
    if not db_file.is_file():
        return {"sessions": []}
    import sqlite3

    con = sqlite3.connect(str(db_file), timeout=30.0)
    try:
        con.row_factory = sqlite3.Row
        q = "SELECT session_id, goal, domain, status, best_rollout_id, created_at, closed_at FROM sessions"
        rows = con.execute(
            q + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC",
            (status,) if status else (),
        ).fetchall()
    finally:
        con.close()
    return {"sessions": [dict(r) for r in rows]}


@server.tool()
def harness2_query_memory(
    workspace: str,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Query persistent SQLite long-term memory and recent sessions for a workspace.

    Args:
        workspace: Path to the git repository workspace.
        category: Optional category filter (e.g. 'architecture', 'conventions', 'quirks').
    """
    ws = Path(workspace).expanduser().resolve()
    db_file = ws / ".harness2" / "memory.db"
    if not db_file.is_file():
        return {"status": "empty", "message": "No SQLite memory database found in this workspace."}

    db = MemoryDB(db_file)
    facts = db.query_longterm_memory(category)
    return {
        "workspace": str(ws),
        "total_facts": len(facts),
        "facts": facts,
        "markdown_summary": db.render_longterm_memory_markdown(),
    }



@server.tool()
def harness2_recursion(
    goal: str,
    workspace: str,
    k: int = 1,
    mode: str = "parallel",
    domain: str = "general",
    verify_cmd: Optional[str] = None,
    timeout: int = 1800,
    allow_mcp: bool = False,
    sandbox: bool = True,
    channel: str = "http",
    improver_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the real Propose-Probe-Compose loop on a repository and goal.

    This is Harness² proper, not a scripted stand-in: an improver model
    proposes harness edits (instructions, guardrails, skills, scripts), each
    candidate is probed by running OpenCode in an isolated git worktree, the
    best contrasts are composed into a refined harness, and a selector model
    picks the winner by reading the candidates -- no ground-truth verifier
    required. The verified result is left in an isolated worktree for review;
    nothing is applied to the repository unless you call harness2_finish with
    action='accept'.

    Costs real model calls: k=1 in one mode is roughly 4 rollouts plus the
    proposal and selection sessions. Start with k=1.

    Args:
        goal: What the agent should accomplish in the repository.
        workspace: Path to the git repository to work in.
        k: Task-level recursion budget (1 = one propose/probe/compose round).
        mode: 'parallel' (wide) or 'sequential' (deep).
        domain: Domain label; entries are filed under it in the experience library.
        verify_cmd: Optional verification command; a rollout that passes it is
            preferred when selecting.
        timeout: Per-rollout timeout in seconds.
        sandbox: Run each rollout in a filesystem sandbox, so a shell command
            cannot write outside the workspace or read your home directory.
            On by default; needs bwrap on the machine.
        allow_mcp: Let the rollout use the MCP servers configured in your
            OpenCode. Off by default: they may hold credentials, and they are
            a route out of the workspace that a path fence does not close.
        channel: 'http' (default) runs the improver and the selector on the
            same model service as the solver, so one API key runs the whole
            loop. 'opencode' runs them through the OpenCode CLI, which needs
            HARNESS2_IMPROVER_MODEL set to a provider-prefixed id.
        improver_model: Override the improver/selector model.
    """
    ws = Path(workspace).expanduser().resolve()
    if not (ws / ".git").exists():
        return {"status": "error", "message": f"{ws} is not a git repository."}
    if k < 1:
        return {"status": "error", "message": "--k must be positive."}
    if mode not in ("parallel", "sequential"):
        return {"status": "error", "message": "mode must be 'parallel' or 'sequential'."}
    if channel not in ("http", "opencode"):
        return {"status": "error", "message": "channel must be 'http' or 'opencode'."}

    from harness2 import config, llm
    from harness2.adapters.general import GeneralTaskAdapter
    from harness2.core.proposer import Proposer
    from harness2.core.schedule import mode_token, run_domain
    from harness2.core.spec import resolve_channel_model

    try:
        # The channel and the model are resolved INSIDE the try: a model id
        # without a provider prefix used to raise out of the tool instead of
        # coming back as an error the caller can act on.
        if channel == "http":
            channel_spec = resolve_channel_model("http", improver_model or None)
        else:
            model = (improver_model or config.IMPROVER_MODEL).strip()
            if not model:
                return {
                    "status": "error",
                    "message": (
                        "no improver model: set HARNESS2_IMPROVER_MODEL, or use "
                        "channel='http' to run the improver on the same endpoint as "
                        "the solver"
                    ),
                }
            channel_spec = resolve_channel_model("opencode", model)
        proposer = Proposer(channel_spec)
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    solver_model = config.SOLVER_MODEL_OC.strip()
    if channel == "http" and llm.resolve_provider() is None:
        return {
            "status": "error",
            "message": (
                "no model service configured: set INCEPTION_API_KEY in harness2/.env, "
                "or HARNESS2_LLM_BASE_URL, HARNESS2_LLM_API_KEY and HARNESS2_LLM_MODEL"
            ),
        }

    modes = (mode_token(mode),)
    try:
        adapter = GeneralTaskAdapter(
            repo_root=ws,
            goal=goal,
            domain=domain,
            verify_cmd=verify_cmd,
            model=solver_model,
            selector_model=channel_spec.model,
            channel=channel,
            timeout=timeout,
            allow_mcp=allow_mcp,
            sandbox=sandbox,
        )
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    library_root = adapter.harness_dir / "library" / f"k{k}"

    try:
        result = run_domain(
            domain,
            adapter.tasks(),
            k,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            library_root=library_root,
            modes=modes,
            # No judge behind this path: the reference executions belong in
            # the pool so a regressed composition can lose to the base.
            pool_policy="finals+base",
        )
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    report = adapter.harness_dir / f"general_{mode}_k{k}.json"
    report.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")

    return {
        "status": "done" if not result.get("failed") else "partial",
        "done": result.get("done", 0),
        "failed": result.get("failed", 0),
        "tasks": result.get("tasks", []),
        "runs_root": str(adapter.spec.runs_root),
        "library_root": str(library_root),
        "report": str(report),
    }



@server.tool()
def harness2_memory_status(
    workspace: str,
    prune: bool = False,
    keep_evidence_runs: int = 3,
    keep_transcript_runs: int = 10,
    keep_runs: int = 50,
) -> Dict[str, Any]:
    """Report, and optionally prune, the harness's memory for a workspace.

    The harness keeps three kinds of memory under `<workspace>/.harness2/`:
    the run artifacts (transcripts and evidence trees), the SQLite record
    (sessions, rollouts, facts, lessons), and the experience library (one
    entry per task). The first grows fastest -- an evidence tree is copied
    again by the selection and reflection stages -- so it is the one that
    needs a policy.

    With prune=false this only reports. With prune=true it applies the
    retention policy: the recent runs keep their evidence and their
    transcripts, older runs keep their conclusions and lose the rest, and
    library entries a newer entry supersedes are retired.

    Args:
        workspace: The repository the harness has been working in.
        prune: Apply the policy rather than only reporting.
        keep_evidence_runs: How many recent runs keep their evidence trees.
        keep_transcript_runs: How many recent runs keep their transcripts.
        keep_runs: How many runs are kept at all.
    """
    ws = Path(workspace).expanduser().resolve()
    harness_dir = ws / ".harness2"
    if not harness_dir.is_dir():
        return {"status": "error", "message": f"{harness_dir} does not exist."}

    from harness2.core.retention import RetentionPolicy, prune_runs

    runs_root = harness_dir / "runs"
    policy = RetentionPolicy(
        keep_evidence_runs=keep_evidence_runs,
        keep_transcript_runs=keep_transcript_runs,
        keep_runs=keep_runs,
    )

    def size_of(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

    before = {
        "runs": size_of(runs_root),
        "library": size_of(harness_dir / "library"),
        "database": size_of(harness_dir / "memory.db"),
    }
    pruned: Dict[str, int] = {}
    if prune:
        pruned = prune_runs(runs_root, policy)
    after = {
        "runs": size_of(runs_root),
        "library": size_of(harness_dir / "library"),
        "database": size_of(harness_dir / "memory.db"),
    }
    return {
        "status": "ok",
        "workspace": str(ws),
        "policy": policy.describe(),
        "before_bytes": before,
        "after_bytes": after,
        "pruned": pruned,
        "saved_bytes": sum(before.values()) - sum(after.values()),
    }


@server.tool()
def harness2_benchmark_status(
    bench: str = "jb",
    substrate: str = "oc",
) -> Dict[str, Any]:
    """Check installation status and task readiness of a benchmark (jb, lab, wb).

    Args:
        bench: Benchmark identifier ('jb', 'lab', 'wb').
        substrate: Substrate executor ('oc' or 'cx').
    """
    cmd = [
        sys.executable,
        "-m",
        "harness2.cli.check",
        "--bench",
        bench,
        "--substrate",
        substrate,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "bench": bench,
        "substrate": substrate,
        "ready": proc.returncode == 0,
        "output": (proc.stdout + "\n" + proc.stderr).strip(),
    }


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
