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

"""Live progress for a run that takes minutes.

A general-path run is tens of minutes of model calls. With nothing printed
between the start and the end, the terminal is blank for the whole run and a
working run is indistinguishable from a hung one -- which is also why people
end up running the loop in the background and guessing.

So the reporter writes a line per stage and per rollout, as it goes. It has
three sinks: human lines on stderr, JSONL for machines, and nothing at all.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, TextIO

FORMATS = ("human", "json", "none")

_MAX_EVENTS = 500


@dataclass
class Progress:
    """A reporter for one run."""

    fmt: str = "human"
    stream: TextIO | None = None
    t0: float = field(default_factory=time.monotonic)
    total_usd: float = 0.0
    events: list[dict] = field(default_factory=list)
    # How many stages and rollouts the run expects, so a line can say "3/14"
    # rather than "3/?".
    total_stages: int = 0
    total_rollouts: int = 0
    _stage_n: int = 0
    _rollout_n: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # emitting
    # ------------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> dict:
        """Record one event and write it out. Never raises: a broken pipe is
        not a reason to lose a run."""
        with self._lock:
            if kind == "stage":
                self._stage_n += 1
                fields.setdefault("index", self._stage_n)
                fields.setdefault("total", self.total_stages or self._stage_n)
            elif kind == "rollout":
                self._rollout_n += 1
                fields.setdefault("index", self._rollout_n)
                fields.setdefault("total", self.total_rollouts or self._rollout_n)
            rec = {"kind": kind, "t": round(time.monotonic() - self.t0, 2), **fields}
            self.total_usd += float(fields.get("usd") or 0.0)
            rec["cum_usd"] = round(self.total_usd, 4)
            if len(self.events) < _MAX_EVENTS:
                self.events.append(rec)
            if self.fmt == "none":
                return rec
            try:
                if self.fmt == "json":
                    print(json.dumps(rec, default=str), file=self.stream or sys.stderr, flush=True)
                else:
                    print(self._human(rec), file=self.stream or sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
        return rec

    def _human(self, rec: dict) -> str:
        kind = rec.get("kind")
        clock = time.strftime("%H:%M:%S")
        if kind == "stage":
            status = rec.get("status", "")
            wall = rec.get("wall_s")
            bits = [f"{clock}", f"stage {rec.get('index', '?')}/{rec.get('total', '?')}"]
            bits.append(f"{rec.get('label', ''):<10}")
            bits.append(f"{status:<12}")
            if wall is not None:
                bits.append(f"{float(wall):>7.1f}s")
            if rec.get("usd") is not None:
                bits.append(f"${float(rec.get('usd')):.4f}")
            if rec.get("error"):
                bits.append(f"  {str(rec['error'])[:120]}")
            return "  ".join(bits)
        if kind == "rollout":
            status = rec.get("status", "")
            bits = [f"{clock}", f"rollout {rec.get('index', '?')}/{rec.get('total', '?')}"]
            bits.append(f"{rec.get('label', ''):<12}")
            bits.append(f"{status:<12}")
            if rec.get("wall_s") is not None:
                bits.append(f"{float(rec.get('wall_s')):>7.1f}s")
            if rec.get("error"):
                bits.append(f"  {str(rec['error'])[:120]}")
            return "  ".join(bits)
        if kind == "note":
            return f"{clock}  {rec.get('text', '')}"
        if kind == "run":
            return f"{clock}  {rec.get('text', '')}"
        return f"{clock}  " + json.dumps(rec, default=str)


# The reporter the current run uses. A module-level default keeps the core
# free of a reporter parameter threaded through every call, and the lock keeps
# concurrent rollouts from interleaving half-lines.
_CURRENT: Progress | None = None
_CURRENT_LOCK = threading.Lock()


def current() -> Progress:
    """The reporter for this process; a no-op one when nothing was installed."""
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is None:
            _CURRENT = Progress(fmt="none")
        return _CURRENT


@contextmanager
def use(reporter: Progress) -> Iterator[Progress]:
    """Install a reporter for the duration of the block."""
    global _CURRENT
    with _CURRENT_LOCK:
        previous = _CURRENT
        _CURRENT = reporter
    try:
        yield reporter
    finally:
        with _CURRENT_LOCK:
            _CURRENT = previous


def silent() -> Progress:
    """A reporter that records events but prints nothing."""
    return Progress(fmt="none")
