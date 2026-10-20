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
from .tree import SurfaceSpec


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
            succ_diff = successful[0].get("git_diff", "")
            fail_diff = failed[0].get("git_diff", "")
            lesson_cue = f"When solving: {goal[:80]}"
            lesson_body = f"Adopt contrastive changes observed in winning rollout {successful[0]['label']} over {failed[0]['label']}."
            
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
        
        # Pattern detection: frameworks & test runners
        if "pytest" in combined_text.lower():
            facts.append(("testing", "Project uses pytest for automated verification", 1.0, "goal/test output"))
        if "jest" in combined_text.lower() or "npm test" in combined_text.lower():
            facts.append(("testing", "Project uses Node/Jest for automated verification", 1.0, "goal/test output"))
        if "fastapi" in combined_text.lower():
            facts.append(("architecture", "Backend framework includes FastAPI", 0.9, "task context"))
        if "sqlite" in combined_text.lower():
            facts.append(("database", "Project incorporates SQLite database storage", 1.0, "context"))
        if "docker" in combined_text.lower():
            facts.append(("devops", "Project supports Docker containerization", 1.0, "context"))

        return facts

    def _generate_skill_candidate(self, goal: str, domain: str, successful_rollouts: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not successful_rollouts:
            return None
        
        # Simple slug generation
        clean_name = re.sub(r"[^a-zA-Z0-9]+", "-", goal.lower()[:30]).strip("-")
        if not clean_name:
            clean_name = f"skill-{domain}"

        skill_content = f"""---
name: {clean_name}
description: Procedural skill synthesized from successful resolution of: {goal}
---

# Skill: {clean_name.replace('-', ' ').title()}

## Trigger Cue
When working on tasks related to:
> {goal}

## Verified Procedure
1. Review baseline dependencies and execute verification command.
2. Apply lessons learned from rollout {successful_rollouts[0].get('label', 'probe')}:
   - Avoid common failure modes seen in early probes.
   - Maintain repository architectural conventions.
3. Validate deliverables before finalizing the session.
"""
        return {"name": clean_name, "content": skill_content}
