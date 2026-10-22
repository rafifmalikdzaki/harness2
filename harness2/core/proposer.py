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

"""Improver sessions with file access in an isolated context workspace.

OpenCode reads output/answer.md or the final message; the Claude CLI reads the
final message. Prompts use stdin, and empty replies raise an error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, llm
from ..adapters.sandbox import sandbox_available
from ..substrate.opencode import opencode_command
from .spec import ChannelSpec
from .tree import normalize_rel

SYSTEM_ARG_MAX = 120_000


MAX_CONCURRENCY = int(config.PROPOSER_CONCURRENCY)
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY) if MAX_CONCURRENCY > 0 else None


_DROP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
)


# The `http` channel has no agent process behind it, so the contract with the
# model is explicit: the session's files go in the prompt, and what comes back
# is applied or returned.
#
# There are two kinds of session. A PROPOSAL session edits the harness tree;
# its answer is a set of file edits. An ANSWER session (the selector, the
# lesson distiller) reads evidence and answers in the format its mission
# specifies; its answer is text, and the format is the mission's own.
_HTTP_SYSTEM_PROPOSE = """You are the improver of an autonomous agent's harness.

You are given a MISSION and the CURRENT harness files. The MISSION says what to change and
why. Reply with ONLY a JSON object, no prose before or after it:

{"notes": "<one short paragraph: what you changed and why, tracing each change to a moment in the evidence>",
 "edits": [{"path": "<path relative to the harness root>", "action": "write"|"delete", "content": "<the full new file content>"}]}

Rules:
- For every write, give the FULL new content of the file. Never a diff, never
  "... unchanged", never a partial file.
- "action": "delete" removes the file and needs no "content".
- Only edit paths the MISSION lists as editable. A path outside that list is
  rejected and the whole proposal is refused.
- Keep every change traceable to a moment in the evidence the MISSION points at.
- If the harness should stay as it is, return an empty "edits" list and say why
  in "notes".
- Your reply is parsed by a program. Anything other than one JSON object is a failed reply."""


_HTTP_SYSTEM_ANSWER = """You are executing one step of an agent-harness refinement.

The MISSION below states exactly what to do and exactly what your reply must contain. Follow its
OUTPUT FORMAT section literally: your reply is parsed by a program, so a ```choice``` block, a
<why> block or an <experience> block that is missing, renamed or reformatted is a failed reply.

Reply with the requested output only. No preamble, no commentary, no restating the mission."""


def _session_kind(label: str) -> str:
    """Whether a session proposes edits or answers in the mission's own format.

    The label is the method's own convention: `select.<mode><k>` and
    `experience.<mode><k>` are answer sessions, and a proposal session is
    labelled after its stage (`inv1`, `dom2`, `agg1`, ...).
    """
    if label.startswith("select.") or label.startswith("experience."):
        return "answer"
    return "propose"


def _http_propose_prompt(mission: str, system: str, files: dict[str, str]) -> str:
    """The user message for a proposal session: mission, then the current tree."""
    parts = [f"# MISSION\n\n{mission}"]
    if system:
        parts.append(f"# ADDITIONAL INSTRUCTIONS\n\n{system}")
    if files:
        rendered = [f'<file path="{rel}">\n{files[rel]}\n</file>' for rel in sorted(files)]
        parts.append(
            "# CURRENT HARNESS FILES\n\n"
            "These are the files you are editing. Emit an edit for each one you change.\n\n"
            + "\n\n".join(rendered)
        )
    else:
        parts.append("# CURRENT HARNESS FILES\n\n(the harness is empty)")
    return "\n\n".join(parts)


def _http_answer_prompt(mission: str, system: str, files: dict[str, str]) -> str:
    """The user message for an answer session: mission, then the evidence."""
    parts = [f"# MISSION\n\n{mission}"]
    if system:
        parts.append(f"# ADDITIONAL INSTRUCTIONS\n\n{system}")
    if files:
        rendered = [f'<file path="{rel}">\n{files[rel]}\n</file>' for rel in sorted(files)]
        parts.append(
            "# THE EVIDENCE IS BELOW, INLINED\n\n"
            "The mission refers to files on disk. Here they are, inline.\n\n"
            + "\n\n".join(rendered)
        )
    return "\n\n".join(parts)


def _obj(d: dict, key: str) -> dict:
    """`d[key]` when it is a dict, else {}."""
    v = d.get(key)
    return v if isinstance(v, dict) else {}


class ProposerError(RuntimeError):
    """A call did not produce a usable reply. Never swallowed into an empty string."""

    def __init__(self, message: str, *, attempts: int = 0, cost_usd: float = 0.0):
        super().__init__(message)
        self.attempts = attempts
        self.cost_usd = cost_usd


class NonRetryableError(RuntimeError):
    """A failure a retry cannot fix: a missing key, a missing binary.

    Retrying it costs the caller the backoff and changes nothing, so the
    session gives up at once and says what is missing.
    """


@dataclass
class Reply:
    """One session: what came back, what it cost, and where the audit trail is."""

    label: str
    text: str
    ok: bool
    cost_usd: float = 0.0
    wall_s: float = 0.0
    attempts: int = 0
    cached: bool = False
    channel: str = ""
    error: str = ""
    audit: Path | None = None
    workspace: Path | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Proposer:
    """Runs agentic sessions on one channel, with an audit trail and a cache."""

    spec: ChannelSpec
    audit_dir: Path | None = None
    attempts: int = 2
    project: str = field(default_factory=lambda: config.VERTEX_PROJECT)
    region: str = field(default_factory=lambda: config.VERTEX_REGION)
    _calls: list[Reply] = field(default_factory=list)
    # Token counts from the last call, for channels that report usage but no price.
    _last_usage: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.spec.model.strip() and self.spec.kind != "http":
            raise ValueError("Set HARNESS2_IMPROVER_MODEL before starting model sessions")

    def env(self) -> dict[str, str]:
        e = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        if self.spec.kind == "claude_cli":
            e.update(
                {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": self.project,
                    "CLOUD_ML_REGION": self.region,
                    "ANTHROPIC_MODEL": self.spec.model,
                }
            )
        else:
            e.update(
                {
                    "GOOGLE_VERTEX_PROJECT": self.project,
                    "GOOGLE_VERTEX_LOCATION": self.region,
                    "VERTEXAI_LOCATION": self.region,
                }
            )
        return e

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
    ) -> Reply:
        """Run one session. Raises ProposerError if no usable reply after `attempts`.

        `harvest` names a subpath of the session's working directory to copy out before that
        directory is destroyed -- e.g. "context/harness", which is how an edited tree comes back.
        """

        cache_key = _call_fingerprint(label, mission, system, context_dir)
        cached = self._cached(label, cache_key, require_workspace=bool(harvest))
        if cached is not None:
            self._calls.append(cached)
            return cached

        wall = int(timeout or self.spec.timeout)
        tries = max(1, int(self.attempts if max_attempts is None else max_attempts))
        last = ""
        spent_usd = 0.0
        t0 = time.time()
        for attempt in range(1, tries + 1):
            if attempt > 1:
                time.sleep(10 * (attempt - 1))
            tmp = Path(tempfile.mkdtemp(prefix="harness2-prop-"))
            try:
                if context_dir and Path(context_dir).is_dir():
                    shutil.copytree(context_dir, tmp / "context", dirs_exist_ok=True)
                with _SEM if _SEM is not None else contextlib.nullcontext():
                    text, cost, err = self._dispatch(tmp, mission, system, wall, label)
                spent_usd += cost
                if not text.strip():
                    last = err or "empty reply"
                    continue
                r = Reply(
                    label=label,
                    text=text,
                    ok=True,
                    cost_usd=spent_usd,
                    wall_s=round(time.time() - t0, 1),
                    attempts=attempt,
                    channel=self.spec.kind,
                )
                if harvest:
                    r.workspace = self._harvest(tmp, harvest, label, cache_key)
                    if r.workspace is None:
                        _drop_workspace(self._paths(label, cache_key))
                self._write_audit(r, mission, system, cache_key)
                self._calls.append(r)
                return r
            except subprocess.TimeoutExpired:
                last = f"timeout after {wall}s"
            except NonRetryableError as exc:
                last = str(exc)
                break
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        r = Reply(
            label=label,
            text="",
            ok=False,
            wall_s=round(time.time() - t0, 1),
            attempts=tries,
            channel=self.spec.kind,
            error=last,
            cost_usd=spent_usd,
        )
        self._calls.append(r)
        self._write_audit(r, mission, system, cache_key)
        raise ProposerError(
            f"[{label}] {self.spec.kind}/{self.spec.model}: {last}",
            attempts=tries,
            cost_usd=spent_usd,
        )

    def _dispatch(
        self, cwd: Path, mission: str, system: str, wall: int, label: str = ""
    ) -> tuple[str, float, str]:
        """Run the session on this channel's backend."""
        if self.spec.kind == "claude_cli":
            return self._claude_cli(cwd, mission, system, wall)
        if self.spec.kind == "http":
            return self._http(cwd, mission, system, wall, label)
        return self._opencode(cwd, mission, system, wall)

    def _http(
        self, cwd: Path, mission: str, system: str, wall: int, label: str
    ) -> tuple[str, float, str]:
        """Run one session over the plain HTTP chat client.

        No agent process runs here, so this channel owns the whole round trip: it
        reads the session's files out of the working directory, sends them with the
        mission, and either applies the model's edits to the harness tree or
        returns the model's answer verbatim. Everything downstream is unchanged --
        a proposal is read back and validated like any other, and an answer is
        parsed by the same regexes the CLI channels' answers are parsed by.

        Cost is reported as 0.0 because an OpenAI-compatible endpoint does not
        return a price. The token counts land in the audit trail instead, so a
        session on this channel is metered rather than priced.
        """
        kind = _session_kind(label)
        provider = llm.resolve_provider()
        if provider is None:
            raise NonRetryableError(
                "no LLM configured: set INCEPTION_API_KEY (or HARNESS2_LLM_BASE_URL, "
                "HARNESS2_LLM_API_KEY and HARNESS2_LLM_MODEL) to use the http channel"
            )

        if kind == "propose":
            harness_dir = _find_harness_dir(cwd)
            if harness_dir is None:
                return "", 0.0, "no harness directory to edit"
            files = _read_harness_files(harness_dir)
            system_prompt = _HTTP_SYSTEM_PROPOSE
            user = _http_propose_prompt(mission, system, files)
        else:
            harness_dir = None
            files = _read_session_files(cwd)
            system_prompt = _HTTP_SYSTEM_ANSWER
            user = _http_answer_prompt(mission, system, files)

        usage: dict = {}
        reply = ""
        last_err = ""
        # A proposal that comes back as prose is the common failure, and a
        # second attempt with the correction attached fixes it more often than
        # the same request again.
        for attempt in range(2):
            try:
                reply = llm.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.4 if kind == "propose" else 0.0,
                    max_tokens=16000,
                    provider=provider,
                    timeout=max(30, min(int(wall), 900)),
                    usage=usage,
                )
            except llm.LLMError as exc:
                return "", 0.0, str(exc)
            self._last_usage = usage
            if kind == "answer":
                if not reply.strip():
                    last_err = "empty reply"
                    continue
                return reply, 0.0, ""
            text, err = self._apply_proposal(harness_dir, reply)
            if not err:
                return text, 0.0, ""
            last_err = err
            user = (
                f"{user}\n\n# CORRECTION\n\nYour previous reply was not usable: {err}\n\n"
                f"Reply with ONE JSON object and nothing else."
            )
        return "", 0.0, last_err or "the model did not return a usable reply"

    def _apply_proposal(self, harness_dir: Path | None, reply: str) -> tuple[str, str]:
        """Apply a proposal's edits to the harness tree; return (reply text, error)."""
        try:
            data = llm.extract_json(reply)
        except (llm.LLMError, ValueError) as exc:
            return "", f"the proposal was not valid JSON: {exc}"
        if not isinstance(data, dict):
            return "", f"the proposal was not a JSON object: {type(data).__name__}"

        edits = data.get("edits")
        if edits is None:
            edits = []
        if not isinstance(edits, list):
            return "", "'edits' was not a list"

        applied, refused = _apply_harness_edits(harness_dir, edits)
        notes = str(data.get("notes") or "").strip()
        if not applied and not refused:
            notes = notes or "(the proposal changed nothing)"
        summary = {
            "edits_applied": applied,
            "edits_refused": refused,
        }
        text = (
            f"<notes>\n{notes or '(no notes given)'}\n</notes>\n\n"
            f"```json\n{json.dumps(summary, indent=2)}\n```"
        )
        return text, ""

    def _claude_cli(
        self, cwd: Path, mission: str, system: str, wall: int
    ) -> tuple[str, float, str]:
        cli = shutil.which("claude") or "claude"
        cmd = [
            cli,
            "--dangerously-skip-permissions",
            "-p",
            "--output-format",
            "json",
            "--effort",
            self.spec.effort,
        ]
        stdin = mission
        if system:
            if len(system) > SYSTEM_ARG_MAX:
                stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
                cmd += [
                    "--append-system-prompt",
                    "Follow the SYSTEM INSTRUCTIONS block at the top of the message.",
                ]
            else:
                cmd += ["--append-system-prompt", system]
        p = subprocess.run(
            cmd,
            input=stdin,
            cwd=cwd,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        payload = _parse_json(p.stdout)
        text = str(payload.get("result") or payload.get("text") or "")
        err = ""
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            err = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
            text = ""
        elif p.returncode != 0 and not text.strip():
            err = f"rc={p.returncode}: {(p.stderr or '')[-300:]}"
        return text, float(payload.get("total_cost_usd") or 0.0), err

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)

        (cwd / "opencode.json").write_text(
            json.dumps({"agent": {"build": {"steps": self.spec.steps}}}), encoding="utf-8"
        )
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        env = self.env()
        command = opencode_command(
            prompt=prompt,
            workspace=cwd,
            model=self.spec.model,
            title="propose",
            variant=self.spec.effort,
            # The proposer's working directory is a scratch directory holding
            # the evidence it must read. It is sandboxed for the same reason
            # the solver is: a relative write must not land outside it, and
            # the operator's home must not be readable.
            sandbox=sandbox_available(),
        )
        env.update(command.env)
        if command.kind == "source":
            # The source build is self-contained, so the session's state can be
            # isolated. The installed build finds its credentials in the
            # real data dir, so redirecting XDG there would lock it out.
            env["XDG_DATA_HOME"] = str(cwd / ".ocdata")
            env["XDG_STATE_HOME"] = str(cwd / ".ocstate")
        p = subprocess.run(
            command.argv,
            cwd=command.cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        cost = _opencode_cost(p.stdout)
        ans = out / "answer.md"
        if ans.is_file() and ans.read_text(errors="replace").strip():
            return ans.read_text(errors="replace"), cost, ""
        text, err = _opencode_stdout(p.stdout)
        if text.strip():
            return text, cost, ""
        tail = _strip_oc_banner(p.stderr or "").strip()[-300:]
        return "", cost, f"rc={p.returncode}: {err or tail or 'empty reply'}"

    def _harvest(self, tmp: Path, sub: str, label: str, cache_key: str = "") -> Path | None:
        """Copy `tmp/<sub>` somewhere durable before the session directory is removed."""
        src = tmp / sub
        if not src.is_dir():
            return None
        dest = Path(self.audit_dir or tempfile.mkdtemp(prefix="harness2-harvest-"))
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        dest = dest / f"{safe}.workspace"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return dest

    def _paths(self, label: str, cache_key: str = "") -> dict[str, Path] | None:
        if not self.audit_dir:
            return None
        d = Path(self.audit_dir)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        return {k: d / f"{safe}.{k}.md" for k in ("mission", "system", "response")} | {
            "meta": d / f"{safe}.meta.json",
            "workspace": d / f"{safe}.workspace",
        }

    def _cached(
        self, label: str, cache_key: str = "", *, require_workspace: bool = False
    ) -> Reply | None:
        p = self._paths(label, cache_key)
        if not p or not p["response"].is_file():
            return None
        if require_workspace and not p["workspace"].is_dir():
            return None
        txt = p["response"].read_text(encoding="utf-8", errors="replace")
        if not txt.strip():
            return None
        return Reply(
            label=label,
            text=txt,
            ok=True,
            cost_usd=0.0,
            cached=True,
            channel=self.spec.kind,
            audit=p["response"],
            workspace=p["workspace"] if p["workspace"].is_dir() else None,
        )

    def _write_audit(self, r: Reply, mission: str, system: str, cache_key: str = "") -> None:
        p = self._paths(r.label, cache_key)
        if not p:
            return
        p["mission"].write_text(mission, encoding="utf-8")
        if system:
            p["system"].write_text(system, encoding="utf-8")
        p["response"].write_text(r.text, encoding="utf-8")
        p["meta"].write_text(
            json.dumps(
                {
                    "label": r.label,
                    "ok": r.ok,
                    "channel": r.channel,
                    "model": self.spec.model,
                    "effort": self.spec.effort,
                    "attempts": r.attempts,
                    "wall_s": r.wall_s,
                    "cost_usd": r.cost_usd,
                    "cost_known": r.channel != "http",
                    "usage": self._last_usage or {},
                    "error": r.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        r.audit = p["response"]


def _drop_workspace(paths: dict[str, Path] | None) -> None:
    """Remove a stale harvested workspace so a later cache read cannot serve it."""
    if paths:
        shutil.rmtree(paths["workspace"], ignore_errors=True)


def _find_harness_dir(cwd: Path) -> Path | None:
    """The harness tree inside a session's working directory."""
    for cand in (cwd / "context" / "harness", cwd / "harness"):
        if cand.is_dir():
            return cand
    return None


def _read_harness_files(harness_dir: Path) -> dict[str, str]:
    """Every file in the harness tree, as rel -> text."""
    out: dict[str, str] = {}
    for p in sorted(harness_dir.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        try:
            out[p.relative_to(harness_dir).as_posix()] = p.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return out


# An answer session's evidence is inlined into the prompt, because the model
# has no filesystem of its own. The budget stops a session with a hundred
# candidates from asking for a context window that does not exist.
_SESSION_FILE_CHARS = 40_000
_SESSION_TOTAL_CHARS = 400_000


def _read_session_files(root: Path) -> dict[str, str]:
    """The session's text files, as rel -> text, within a budget."""
    out: dict[str, str] = {}
    total = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz"):
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not body.strip():
            continue
        clipped = body[:_SESSION_FILE_CHARS]
        if len(body) > _SESSION_FILE_CHARS:
            clipped += "\n\n(... truncated; read the file on disk for the rest ...)"
        if total + len(clipped) > _SESSION_TOTAL_CHARS:
            break
        out[p.relative_to(root).as_posix()] = clipped
        total += len(clipped)
    return out


def _apply_harness_edits(
    harness_dir: Path | None, edits: list
) -> tuple[list[str], list[str]]:
    """Write the model's edits into the harness tree; return (applied, refused).

    A path that escapes the tree is refused rather than written: the tree is the
    one place a proposal can touch, and a proposal that reaches outside it has
    to be told no rather than obeyed.
    """
    if harness_dir is None:
        return [], ["(no harness directory to edit)"]
    root = harness_dir.resolve()
    applied: list[str] = []
    refused: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            refused.append("(not an object)")
            continue
        raw = str(edit.get("path") or "").strip().replace("\\", "/")
        if not raw:
            refused.append("(no path)")
            continue
        try:
            rel = normalize_rel(raw)
        except ValueError:
            refused.append(f"{raw} (escapes the harness)")
            continue
        target = root / rel
        try:
            resolved = target.resolve()
        except OSError:
            refused.append(f"{rel} (unresolvable path)")
            continue
        if resolved != root and root not in resolved.parents:
            refused.append(f"{rel} (escapes the harness)")
            continue
        action = str(edit.get("action") or "write").strip().lower()
        if action == "delete":
            if target.is_file():
                target.unlink()
                applied.append(rel)
            else:
                refused.append(f"{rel} (not present)")
            continue
        content = edit.get("content")
        if not isinstance(content, str):
            refused.append(f"{rel} (no content)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        applied.append(rel)
    return applied, refused


def _opencode_dir() -> Path:
    """The upstream OpenCode checkout to run through bun: config.OPENCODE_DIR."""
    oc = config.OPENCODE_DIR
    if not (oc / "packages" / "opencode" / "src" / "index.ts").is_file():
        raise FileNotFoundError(
            f"no OpenCode checkout at {oc} (config.OPENCODE_DIR); run "
            f"scripts/setup_benchmark.py <benchmark> to clone the pinned commit there"
        )
    return oc


def _parse_json(stdout: str) -> dict:
    """The claude CLI's one JSON object, tolerating banners and trailing noise around it.

    Not for opencode stdout, which is a JSONL event stream: use `_opencode_stdout`.
    """
    s = (stdout or "").strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                pass
    return {"result": s}


_OC_BANNER = re.compile(
    r"^(?:Performing one time database migration[^\n]*|sqlite-migration:[^\n]*"
    r"|Database migration complete\.)[ \t]*$",
    re.M,
)


def _strip_oc_banner(s: str) -> str:
    """Drop the migration banner lines from OpenCode output. No-op when they are absent."""
    return _OC_BANNER.sub("", s or "")


def _opencode_stdout(stdout: str) -> tuple[str, str]:
    """Fold `opencode run --format json` into (final assistant text, error text).

    The stream is one raw event per line and assistant prose lives at part.text of a `text`
    event. All text events are joined, so a trailing terminator event cannot swallow the
    answer; `error` events become the error string. Plain-text stdout is returned as-is.
    """
    texts: list[str] = []
    errors: list[str] = []
    saw_json = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        saw_json = True
        part = _obj(ev, "part")
        ptype = part.get("type") or ev.get("type")
        if ptype == "text":
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t)
        elif ptype == "error":
            e = ev.get("error")
            if isinstance(e, dict):
                msg = str(_obj(e, "data").get("message") or e.get("name") or "").strip()
            else:
                msg = str(e or "").strip()
            if msg:
                errors.append(msg)
    if not saw_json:
        return _strip_oc_banner(stdout).strip(), ""
    return "\n\n".join(texts), "; ".join(errors)[-300:]


def _opencode_cost(stdout: str) -> float:
    """Sum `part.cost` over the step_finish events of an `opencode run --format json` stream."""
    total = 0.0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        part = _obj(ev, "part")
        if (part.get("type") or ev.get("type")) in ("step-finish", "step_finish"):
            c = part.get("cost")
            if isinstance(c, (int, float)):
                total += float(c)
    return round(total, 6)


def _call_fingerprint(label: str, mission: str, system: str, context_dir: Path | str | None) -> str:
    """Content-address one call, including its evidence and its mode/task workspace path.

    The resolved path is part of the key: the two modes can have byte-identical k=1 evidence
    and must still be independent draws.
    """
    h = hashlib.sha256()
    for value in (label, mission, system):
        h.update(value.encode("utf-8", errors="replace"))
        h.update(b"\0")
    if context_dir:
        root = Path(context_dir)
        h.update(str(root.resolve()).encode("utf-8", errors="replace"))
        h.update(b"\0")
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file() or "__pycache__" in p.parts:
                    continue
                h.update(str(p.relative_to(root)).encode("utf-8", errors="replace"))
                h.update(b"\0")
                try:
                    with p.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
                except OSError as exc:
                    h.update(f"<unreadable:{type(exc).__name__}:{exc}>".encode())
                h.update(b"\0")
    return h.hexdigest()[:16]
