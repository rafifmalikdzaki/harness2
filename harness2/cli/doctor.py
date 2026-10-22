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

"""Check that this machine can run Harness², and say what to fix if it cannot.

The general path needs a model service, a git repository, a working worktree
mechanism and a writable state directory. When one of them is missing, the
failure used to surface as a traceback twenty minutes into a run.

    harness2 doctor --workspace ~/Research/mmRetFund

Every check prints what it found and, when it failed, the line that fixes it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import config, llm
from ..adapters.sandbox import sandbox_available

OK = "ok"
WARN = "warn"
FAIL = "fail"


class Checks:
    """The check list, and how it renders."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.rows.append((name, status, detail, fix))

    @property
    def failures(self) -> int:
        return sum(1 for _n, s, _d, _f in self.rows if s == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for _n, s, _d, _f in self.rows if s == WARN)

    def render(self) -> str:
        glyph = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}
        lines = []
        for name, status, detail, fix in self.rows:
            lines.append(f"[{glyph[status]}] {name}" + (f" -- {detail}" if detail else ""))
            if fix and status != OK:
                lines.append(f"       fix: {fix}")
        return "\n".join(lines)


def _check_python(checks: Checks) -> None:
    if sys.version_info >= (3, 12):
        checks.add("python", OK, f"{sys.version.split()[0]}")
    else:
        checks.add(
            "python",
            FAIL,
            f"{sys.version.split()[0]} (3.12+ required)",
            "install Python 3.12 or newer",
        )


def _check_provider(checks: Checks) -> None:
    provider = llm.resolve_provider()
    if provider is None:
        checks.add(
            "model service",
            FAIL,
            "no provider configured",
            "set INCEPTION_API_KEY in harness2/.env, or HARNESS2_LLM_BASE_URL, "
            "HARNESS2_LLM_API_KEY and HARNESS2_LLM_MODEL",
        )
        return
    checks.add(
        "model service",
        OK,
        f"{provider.name} / {provider.model} at {provider.base_url}",
    )


def _check_mcp_package(checks: Checks) -> None:
    try:
        import mcp  # noqa: F401

        checks.add("mcp package", OK, "importable (the MCP server can start)")
    except ImportError:
        checks.add(
            "mcp package",
            WARN,
            "the `mcp` package is not installed, so harness2-mcp cannot start",
            "pip install 'harness2[mcp]'",
        )


def _check_git(checks: Checks) -> None:
    if not shutil.which("git"):
        checks.add("git", FAIL, "not on PATH", "install git")
        return
    try:
        res = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=30
        )
    except Exception as exc:  # pragma: no cover - depends on the machine
        checks.add("git", FAIL, f"could not run git: {exc}", "install git")
        return
    checks.add("git", OK, res.stdout.strip())


def _check_worktree(checks: Checks, workspace: Path | None) -> None:
    """A worktree round trip, in a throwaway repository.

    The worktree is how every candidate is isolated. If it does not work
    here, no candidate will run at all -- and the failure used to appear as a
    confusing per-candidate error.
    """
    if not shutil.which("git"):
        return
    with tempfile.TemporaryDirectory(prefix="harness2-doctor-") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        try:
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
                           capture_output=True, timeout=60)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True,
                           capture_output=True, timeout=60)
            subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True,
                           capture_output=True, timeout=60)
            (repo / "f.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True,
                           timeout=60)
            subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True,
                           capture_output=True, timeout=60)
            wt = Path(tmp) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt), "HEAD"],
                cwd=repo, check=True, capture_output=True, timeout=60,
            )
            made = wt.is_dir()
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=repo, check=False, capture_output=True, timeout=60,
            )
            subprocess.run(["git", "worktree", "prune"], cwd=repo, check=False,
                           capture_output=True, timeout=60)
        except subprocess.CalledProcessError as exc:
            checks.add(
                "git worktree",
                FAIL,
                f"could not create a detached worktree: {exc.stderr or exc}",
                "check that git can create worktrees in this repository",
            )
            return
        except Exception as exc:  # pragma: no cover - depends on the machine
            checks.add("git worktree", FAIL, f"{type(exc).__name__}: {exc}", "")
            return
        checks.add(
            "git worktree",
            OK if made else FAIL,
            "a detached worktree can be created and removed"
            if made
            else "the worktree was not created",
        )


def _check_sandbox(checks: Checks) -> None:
    if sandbox_available():
        checks.add("filesystem sandbox", OK, "bwrap is available; rollouts are confined")
    else:
        checks.add(
            "filesystem sandbox",
            WARN,
            "bwrap is not available, so a rollout can reach the rest of the machine",
            "install bubblewrap (bwrap) to confine rollouts to the worktree",
        )


def _check_state_dir(checks: Checks, workspace: Path | None) -> None:
    if workspace is None:
        return
    state = workspace / ".harness2"
    try:
        state.mkdir(parents=True, exist_ok=True)
        probe = state / ".doctor-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        checks.add(
            "harness state directory",
            FAIL,
            f"{state} is not writable: {exc}",
            f"make {state} writable",
        )
        return
    checks.add("harness state directory", OK, str(state))


def _check_workspace(checks: Checks, workspace: Path | None) -> None:
    if workspace is None:
        return
    if not workspace.is_dir():
        checks.add("workspace", FAIL, f"{workspace} does not exist", "check the path")
        return
    if not (workspace / ".git").exists():
        checks.add(
            "workspace",
            FAIL,
            f"{workspace} is not a git repository",
            "run `git init` there, or point --workspace at a repository",
        )
        return
    checks.add("workspace", OK, str(workspace))


def _check_dirty_tree(checks: Checks, workspace: Path | None) -> None:
    """Uncommitted work is invisible to a rollout.

    A worktree is built from HEAD, so anything the operator has not committed
    is not in the candidate's checkout. The candidate can delete it on the way
    out, and the operator finds out afterwards.
    """
    if workspace is None or not (workspace / ".git").exists():
        return
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception:
        return
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if lines:
        checks.add(
            "working tree",
            WARN,
            f"{len(lines)} uncommitted change(s); a rollout works from HEAD and will "
            f"not see them",
            "commit or stash them before running, so the rollout's base is what you think it is",
        )
    else:
        checks.add("working tree", OK, "clean")


def _check_env_file(checks: Checks) -> None:
    example = config.ROOT / ".env.example"
    if example.is_file():
        checks.add(".env.example", OK, str(example))
    else:
        checks.add(
            ".env.example",
            WARN,
            "missing; the README and setup docs tell you to copy it",
            "create .env.example listing every variable the harness reads",
        )
    env = config.ROOT / ".env"
    checks.add(
        ".env",
        OK if env.is_file() else WARN,
        str(env) if env.is_file() else "not present (the environment may supply the keys)",
    )


def _check_opencode(checks: Checks) -> None:
    path = shutil.which("opencode")
    if path:
        checks.add("opencode", OK, f"{path} (only needed for --channel opencode)")
    else:
        checks.add(
            "opencode",
            WARN,
            "not on PATH; only needed for --channel opencode or a benchmark run",
            "install opencode, or use --channel http",
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness2-doctor",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="The repository you intend to run against (optional)",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Make one live model call to confirm the endpoint answers",
    )
    args = parser.parse_args(argv)

    workspace = args.workspace.expanduser().resolve() if args.workspace else None
    checks = Checks()
    _check_python(checks)
    _check_provider(checks)
    _check_mcp_package(checks)
    _check_git(checks)
    _check_opencode(checks)
    _check_sandbox(checks)
    _check_env_file(checks)
    if workspace is not None:
        _check_workspace(checks, workspace)
        _check_state_dir(checks, workspace)
        _check_dirty_tree(checks, workspace)
    _check_worktree(checks, workspace)

    if args.probe:
        provider = llm.resolve_provider()
        if provider is None:
            checks.add("model service (live)", FAIL, "no provider configured", "")
        else:
            try:
                llm.chat(
                    [{"role": "user", "content": "Reply with: OK"}], max_tokens=512, retries=1
                )
                checks.add("model service (live)", OK, f"{provider.name} answered")
            except llm.LLMError as exc:
                checks.add(
                    "model service (live)",
                    FAIL,
                    str(exc)[:300],
                    "check the API key, the base URL and the model name",
                )

    print(checks.render())
    print()
    if checks.failures:
        print(f"{checks.failures} check(s) failed. The run will not work until they do.")
        return 1
    if checks.warnings:
        print(f"{checks.warnings} warning(s). The run will work, with the caveats above.")
        return 0
    print("Everything checks out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
