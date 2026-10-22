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

"""Check one installed benchmark before running adaptation and judging."""

from __future__ import annotations

import argparse
import sys

from harness2.cli._common import make_adapter
from harness2.preflight import check


def main(argv=None):
    p = argparse.ArgumentParser(prog="harness2-check", description=__doc__)
    p.add_argument(
        "--bench", choices=("jb", "lab", "wb"), required=True, help="Which benchmark to check"
    )
    p.add_argument(
        "--subset",
        choices=("office", "web", "code"),
        help="wb only: which subset to check",
    )
    p.add_argument(
        "--substrate",
        choices=("oc", "cx"),
        default="oc",
        help="Which agent runtime to check",
    )
    args = p.parse_args(argv)
    try:
        adapter = make_adapter(args.bench, args.subset, args.substrate)
        errors = check(adapter, judge=True)
    except (OSError, RuntimeError, ValueError) as exc:
        errors = [str(exc)]
    # Failures go to stderr: in a pipeline they must not be mistaken for the
    # command's normal output.
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        print(
            f"Setup incomplete: {len(errors)} issue(s). See docs/BENCHMARKS.md.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Ready: {adapter.spec.name}/{args.substrate}; {len(adapter.tasks())} tasks. "
        "Model access is not exercised; the first real run is the first model call."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
