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

Exposes Harness² recursive harnessing capabilities, demo runner, and domain
experience library inspection to Claude Code, Codex, OpenCode, and Antigravity (agy).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from harness2 import config

server = MCPServer(
    name="harness2",
    instructions=(
        "Harness² MCP Server: Automates recursive agent harnessing (Propose-Probe-Compose) "
        "and manages persistent domain experience libraries (skills, scripts, guardrails)."
    ),
    version="0.1.0",
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


@server.tool()
def harness2_apply_harness(
    harness_path: str,
    target_workspace: str,
    format: str = "auto",
) -> Dict[str, Any]:
    """Export and apply a synthesized harness tree to a workspace for Codex, OpenCode, or Antigravity (agy).

    Copies synthesized skills, scripts, and instructions into target client conventions:
    - agy: writes to .gemini/skills/ and scripts/
    - codex: writes to AGENTS.md, .agents/skills/, and scripts/
    - opencode: writes to systemprompt.md, .opencode/skills/, and scripts/

    Args:
        harness_path: Absolute or relative path to the harness source directory.
        target_workspace: Root directory of the client project workspace.
        format: Target format ('agy', 'codex', 'opencode', or 'auto').
    """
    src = Path(harness_path).expanduser().resolve()
    dest = Path(target_workspace).expanduser().resolve()

    if not src.is_dir():
        return {"status": "error", "message": f"Harness path does not exist: {src}"}
    if not dest.is_dir():
        return {"status": "error", "message": f"Target workspace does not exist: {dest}"}

    copied_files: List[str] = []

    # Detect format if auto
    chosen_format = format
    if format == "auto":
        if (dest / ".gemini").is_dir():
            chosen_format = "agy"
        elif (dest / ".agents").is_dir() or (dest / "AGENTS.md").is_file():
            chosen_format = "codex"
        else:
            chosen_format = "agy"  # default to agy if ambiguous

    for root_dir, _, files in os.walk(src):
        for file in files:
            file_path = Path(root_dir) / file
            rel_path = file_path.relative_to(src)

            # Determine destination based on format
            if chosen_format == "agy":
                if rel_path.name.endswith(".py") and "scripts" in rel_path.parts:
                    target_file = dest / "scripts" / rel_path.name
                elif "skills" in rel_path.parts:
                    target_file = dest / ".gemini" / "skills" / rel_path.name
                elif rel_path.name in ("systemprompt.md", "AGENTS.md", "guardrails.md"):
                    target_file = dest / ".gemini" / "rules" / f"{rel_path.stem}.md"
                else:
                    target_file = dest / rel_path
            elif chosen_format == "codex":
                if "skills" in rel_path.parts:
                    target_file = dest / ".agents" / "skills" / rel_path.name
                elif rel_path.name in ("systemprompt.md", "AGENTS.md"):
                    target_file = dest / "AGENTS.md"
                else:
                    target_file = dest / rel_path
            else:  # opencode
                target_file = dest / rel_path

            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target_file)
            copied_files.append(str(target_file.relative_to(dest)))

    return {
        "status": "success",
        "format": chosen_format,
        "copied_files_count": len(copied_files),
        "files": copied_files,
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
