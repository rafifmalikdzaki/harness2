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
  * probe them by running OpenCode in an isolated worktree,
  * compose the best contrasts into a refined harness, and
  * select a winner by reading the candidates, without a ground-truth verifier.

    harness2-general --workspace ~/Research/histodae \\
        --goal "add a --config-path flag that accepts a packaged config name" \\
        --k 2 --mode parallel

The verification command is optional. With one, the rollout is also graded by
its exit status; without one, the selection is made purely on the contrast
between candidates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import config
from ..adapters.general import GeneralTaskAdapter
from ..core.proposer import Proposer
from ..core.schedule import mode_token, run_domain
from ..core.spec import resolve_channel_model


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, required=True, help="Git repository to work in")
    parser.add_argument("--goal", required=True, help="What the agent should accomplish")
    parser.add_argument("--domain", default="general", help="Domain label for the experience library")
    parser.add_argument("--k", type=int, default=2, help="Task-level recursion budget")
    parser.add_argument("--mode", choices=("parallel", "sequential", "both"), default="parallel")
    parser.add_argument("--verify", default=None, help="Verification command run after each rollout")
    parser.add_argument("--model", default=None, help="Solver model (default: HARNESS2_SOLVER_MODEL)")
    parser.add_argument("--improver-model", default=None, help="Proposer/selector model (default: HARNESS2_IMPROVER_MODEL)")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-rollout timeout in seconds")
    parser.add_argument("--runs-root", type=Path, default=None, help="Where run artifacts go (default: <workspace>/.harness2/runs)")
    args = parser.parse_args(argv)

    if args.k < 1:
        parser.error("--k must be positive")

    workspace = args.workspace.expanduser().resolve()
    if not (workspace / ".git").exists():
        parser.error(f"--workspace {workspace} is not a git repository")

    solver_model = (args.model or config.SOLVER_MODEL_OC).strip()
    if not solver_model:
        parser.error(
            "no solver model: set HARNESS2_SOLVER_MODEL or pass --model "
            "(the opencode channel needs a provider-prefixed id, e.g. anthropic/claude-sonnet-4-5)"
        )

    improver_model = (args.improver_model or config.IMPROVER_MODEL).strip()
    if not improver_model:
        parser.error(
            "no improver model: set HARNESS2_IMPROVER_MODEL or pass --improver-model"
        )

    modes = ("agg", "seq") if args.mode == "both" else (mode_token(args.mode),)

    adapter = GeneralTaskAdapter(
        repo_root=workspace,
        goal=args.goal,
        domain=args.domain,
        verify_cmd=args.verify,
        runs_root=args.runs_root,
        model=solver_model,
        selector_model=improver_model,
        timeout=args.timeout,
    )

    channel = resolve_channel_model("opencode", improver_model)
    proposer = Proposer(channel)

    library_root = adapter.harness_dir / "library" / f"k{args.k}"
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

    print(f"General harness: {result['done']} done, {result['failed']} failed.")
    print(f"  runs    : {adapter.spec.runs_root}")
    print(f"  library : {library_root}")
    print(f"  report  : {report}")
    for row in result["tasks"]:
        print(f"  - {row['task']}: {row['status']}")
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
