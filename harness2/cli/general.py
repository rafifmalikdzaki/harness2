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

"""Run the real Harness² recursion on any repository and goal.

This is the general-task entry point: no benchmark, no dataset, no judge.
Point it at a git repository and a goal, and the same Propose-Probe-Compose
engine the paper describes will

  * propose harness edits (instructions, guardrails, skills, scripts),
  * probe them by running a solver in an isolated worktree, with the proposed
    harness as that solver's operating instructions,
  * compose the best contrasts into a refined harness, and
  * select a winner by reading the candidates, without a ground-truth verifier.

    harness2-general --workspace ~/Research/histodae \\
        --goal "add a --config-path flag that accepts a packaged config name" \\
        --k 2 --mode parallel

The verification command is optional. With one, the rollout is also graded by
its exit status; without one, the selection is made purely on the contrast
between candidates.

One API key is enough: the solver, the improver and the selector all run on
the same model service. `--channel opencode` moves the improver and the
selector onto the OpenCode CLI instead, which needs its own model id.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config, llm
from ..adapters.general import GeneralTaskAdapter, candidate_rows, render_candidate_table
from ..core import progress
from ..core.proposer import Proposer
from ..core.retention import prune_runs
from ..core.schedule import mode_token, run_domain
from ..core.spec import resolve_channel_model


def _compact_library(library_root: Path, adapter, modes) -> int:
    """Retire the library entries a newer one supersedes.

    The scheduler keeps one library per mode, under ``<root>/<mode>``.
    """
    retired = 0
    for mode in modes:
        try:
            from ..core.library import Library

            lib = Library(
                root=Path(library_root) / mode,
                domain=adapter.domain,
                surface=adapter.substrate.surface(),
            )
            retired += lib.compact()
        except Exception as exc:
            print(f"[!] could not compact the {mode} library: {exc}", file=sys.stderr)
    return retired


def _estimate(k: int, modes: tuple[str, ...]) -> dict:
    """What a run of this shape costs in model calls.

    Each mode schedules 4k rollouts and 3k improvement sessions, and the base
    rollouts are shared across modes.
    """
    rollouts = sum(4 * k for _ in modes)
    sessions = sum(3 * k for _ in modes)
    return {
        "modes": list(modes),
        "k": k,
        "rollouts": rollouts,
        "improver_sessions": sessions,
        "selector_sessions": len(modes),
        "model_calls": rollouts + sessions + len(modes),
    }


def _dry_run(workspace: Path, goal: str, k: int, modes, channel, verify_cmd) -> int:
    """Print what the run would do, without doing any of it."""
    est = _estimate(k, modes)
    provider = llm.resolve_provider()
    print(f"workspace : {workspace}")
    print(f"goal      : {goal}")
    print(f"verify    : {verify_cmd or '(none -- the selection is made on contrast alone)'}")
    print(f"channel   : {channel}")
    if channel == "http":
        if provider is None:
            print(
                "model     : NOT CONFIGURED -- set INCEPTION_API_KEY in harness2/.env, "
                "or HARNESS2_LLM_BASE_URL / HARNESS2_LLM_API_KEY / HARNESS2_LLM_MODEL"
            )
        else:
            print(f"model     : {provider.name} {provider.model} at {provider.base_url}")
    else:
        print(f"model     : {config.IMPROVER_MODEL or '(unset -- the opencode channel needs one)'}")
    print(f"modes     : {', '.join(modes)}")
    print(f"budget    : {est['rollouts']} rollouts, {est['improver_sessions']} improver sessions, "
          f"{est['selector_sessions']} selection session(s)")
    print(f"total     : ~{est['model_calls']} model calls")
    print(f"artifacts : {workspace / '.harness2' / 'runs'}")
    print()
    print("Nothing was run. Drop --dry-run to start.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness2-general",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace", type=Path, required=True, help="Git repository to work in")
    parser.add_argument("--goal", required=True, help="What the agent should accomplish")
    parser.add_argument("--domain", default="general", help="Domain label for the experience library")
    parser.add_argument("--k", type=int, default=2, help="Task-level recursion budget")
    parser.add_argument("--mode", choices=("parallel", "sequential", "both"), default="parallel")
    parser.add_argument("--verify", default=None, help="Verification command run after each rollout")
    parser.add_argument(
        "--model", default=None, help="Solver model for the opencode runtime (default: HARNESS2_SOLVER_MODEL)"
    )
    parser.add_argument(
        "--improver-model",
        default=None,
        help="Proposer/selector model (default: the solver's model service)",
    )
    parser.add_argument(
        "--channel",
        choices=("http", "opencode"),
        default="http",
        help=(
            "How the improver and the selector run. 'http' (default) uses the same "
            "model service as the solver, so one API key runs the whole loop. "
            "'opencode' uses the OpenCode CLI and needs a provider-prefixed id."
        ),
    )
    parser.add_argument("--timeout", type=int, default=1800, help="Per-rollout timeout in seconds")
    parser.add_argument("--runs-root", type=Path, default=None, help="Where run artifacts go (default: <workspace>/.harness2/runs)")
    parser.add_argument(
        "--progress",
        choices=("human", "json", "none"),
        default="human",
        help="How to report progress while the run goes (default: human lines on stderr)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the run would do -- the budget and the models -- and stop",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help=(
            "Run the rollout without the filesystem sandbox. The sandbox is "
            "what keeps a shell command from writing outside the workspace or "
            "reading your home directory; it is on by default when bwrap is "
            "available."
        ),
    )
    parser.add_argument(
        "--allow-mcp",
        action="store_true",
        help=(
            "Let the rollout use the MCP servers configured in your OpenCode. "
            "Off by default: they may hold credentials, and they are a route out "
            "of the workspace that a path fence does not close."
        ),
    )
    args = parser.parse_args(argv)

    if args.k < 1:
        parser.error("--k must be positive")

    workspace = args.workspace.expanduser().resolve()
    if not (workspace / ".git").exists():
        parser.error(f"--workspace {workspace} is not a git repository")

    modes = ("agg", "seq") if args.mode == "both" else (mode_token(args.mode),)

    if args.channel == "http":
        provider = llm.resolve_provider()
        if provider is None:
            parser.error(
                "no model service configured: set INCEPTION_API_KEY in harness2/.env, or "
                "HARNESS2_LLM_BASE_URL / HARNESS2_LLM_API_KEY / HARNESS2_LLM_MODEL "
                "(or pass --channel opencode to use the OpenCode CLI instead)"
            )
        try:
            channel_spec = resolve_channel_model("http", args.improver_model or None)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        improver_model = (args.improver_model or config.IMPROVER_MODEL).strip()
        if not improver_model:
            parser.error(
                "no improver model: set HARNESS2_IMPROVER_MODEL or pass --improver-model "
                "(the opencode channel needs a provider-prefixed id, e.g. anthropic/claude-sonnet-4-5)"
            )
        try:
            channel_spec = resolve_channel_model("opencode", improver_model)
        except ValueError as exc:
            parser.error(str(exc))

    if args.dry_run:
        return _dry_run(workspace, args.goal, args.k, modes, args.channel, args.verify)

    solver_model = (args.model or config.SOLVER_MODEL_OC).strip()
    if not solver_model:
        solver_model = channel_spec.model

    try:
        adapter = GeneralTaskAdapter(
            repo_root=workspace,
            goal=args.goal,
            domain=args.domain,
            verify_cmd=args.verify,
            runs_root=args.runs_root,
            model=solver_model,
            selector_model=channel_spec.model,
            channel=args.channel,
            timeout=args.timeout,
            allow_mcp=args.allow_mcp,
            sandbox=not args.no_sandbox,
        )
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # The audit trail: every mission, every reply, every harness that came
    # back. Without it a run that produced the wrong thing cannot be explained.
    proposer = Proposer(channel_spec, audit_dir=adapter.harness_dir / "audit")
    library_root = adapter.harness_dir / "library" / f"k{args.k}"

    reporter = progress.Progress(fmt=args.progress)
    with progress.use(reporter):
        result = run_domain(
            args.domain,
            adapter.tasks(),
            args.k,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            library_root=library_root,
            modes=modes,
            # No judge sits behind this path, so the reference executions belong
            # in the selection pool: a composition that regresses must be able to
            # lose to the harness it started from.
            pool_policy="finals+base",
        )

    report = adapter.harness_dir / f"general_{args.mode}_k{args.k}.json"
    report.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")

    # Keep the memory bounded. The run has produced its conclusions -- the
    # library entry and the memory record -- so the transcripts and the
    # evidence trees of older runs can go.
    pruned = prune_runs(adapter.spec.runs_root)
    retired = _compact_library(library_root, adapter, modes)

    rows = candidate_rows(adapter.spec.runs_root, adapter.session_id)
    print()
    print(f"General harness: {result['done']} done, {result['failed']} failed.")
    print()
    print(render_candidate_table(rows))
    print()
    for row in result["tasks"]:
        err = f"  :: {row['error']}" if row.get("error") else ""
        print(f"  {row['task']}: {row['status']}{err}")
    print(f"  runs    : {adapter.spec.runs_root}")
    print(f"  library : {library_root}")
    print(f"  report  : {report}")
    if any(pruned.values()):
        print(f"  pruned  : {pruned}")
    if retired:
        print(f"  library: retired {retired} superseded entries")
    if rows:
        print()
        print(
            f"Next: review the candidates, then accept one with\n"
            f"  harness2-finish {adapter.session_id} --workspace {workspace} --label <candidate>"
        )
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
