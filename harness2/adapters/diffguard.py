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

"""What a patch would do to the repository, read before it does it.

Applying a candidate's diff is the one irreversible thing the harness does to
someone's repository. A patch that adds files and a patch that deletes them
are not the same kind of thing, and a verifier that exited 0 does not make a
151-file deletion safe -- a candidate once passed its tests after removing the
project's entire ``scripts/`` tree.

So the patch is parsed first, and a destructive one is refused until the
operator says otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["PatchStats", "analyze_patch", "describe_destructive"]

_DIFF_HEADER = re.compile(r"^diff --git ", re.M)
_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class PatchStats:
    """What one patch does to the tree."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    bytes_added: int = 0
    bytes_removed: int = 0
    # False when the patch could not be read at all. A patch nobody can parse
    # might delete anything, so the guard refuses it rather than waving it
    # through.
    parseable: bool = True

    @property
    def files_changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)

    @property
    def destructive(self) -> bool:
        """Does this patch remove files that were there?"""
        return bool(self.deleted) or not self.parseable

    def to_dict(self) -> dict:
        return {
            "files_changed": self.files_changed,
            "added": list(self.added),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "bytes_added": self.bytes_added,
            "bytes_removed": self.bytes_removed,
            "destructive": self.destructive,
            "parseable": self.parseable,
        }


def _paths(block: str) -> tuple[str | None, str | None]:
    """The (old, new) path of one diff block, from its --- / +++ lines.

    The scan stops at the hunk header: a line INSIDE a hunk that starts with
    `--- ` or `+++ ` is a source line, and reading it as a path made a
    deleted file be reported under the name of a comment in its own body.
    """
    old = new = None
    for line in block.splitlines():
        if line.startswith("@@"):
            break
        if line.startswith("--- "):
            old = line[4:].split("\t", 1)[0].strip()
        elif line.startswith("+++ "):
            new = line[4:].split("\t", 1)[0].strip()
    def clean(p: str | None) -> str | None:
        if not p or p == _DEV_NULL:
            return None
        if p.startswith(("a/", "b/")):
            p = p[2:]
        return p

    return clean(old), clean(new)


def analyze_patch(patch: str) -> PatchStats:
    """Read a unified diff and say what it would do.

    Binary patches (the ``--binary`` payload git emits for images) carry no
    line counts; they are counted as a change with unknown size, which is
    enough for the guard: a binary file is not what the guard is looking for.
    """
    if not patch or not patch.strip():
        return PatchStats()

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    bytes_added = 0
    bytes_removed = 0

    blocks = _DIFF_HEADER.split(patch)
    if len(blocks) == 1:
        # No `diff --git` header. The patch is not in a shape this reader
        # understands, so it might do anything -- and the guard must not wave
        # an unreadable patch through.
        return PatchStats(parseable=False)
    for block in blocks[1:]:
        old, new = _paths(block)
        is_new = "new file mode" in block
        is_del = "deleted file mode" in block
        path = new or old or "(unknown)"
        if is_new:
            added.append(path)
        elif is_del:
            deleted.append(path)
        else:
            modified.append(path)

        in_hunk = False
        for line in block.splitlines():
            if line.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith("+") and not line.startswith("+++"):
                bytes_added += len(line) - 1
            elif line.startswith("-") and not line.startswith("---"):
                bytes_removed += len(line) - 1

    return PatchStats(
        added=tuple(added),
        modified=tuple(modified),
        deleted=tuple(deleted),
        bytes_added=bytes_added,
        bytes_removed=bytes_removed,
    )


def describe_destructive(stats: PatchStats, *, limit: int = 12) -> str:
    """One sentence saying what the patch would destroy, for a refusal message."""
    if not stats.parseable:
        return (
            "the patch could not be read, so what it would remove is unknown. "
            "It is not in a format this harness can check"
        )
    shown = list(stats.deleted[:limit])
    more = len(stats.deleted) - len(shown)
    names = ", ".join(f"`{p}`" for p in shown)
    if more > 0:
        names += f" and {more} more"
    return (
        f"it deletes {len(stats.deleted)} file(s) ({names}) "
        f"and removes {stats.bytes_removed:,} bytes"
    )
