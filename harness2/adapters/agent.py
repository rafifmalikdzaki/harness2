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

"""LLM solver + independent critic for the general-task path.

The solver proposes whole-file edits as JSON; edits are applied only inside an isolated
worktree. The critic reviews the resulting diff against the goal with no access to the
solver's reasoning, and must list concrete issues and unverified claims. Passing a weak
verification command is never treated as proof of correctness.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import llm

MAX_CONTEXT_CHARS = 60_000
MAX_FILE_CHARS = 12_000

# The harness travels in the prompt, so it needs a budget of its own: the
# always-on prose competes with the goal, and a harness nobody can read is
# not a harness.
MAX_HARNESS_CHARS = 24_000
MAX_HARNESS_DOC_CHARS = 4_000

STRATEGIES = {
    "minimal": "Make the smallest correct change that fully achieves the goal.",
    "thorough": "Achieve the goal robustly: handle edge cases and add/adjust tests where sensible.",
    "skeptical": (
        "Before editing, re-derive every number, citation, and factual claim you rely on from "
        "the repository itself; do not invent figures. Prefer stating uncertainty over guessing."
    ),
}

SOLVER_SYSTEM = """You are a careful software/research engineer working in a git worktree.
The HARNESS section, when it is present, is the operating instructions for this run:
follow its rules and its procedures unless the Goal contradicts them.
Return ONLY a JSON object:
{"plan": "<short plan>",
 "edits": [{"path": "<repo-relative path>", "action": "write"|"delete", "content": "<full new file content>"}],
 "claims": ["<each factual/numeric claim your change asserts, with where in the repo it is supported>"]}
Rules: full file contents for every write; never touch .git; keep unrelated code unchanged;
do not fabricate numbers, citations, or results; do not use held-out/test data for tuning or
'final confirmation' unless the goal explicitly says so."""

CRITIC_SYSTEM = """You are an independent, strict reviewer. You did not write this change.
Given a goal, a diff, the verification output and the author's claims, find real problems:
wrong numbers or citations, claims not supported by the repository, methodological errors
(e.g. data leakage, evaluating on a repeatedly-used test set), bugs, and goal not met.
Return ONLY JSON:
{"score": <0-10>, "goal_met": true|false,
 "issues": [{"severity": "high"|"medium"|"low", "description": "..."}],
 "unverified_claims": ["..."]}"""

_WEAK_VERIFY = re.compile(r"^\s*(test\s+-[fe]|\[\s+-[fe]|ls\b|cat\b|grep\b|true\b|echo\b)")


def verification_strength(verify_cmd: Optional[str]) -> str:
    """'none' | 'weak' (existence/string checks only) | 'executable'."""
    if not verify_cmd or not verify_cmd.strip():
        return "none"
    parts = re.split(r"&&|\|\||;", verify_cmd)
    if all(_WEAK_VERIFY.match(p) for p in parts if p.strip()):
        return "weak"
    return "executable"


def _git_files(root: Path) -> List[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f and not f.startswith((".harness2/", ".harness_staged/"))]


def gather_context(root: Path, goal: str, extra_files: Optional[List[str]] = None) -> str:
    files = _git_files(root)
    parts = ["# Repository files\n" + "\n".join(files[:500])]
    budget = MAX_CONTEXT_CHARS - len(parts[0])
    wanted: List[str] = []
    for f in (extra_files or []):
        if f in files:
            wanted.append(f)
    low = goal.lower()
    for f in files:
        if f in wanted:
            continue
        if f.lower() in low or Path(f).name.lower() in low:
            wanted.append(f)
    for f in ("README.md", "AGENTS.md", "CLAUDE.md", "pyproject.toml", "package.json"):
        if f in files and f not in wanted:
            wanted.append(f)
    for f in wanted:
        try:
            body = (root / f).read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            continue
        chunk = f"\n# File: {f}\n```\n{body}\n```\n"
        if len(chunk) > budget:
            break
        parts.append(chunk)
        budget -= len(chunk)
    return "".join(parts)


def _safe_target(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if root.resolve() not in p.parents or ".git" in Path(rel).parts:
        raise ValueError(f"unsafe edit path: {rel!r}")
    return p


def apply_edits(root: Path, edits: List[Dict[str, Any]]) -> List[str]:
    touched: List[str] = []
    for e in edits:
        rel = str(e.get("path", "")).strip()
        if not rel:
            continue
        target = _safe_target(root, rel)
        if e.get("action") == "delete":
            if target.is_file():
                target.unlink()
                touched.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(e.get("content", "")), encoding="utf-8")
        touched.append(rel)
    return touched


def solve(
    root: Path,
    goal: str,
    strategy: str,
    memory_md: str = "",
    feedback: str = "",
    extra_files: Optional[List[str]] = None,
    harness: str = "",
) -> Dict[str, Any]:
    """Solve the goal in `root` and return the plan, the claims and the files touched.

    `harness` is the improver's proposal, rendered as text. It is placed BEFORE the
    goal so it reads as the operating instructions for this run -- this is the only
    channel by which a proposed harness reaches the solver, and without it every
    candidate runs the same instructions and the candidates differ in nothing but
    luck.
    """
    ctx = gather_context(root, goal, extra_files)
    parts: List[str] = []
    if harness.strip():
        parts.append(harness.strip())
    parts.append(f"# Goal\n{goal}")
    parts.append(f"# Strategy\n{STRATEGIES.get(strategy, STRATEGIES['minimal'])}")
    if memory_md.strip():
        parts.append(f"# Project memory\n{memory_md}")
    if feedback:
        parts.append(f"# Feedback from previous attempt\n{feedback}")
    parts.append(ctx)
    user = "\n\n".join(parts)
    reply = llm.chat(
        [{"role": "system", "content": SOLVER_SYSTEM}, {"role": "user", "content": user}],
        temperature={"minimal": 0.1, "thorough": 0.4, "skeptical": 0.2}.get(strategy, 0.2),
        max_tokens=16000,
    )
    data = llm.extract_json(reply)
    touched = apply_edits(root, data.get("edits") or [])
    return {"plan": data.get("plan", ""), "claims": data.get("claims") or [], "touched": touched}


def critique(goal: str, diff: str, verify_output: str, claims: List[str], strength: str) -> Dict[str, Any]:
    user = (
        f"# Goal\n{goal}\n\n# Verification strength: {strength}\n"
        f"# Verification output\n{verify_output[-6000:]}\n\n"
        f"# Author claims\n" + "\n".join(f"- {c}" for c in claims) + f"\n\n# Diff\n{diff[:40000]}"
    )
    try:
        data = llm.extract_json(
            llm.chat(
                [{"role": "system", "content": CRITIC_SYSTEM}, {"role": "user", "content": user}],
                temperature=0.0,
                max_tokens=6000,
            )
        )
    except (llm.LLMError, ValueError) as e:
        return {"score": None, "goal_met": None, "issues": [], "unverified_claims": [], "error": str(e)}
    data.setdefault("issues", [])
    data.setdefault("unverified_claims", [])
    try:
        data["score"] = float(data.get("score"))
    except (TypeError, ValueError):
        data["score"] = None
    return data
