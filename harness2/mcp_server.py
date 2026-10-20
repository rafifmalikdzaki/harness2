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
    version="0.4.0",
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
    proc = subprocess.run(cmd, capture_output=True, text=True)
    report_file = out / "demo.json"
    result: Dict[str, Any] = {
        "status": "success" if proc.returncode == 0 else "failed",
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }
    if report_file.is_file():
        try:
            result["report"] = json.loads(report_file.read_text(encoding="utf-8"))
        except Exception:
            pass
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


@server.tool()
def harness2_solve(
    goal: str,
    workspace: str,
    verify_cmd: Optional[str] = None,
    domain: str = "general",
) -> Dict[str, Any]:
    """Initiate a general task run inside an isolated git worktree without altering main.

    Creates a session record in SQLite (.harness2/memory.db) and executes candidate rollouts
    in isolated git worktrees. The worktrees stay intact for inspection until harness2_finish is called.

    Args:
        goal: The task goal or problem to solve.
        workspace: Path to the target git repository.
        verify_cmd: Optional verification command to test changes (e.g. 'pytest', 'npm test').
        domain: Domain or project category (default: 'general').
    """
    ws = Path(workspace).expanduser().resolve()
    if not (ws / ".git").exists():
        return {"status": "error", "message": f"{ws} is not a git repository."}

    adapter = GeneralTaskAdapter(repo_root=ws, goal=goal, domain=domain, verify_cmd=verify_cmd)
    sess_id = adapter.session_id

    run_dir = adapter.execute(task_id=sess_id, harness_dir=None, run_id="probe_1")

    diff_text = adapter.deliverable_text(run_dir)
    test_out = (run_dir / "test_output.txt").read_text(encoding="utf-8")
    wt_path = (run_dir / "worktree_path.txt").read_text(encoding="utf-8")

    return {
        "status": "ready_for_review",
        "session_id": sess_id,
        "worktree_path": wt_path,
        "git_diff": diff_text,
        "verification_output": test_out,
        "instructions": (
            f"Review changes in worktree '{wt_path}'. "
            f"When finished, call harness2_finish(session_id='{sess_id}', workspace='{workspace}', action='accept'|'discard') "
            "to clean up the worktree."
        ),
    }


@server.tool()
def harness2_finish(
    session_id: str,
    workspace: str,
    action: str = "accept",
    learned_facts: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Finalize a session, apply or discard changes, run self-evolution, and delete worktrees.

    Selects the best rollout (prioritizing test_passed=1), applies genuine code changes without
    leaking internal scaffolding, triggers the self-evolution engine, and prunes the worktree.

    Args:
        session_id: The session ID returned by harness2_solve.
        workspace: Path to the target git repository.
        action: 'accept' (applies diff to repository and records memory) or 'discard' (cleans up without changes).
        learned_facts: Optional list of facts to record into SQLite long-term memory.
    """
    ws = Path(workspace).expanduser().resolve()
    db = MemoryDB(ws / ".harness2" / "memory.db")
    wt_mgr = WorktreeManager(ws)

    session = db.get_session(session_id)
    if not session:
        return {"status": "error", "message": f"Session {session_id} not found."}

    applied = False
    chosen_label = None

    if action == "accept":
        rollouts = session.get("rollouts", [])
        if rollouts:
            # Select the best rollout: first prioritize test_passed == 1
            passed_rollouts = [r for r in rollouts if r.get("test_passed") == 1]
            chosen_rollout = passed_rollouts[0] if passed_rollouts else rollouts[-1]
            chosen_label = chosen_rollout["label"]

            wt_mgr.apply_to_repo(session_id, chosen_label)
            applied = True

        # Store explicit user/agent learned facts
        if learned_facts:
            for item in learned_facts:
                if "category" in item and "fact" in item:
                    db.add_longterm_fact(
                        category=item["category"],
                        fact=item["fact"],
                        confidence=float(item.get("confidence", 1.0)),
                        evidence=item.get("evidence"),
                    )

        # Trigger Self-Evolution Engine: automatically synthesize lessons, facts, and skills
        evolution_engine = SelfEvolutionEngine(ws, db=db)
        evolution_result = evolution_engine.analyze_session(session_id)

        db.close_session(session_id, status="accepted", best_rollout_id=chosen_label)
    else:
        db.close_session(session_id, status="discarded")
        evolution_result = {"status": "skipped", "message": "Session discarded"}

    # Prune and delete all worktrees associated with this session
    cleaned_worktrees = wt_mgr.cleanup_session(session_id)

    return {
        "status": "finished",
        "action": action,
        "chosen_rollout": chosen_label,
        "applied_to_repository": applied,
        "deleted_worktrees": cleaned_worktrees,
        "self_evolution": evolution_result,
    }


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
    """
    ws = Path(workspace).expanduser().resolve()
    if not (ws / ".git").exists():
        return {"status": "error", "message": f"{ws} is not a git repository."}
    if k < 1:
        return {"status": "error", "message": "--k must be positive."}
    if mode not in ("parallel", "sequential"):
        return {"status": "error", "message": "mode must be 'parallel' or 'sequential'."}

    from harness2 import config
    from harness2.adapters.general import GeneralTaskAdapter
    from harness2.core.proposer import Proposer
    from harness2.core.schedule import mode_token, run_domain
    from harness2.core.spec import resolve_channel_model

    solver_model = config.SOLVER_MODEL_OC.strip()
    improver_model = config.IMPROVER_MODEL.strip()
    if not solver_model or not improver_model:
        return {
            "status": "error",
            "message": (
                "no models configured: set HARNESS2_SOLVER_MODEL and "
                "HARNESS2_IMPROVER_MODEL (the opencode channel needs a "
                "provider-prefixed id, e.g. anthropic/claude-sonnet-4-5)."
            ),
        }

    modes = (mode_token(mode),)
    adapter = GeneralTaskAdapter(
        repo_root=ws,
        goal=goal,
        domain=domain,
        verify_cmd=verify_cmd,
        model=solver_model,
        selector_model=improver_model,
        timeout=timeout,
        allow_mcp=allow_mcp,
        sandbox=sandbox,
    )
    proposer = Proposer(resolve_channel_model("opencode", improver_model))
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
