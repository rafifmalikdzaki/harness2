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

"""Self-Evolution Engine for Harness².

Analyzes execution trajectories, contrastive diffs, and verification results across
sessions to automatically synthesize new reusable skills (SKILL.md), scripts,
and distilled long-term memory facts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .memory_db import MemoryDB


def _changed_files(diff: str) -> List[str]:
    """The paths a unified diff touches, in order, without duplicates."""
    out: List[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ b/"):
            continue
        rel = line[6:].strip()
        if rel in ("/dev/null", ""):
            continue
        if rel not in out:
            out.append(rel)
    return out


def _contrast_lesson(
    goal: str,
    winner: Dict[str, Any],
    loser: Dict[str, Any],
    winner_files: List[str],
    loser_files: List[str],
) -> str:
    """A lesson that says what the winning rollout did differently."""
    win = ", ".join(f"`{f}`" for f in winner_files[:5]) or "(no file changes recorded)"
    lose = ", ".join(f"`{f}`" for f in loser_files[:5]) or "(no file changes recorded)"
    return (
        f"Rollout `{winner.get('label', '?')}` passed verification while "
        f"`{loser.get('label', '?')}` did not. The winning rollout changed {win}; "
        f"the failing one changed {lose}. For goals like this one, start from the "
        f"files the winning rollout touched."
    )


class SelfEvolutionEngine:
    """Extracts procedural lessons, auto-synthesizes skills, and updates project memory."""

    def __init__(self, workspace_root: Path | str, db: Optional[MemoryDB] = None):
        self.workspace = Path(workspace_root).expanduser().resolve()
        self.harness_dir = self.workspace / ".harness2"
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or MemoryDB(self.harness_dir / "memory.db")

    def analyze_session(self, session_id: str) -> Dict[str, Any]:
        """Examine session rollouts, verification outputs, and errors to extract actionable evolutions."""
        session = self.db.get_session(session_id)
        if not session:
            return {"status": "error", "message": f"Session {session_id} not found."}

        goal = session.get("goal", "")
        domain = session.get("domain", "general")
        rollouts = session.get("rollouts", [])

        evolutions: List[Dict[str, Any]] = []
        synthesized_facts: List[Dict[str, Any]] = []
        synthesized_skills: List[Dict[str, str]] = []

        # Find successful rollouts vs failed rollouts
        successful = [r for r in rollouts if r.get("test_passed") == 1]
        failed = [r for r in rollouts if r.get("test_passed") == 0]

        # 1. Self-Evolution from Test Verifications & Contrasts
        if successful and failed:
            succ_files = _changed_files(successful[0].get("git_diff", ""))
            fail_files = _changed_files(failed[0].get("git_diff", ""))
            lesson_cue = f"When solving: {goal[:80]}"
            lesson_body = _contrast_lesson(
                goal,
                successful[0],
                failed[0],
                succ_files,
                fail_files,
            )

            self.db.record_lesson(
                domain=domain,
                cue=lesson_cue,
                lesson=lesson_body,
                evidence=f"Verification passed with {successful[0]['label']}",
                session_id=session_id,
            )
            evolutions.append({
                "type": "contrastive_lesson",
                "cue": lesson_cue,
                "lesson": lesson_body,
            })

        # 2. Extract Architectural / Tech Stack Facts from Goal & Outputs
        extracted_facts = self._extract_facts_from_context(goal, rollouts)
        for cat, fact, conf, ev in extracted_facts:
            self.db.add_longterm_fact(cat, fact, confidence=conf, evidence=ev)
            synthesized_facts.append({"category": cat, "fact": fact})

        # 3. Check for Reusable Procedural Skills
        # If the task solved a recurrent pattern (e.g. database migration, test fixture, auth setup),
        # draft a SKILL.md in the project skill store
        skill_candidate = self._generate_skill_candidate(goal, domain, successful)
        if skill_candidate:
            skill_dir = self.workspace / ".harness2" / "skills" / skill_candidate["name"]
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(skill_candidate["content"], encoding="utf-8")
            synthesized_skills.append(skill_candidate)

        return {
            "status": "evolved",
            "session_id": session_id,
            "domain": domain,
            "evolutions": evolutions,
            "synthesized_facts": synthesized_facts,
            "synthesized_skills": synthesized_skills,
        }

    def _extract_facts_from_context(self, goal: str, rollouts: List[Dict[str, Any]]) -> List[tuple]:
        facts = []
        combined_text = goal + "\n" + "\n".join(r.get("test_output", "") for r in rollouts)
        lower = combined_text.lower()

        # Pattern detection: frameworks & test runners
        if "pytest" in lower:
            facts.append(("testing", "Project uses pytest for automated verification", 1.0, "goal/test output"))
        if "jest" in lower or "npm test" in lower:
            facts.append(("testing", "Project uses Node/Jest for automated verification", 1.0, "goal/test output"))
        if "fastapi" in lower:
            facts.append(("architecture", "Backend framework includes FastAPI", 0.9, "task context"))
        if "sqlite" in lower:
            facts.append(("database", "Project incorporates SQLite database storage", 1.0, "context"))
        if "docker" in lower:
            facts.append(("devops", "Project supports Docker containerization", 1.0, "context"))

        # What the successful rollouts actually touched: a fact about the
        # repository's shape, not about the toolchain.
        touched: set[str] = set()
        for r in rollouts:
            if r.get("test_passed") == 1:
                touched.update(_changed_files(r.get("git_diff", "")))
        by_dir: dict[str, int] = {}
        for rel in touched:
            top = rel.split("/", 1)[0]
            if top and top not in (".", ".."):
                by_dir[top] = by_dir.get(top, 0) + 1
        for top, count in sorted(by_dir.items(), key=lambda kv: -kv[1])[:3]:
            facts.append(
                (
                    "codebase",
                    f"Successful work in this project touches {top}/ ({count} file(s) changed)",
                    0.8,
                    "winning rollout diffs",
                )
            )

        return facts

    def _generate_skill_candidate(self, goal: str, domain: str, successful_rollouts: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not successful_rollouts:
            return None
        
        # Simple slug generation
        clean_name = re.sub(r"[^a-zA-Z0-9]+", "-", goal.lower()[:30]).strip("-")
        if not clean_name:
            clean_name = f"skill-{domain}"

        label = successful_rollouts[0].get("label", "probe")
        files = _changed_files(successful_rollouts[0].get("git_diff", ""))
        touched = ", ".join(f"`{f}`" for f in files[:8]) or "(no file changes recorded)"
        procedure = (
            "\n".join(f"{i}. Edit {rel}." for i, rel in enumerate(files[:8], 1))
            or "1. Edit the files the goal names."
        )
        skill_content = f"""---
name: {clean_name}
description: Procedural skill synthesized from a verified resolution of: {goal}
---

# Skill: {clean_name.replace('-', ' ').title()}

## Trigger Cue
When working on tasks related to:
> {goal}

## Verified Procedure
The rollout `{label}` passed its verification command. It changed:
{touched}

{procedure}

3. Re-run the verification command and confirm it passes before finishing.
"""
        return {"name": clean_name, "content": skill_content}
