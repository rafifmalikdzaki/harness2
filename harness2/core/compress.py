"""Keeping the harness's memory small enough to be worth keeping.

A harness that runs for months accumulates three kinds of memory: the
run artifacts (transcripts, diffs, evidence trees), the SQLite record
(sessions, rollouts, facts, lessons), and the experience library (one
entry per task). All three grew without bound, and the first of them grew
fast enough to fill a disk.

The rule throughout is the same: **keep the conclusions, not the
transcripts.** A trajectory is compressed and kept for audit; a lesson is
a sentence; a fact is a line. What the harness reads back into the next
run's harness is the lesson and the fact -- a few hundred bytes -- not the
evidence that produced them.

Text that is written once and read often is stored gzipped. The readers
are transparent about it: a path ending in ``.gz`` is decompressed, and
one that does not is read as-is, so a directory written by an older
version still reads.
"""

from __future__ import annotations

import gzip
from pathlib import Path

__all__ = [
    "compress_text",
    "decompress_text",
    "read_text",
    "write_text",
    "copy_compressed",
]

# A gzip level that is quick enough to run on every rollout and still
# takes an order of magnitude off a transcript.
_LEVEL = 6


def compress_text(text: str) -> bytes:
    """Compress text for storage."""
    return gzip.compress(text.encode("utf-8"), compresslevel=_LEVEL)


def decompress_text(data: bytes) -> str:
    """Decompress text written by :func:`compress_text`."""
    return gzip.decompress(data).decode("utf-8", errors="replace")


def write_text(path: Path | str, text: str, *, compress: bool = True) -> Path:
    """Write text, gzipped when the name says so.

    A path ending in ``.gz`` is compressed; anything else is written
    plainly, so a caller that wants a readable artifact still gets one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".gz"):
        path.write_bytes(compress_text(text))
    else:
        path.write_text(text, encoding="utf-8")
    return path


def read_text(path: Path | str) -> str:
    """Read text, decompressing it when the name says so.

    A plain file is read as-is, so a directory written before compression
    was added still reads.
    """
    path = Path(path)
    if not path.is_file():
        return ""
    if path.name.endswith(".gz"):
        try:
            return decompress_text(path.read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile):
            return ""
    return path.read_text(encoding="utf-8", errors="replace")


def copy_compressed(src: Path | str, dst: Path | str) -> Path | None:
    """Copy a text file, compressing the copy if it is worth compressing.

    Transcripts compress by roughly an order of magnitude; a small diff
    does not, and stays readable.
    """
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        return None
    text = src.read_text(encoding="utf-8", errors="replace")
    # Below this size the compression overhead is not worth the
    # unreadability, and a reviewer will want to read it.
    if len(text) >= _MIN_COMPRESS_BYTES:
        return write_text(dst.with_suffix(dst.suffix + ".gz"), text)
    return write_text(dst, text)


_MIN_COMPRESS_BYTES = 16_000
