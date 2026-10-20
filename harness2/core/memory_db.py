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

"""SQLite-backed persistent session and long-term memory engine for Harness²."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryDB:
    """Manages SQLite storage for sessions, rollouts, lessons, and long-term facts."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    best_rollout_id TEXT,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS longterm_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    evidence TEXT,
                    created_at TEXT NOT NULL,
                    last_observed TEXT NOT NULL,
                    UNIQUE(category, fact)
                );

                CREATE TABLE IF NOT EXISTS rollouts (
                    rollout_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    worktree_path TEXT,
                    git_diff TEXT,
                    test_passed INTEGER,
                    test_output TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    domain TEXT NOT NULL,
                    cue TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    evidence TEXT,
                    confidence TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
                """
            )

    def start_session(self, goal: str, domain: str = "general") -> str:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, goal, domain, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, goal, domain, "active", now),
            )
        return session_id

    def record_rollout(
        self,
        session_id: str,
        label: str,
        worktree_path: Optional[str] = None,
        git_diff: Optional[str] = None,
        test_passed: Optional[bool] = None,
        test_output: Optional[str] = None,
    ) -> str:
        rollout_id = f"roll_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        passed_val = None if test_passed is None else (1 if test_passed else 0)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rollouts (rollout_id, session_id, label, worktree_path, git_diff, test_passed, test_output, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rollout_id, session_id, label, worktree_path, git_diff, passed_val, test_output, now),
            )
        return rollout_id

    def add_longterm_fact(
        self,
        category: str,
        fact: str,
        confidence: float = 1.0,
        evidence: Optional[str] = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO longterm_memory (category, fact, confidence, evidence, created_at, last_observed)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, fact) DO UPDATE SET
                    confidence = excluded.confidence,
                    evidence = COALESCE(excluded.evidence, longterm_memory.evidence),
                    last_observed = excluded.last_observed;
                """,
                (category, fact.strip(), confidence, evidence, now, now),
            )

    def record_lesson(
        self,
        domain: str,
        cue: str,
        lesson: str,
        evidence: Optional[str] = None,
        confidence: str = "high",
        session_id: Optional[str] = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lessons (session_id, domain, cue, lesson, evidence, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, domain, cue.strip(), lesson.strip(), evidence, confidence, now),
            )

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if not row:
                return None
            sess = dict(row)
            rollouts = conn.execute("SELECT * FROM rollouts WHERE session_id = ?", (session_id,)).fetchall()
            sess["rollouts"] = [dict(r) for r in rollouts]
            return sess

    def get_active_worktrees(self, session_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT rollout_id, label, worktree_path FROM rollouts WHERE session_id = ? AND worktree_path IS NOT NULL",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def close_session(
        self,
        session_id: str,
        status: str = "completed",
        best_rollout_id: Optional[str] = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, best_rollout_id = ?, closed_at = ? WHERE session_id = ?",
                (status, best_rollout_id, now, session_id),
            )

    def query_longterm_memory(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM longterm_memory WHERE category = ? ORDER BY confidence DESC, last_observed DESC",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM longterm_memory ORDER BY category ASC, confidence DESC, last_observed DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def render_longterm_memory_markdown(self) -> str:
        """Render all long-term memory facts as a clean markdown block for component c3."""
        facts = self.query_longterm_memory()
        if not facts:
            return "# Long-Term Project Memory\n\nNo stored memory entries.\n"

        lines = ["# Long-Term Project Memory\n"]
        current_cat = None
        for f in facts:
            if f["category"] != current_cat:
                current_cat = f["category"]
                lines.append(f"\n## {current_cat.capitalize()}")
            evidence_note = f" (Evidence: {f['evidence']})" if f["evidence"] else ""
            lines.append(f"- **{f['fact']}**{evidence_note}")
        return "\n".join(lines) + "\n"
