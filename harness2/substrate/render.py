"""Rendering an editable harness tree into OpenCode-native artifacts.

This lives in the substrate, not in a benchmark, because it is a property of
the *OpenCode surface* rather than of any one task set: given a staged tree it
produces the files an OpenCode session actually loads — ``AGENTS.md``,
``.opencode/skills/<n>/SKILL.md``, ``.opencode/agent/<n>.md``, ``scripts/`` and
``.opencode/plugin/``.

JobBench and the general-task adapter both render through these functions, so
the two paths cannot drift apart in how they deliver a harness to an agent.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

__all__ = [
    "compose_agent_body",
    "ensure_mode_subagent",
    "interp",
    "list_scripts",
    "read_component",
    "render_opencode_tree",
    "script_doc",
    "strip_comments",
]

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def interp(text: str, ctx: dict) -> str:
    """Substitute {{ name }} tokens only; single braces in prose stay untouched."""
    return _TOKEN.sub(lambda m: str(ctx.get(m.group(1), "")), text)


def strip_comments(text: str) -> str:
    """Drop HTML comment blocks, so a comment-only component renders to nothing."""
    return _COMMENT.sub("", text).strip()


def read_component(staged: Path, rel: str) -> str:
    """Read one component of a staged tree, or "" when it is absent."""
    p = Path(staged) / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def list_scripts(staged: Path) -> list[Path]:
    """The helper scripts of a staged tree, in name order."""
    d = Path(staged) / "scripts"
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.py") if not p.name.startswith("_"))


def script_doc(p: Path) -> str:
    """First line of the script's module docstring, if any."""
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "helper script"
    m = re.search(r'^\s*(?:"""|\'\'\')(.+)', txt)
    return m.group(1).strip()[:120] if m else "helper script"


def ensure_mode_subagent(text: str) -> str:
    """Give a subagent definition the `mode: subagent` frontmatter it needs."""
    if re.search(r"(?m)^mode:\s*subagent", text):
        return text
    if text.lstrip().startswith("---"):
        return re.sub(r"^---\n", "---\nmode: subagent\n", text, count=1)
    return "---\nmode: subagent\ndescription: delegated subagent\n---\n\n" + text


def compose_agent_body(staged: Path, ctx: dict) -> str:
    """c1 prose + c6 guardrails + c4 tool guidance + c3 memory + a c7 script note."""
    parts: list[str] = []
    sp = strip_comments(interp(read_component(staged, "systemprompt.md"), ctx))
    if sp:
        parts.append(sp)
    gr = strip_comments(interp(read_component(staged, "guardrails.md"), ctx))
    if gr:
        parts.append("# Operating Guardrails\n\n" + gr)
    tg = strip_comments(
        interp(read_component(staged, "tool_descriptions/tool_guidance.md"), ctx)
    )
    if tg:
        parts.append(tg)
    mem = strip_comments(interp(read_component(staged, "LongTermMEMORY.md"), ctx))
    if mem:
        parts.append("# Long-Term Memory\n\n" + mem)
    scripts = list_scripts(staged)
    if scripts:
        listing = "\n".join(f"- `python scripts/{s.name}` — {script_doc(s)}" for s in scripts)
        parts.append(
            "# Helper scripts (run via Bash)\n\nDeterministic helpers are available "
            "in `./scripts`. Prefer them over hand-rolling the same logic:\n\n" + listing
        )
    return "\n\n".join(p for p in parts if p.strip()) + "\n"


def render_opencode_tree(staged: Path, render_dir: Path, ctx: dict) -> None:
    """Materialise the staged tree into OpenCode-loadable artifacts under render_dir:
    AGENTS.md, .opencode/skills/<n>/, .opencode/agent/<sub>.md, scripts/, plugin/.
    Skill directories are copied whole, so skills/<n>/scripts/*.py survive."""
    staged, rd = Path(staged), Path(render_dir)
    if rd.exists():
        shutil.rmtree(rd)
    (rd / ".opencode" / "agent").mkdir(parents=True)
    (rd / ".opencode" / "skills").mkdir(parents=True)

    body = compose_agent_body(staged, ctx)
    if body.strip():
        (rd / "AGENTS.md").write_text(body, encoding="utf-8")

    skills_dir = staged / "skills"
    if skills_dir.is_dir():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            shutil.copytree(skill_md.parent, rd / ".opencode" / "skills" / skill_md.parent.name)

    sub_dir = staged / "sub_agents"
    if sub_dir.is_dir():
        for agent_md in sorted(sub_dir.glob("*/AGENT.md")):
            (rd / ".opencode" / "agent" / f"{agent_md.parent.name}.md").write_text(
                ensure_mode_subagent(agent_md.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8",
            )

    for s in list_scripts(staged):
        (rd / "scripts").mkdir(exist_ok=True)
        shutil.copy(s, rd / "scripts" / s.name)

    plugin_dir = staged / "plugin"
    if plugin_dir.is_dir():
        for p in sorted(plugin_dir.glob("*.ts")):
            if p.name.startswith("_"):
                continue
            (rd / ".opencode" / "plugin").mkdir(parents=True, exist_ok=True)
            shutil.copy(p, rd / ".opencode" / "plugin" / p.name)
