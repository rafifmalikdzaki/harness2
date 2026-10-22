# Findings: bottlenecks found, and what was done about them

Every bottleneck this harness hit while being made to work on a real
repository, with the measurement that exposed it, the root cause, and the
change that fixed it. Each finding has an id (`F-nn`) and is cross-referenced
from the [experiment log](GENERAL_HARNESS_EXPERIMENTS.md).

Status is one of:

| status | meaning |
|---|---|
| **fixed** | the change is in the tree and a test pins it |
| **mitigated** | the risk is reduced but not removed; the remainder is stated |
| **open** | known, not yet addressed |

---

## Summary

| area | findings | fixed | mitigated | open |
|---|---|---|---|---|
| A. The general path never ran | 6 | 6 | 0 | 0 |
| B. Isolation | 10 | 7 | 3 | 0 |
| C. Memory and disk | 6 | 5 | 1 | 0 |
| D. The runtime | 6 | 6 | 0 | 0 |
| E. Evidence and grading | 5 | 4 | 0 | 1 |
| F. The second round | 11 | 11 | 0 | 0 |

The two that mattered most were **F-17** (the evidence tree copied the
repository, and then itself, until the disk was full) and **F-11** (the
proposer was unsandboxed, so its relative writes landed in whatever
directory the harness had been launched from).

---

## F. The second round

Round one was measured against a harness whose general path never ran. Once it
ran, a second set of problems showed up — most of them in the seam between
"the improver proposed something" and "the solver did something with it".

**These are the findings that mattered.** F-34 is the one that made the whole
loop vacuous.

### F-34 — the proposed harness never reached the solver

**Measurement.** `GeneralTaskAdapter.execute()` took a `harness_dir` argument
and never used it. The improver's proposal was written to
`runs/<session>/harnesses/<label>/` and stopped there. The solver was called
with the goal, a strategy and the project's files — never with the harness.

**Evidence.** In a real run, `inv1` proposed a 1361-byte `guardrails.md` that
named the exact verification command the agent had to run. The rollout's diff
was indistinguishable from `base1`'s. Every candidate ran identical
instructions; the only thing that varied between them was the strategy string
and the luck of the draw.

**Root cause.** A rewrite of the solver path (`47b7c19`) replaced the
substrate-driven rollout with a direct model call and dropped the harness
staging on the way. Nothing failed: the proposal was still written, still
validated, still recorded as a candidate's harness. The loop looked busy and
measured nothing.

**Solution.** `execute()` now renders the harness into text
(`core/tree.py: render_harness_text`), places it **above** the goal in the
solver's prompt, and stages its scripts into the worktree. Each candidate
records the harness it ran, so a run can be audited after the fact.
**Fixed** — `harness2/adapters/general.py`, `harness2/adapters/agent.py`.

### F-35 — a tripwire for benchmarks aborted every general run

**Measurement.** Two consecutive runs of the general path on a research
repository ended `status: "partial"` with `selections: {}` — no deliverable at
all. The error was:

```
grader verdict would enter model context: 'source/scripts/audit_expanded_goal.py'
contains marker '"passed_count"' at offset 912
```

and, on the second run, the same marker in the candidate's own diff, which the
evidence tree had copied into `pool/<label>/produced.md`.

**Root cause.** The tripwire exists so a benchmark's grader output never
reaches the model. On a general task there is no grader, the verifier's output
*is* the evidence, and the evidence is the user's own repository — in which
`"passed_count"` is an ordinary JSON key.

**Solution.** `BenchSpec` now carries the markers and the policy. Benchmarks
keep the full set and abort; the general path declares none and quarantines a
hit instead of aborting. **Fixed** — `harness2/core/context.py`,
`harness2/core/spec.py`.

### F-36 — a candidate deleted 151 of the project's files, and passed

**Measurement.** One candidate's diff was 1.35 MB: 153 files changed, **151
deletions**. Its verifier exited 0. `apply_to_repo` would have applied it to
the user's working tree.

**Root cause.** The agent ran git commands against the repository it was
working in. Nothing in the harness read the patch before applying it, and a
passing verifier was treated as a safety argument.

**Solution.** `adapters/diffguard.py` reads the patch: files added, modified,
**deleted**, bytes removed. `apply_to_repo` refuses a destructive patch and
says exactly what it would remove; `--allow-destructive` overrides. The guard
is independent of the verifier's verdict. **Fixed** —
`harness2/adapters/worktree.py`.

### F-37 — an apply that did nothing reported that it had applied something

**Measurement.** `git add` and `git diff --cached` were run without checking
their return codes. Either failing produced an empty patch, which the code
treated as "nothing to apply" — and returned success. The caller reported
`applied_to_repository: true` and the changes were gone.

**Solution.** Every git step is checked, and the outcome distinguishes "no
changes" from "applied". **Fixed** — `harness2/adapters/worktree.py`.

### F-38 — `harness2-finish` cleaned the wrong directory and applied the wrong rollout

**Measurement.** It created a `WorktreeManager` with the default temp root
(`/tmp/harness2_worktrees`) while the worktrees live in
`<workspace>/.harness2/worktrees`. It reported "Cleaned and deleted 0
worktrees" and left every one behind, with its `git worktree` registration.
It also applied the **last** rollout rather than the recommended one.

**Solution.** The command looks where the worktrees are, reads the
recommendation out of `selection.json`, and takes `--label` for an override.
**Fixed** — `harness2/cli/finish.py`.

### F-39 — every general rollout was graded as failed

**Measurement.** `GeneralTaskAdapter._verdicts` was declared and never
written. `score()` therefore always returned `n_passed: 0`, and every
candidate was a failure.

**Solution.** The verdict is recorded as the rollout runs and read back out of
the artifact, so a judge in another process sees the same one. A candidate
whose command never ran is ungraded, not a zero. **Fixed** —
`harness2/adapters/general.py`.

### F-40 — one API key could not run the loop

**Measurement.** The solver ran on one provider (an OpenAI-compatible HTTP
client) and the improver on another (the OpenCode CLI, needing a
provider-prefixed model id). The README documented neither. On a machine with
only `INCEPTION_API_KEY` set, the improver could not run at all.

**Solution.** The `http` channel: the improver and the selector run on the
same model service as the solver, and one key runs the whole loop. The
OpenCode channel stays available. **Fixed** — `harness2/core/proposer.py`,
`harness2/core/spec.py`.

### F-41 — a run that takes minutes looked like a hung process

**Measurement.** Nothing was printed between the start of a run and the end.
A $k=2$ run in both modes is tens of minutes of model calls with a blank
terminal, so runs were started in the background and guessed at.

**Solution.** `core/progress.py` prints a line per stage and per rollout, with
elapsed time and spend; JSONL and silent modes too. `--dry-run` prints the
budget before any of it is spent, and the run ends with a table of the
candidates. **Fixed** — `harness2/core/progress.py`, `harness2/cli/general.py`.

### F-42 — the tests made paid calls with the repository's contents

**Measurement.** `tests/test_general_end_to_end.py` claimed to be offline. It
was not: the production path called `llm.chat`, so the suite posted the temp
repository's file contents to a third-party API and billed for it. There was
no `conftest.py`.

**Solution.** `tests/conftest.py` makes every model call fail loudly and
blocks the agent runtimes. The end-to-end test is hermetic and asserts the
thing that matters — that the harness reaches the solver. **Fixed** —
`tests/conftest.py`.

### F-43 — the documentation described a harness that did not exist

**Measurement.** The README said the MCP server exposed 8 tools; it exposed
11. The flagship one, `harness2_recursion`, was not in the table. Several
signatures were wrong. `docs/FINDINGS.md` marked four findings as fixed that
`47b7c19` had reverted. `.env.example` was referenced by the README, the setup
guide, the error messages and `MANIFEST.in` — and did not exist.

**Solution.** The tools table is generated from the server's real signatures,
the stale findings point at the round that reopened them, and `.env.example`
lists every variable the harness reads. **Fixed** — `README.md`,
`docs/FINDINGS.md`, `.env.example`.

### F-44 — a run could not be explained after the fact

**Measurement.** The audit trail — every mission, every reply, every harness
that came back — was only written when a proposer was given an audit
directory, and the general path never did. The general path also recorded no
trajectory at all: the improver and the selector saw a diff and a verdict, and
nothing of the solver's reasoning.

**Solution.** The general path writes the audit trail, and the solver's
reasoning is recorded as a trajectory in the shared grammar. **Fixed** —
`harness2/cli/general.py`, `harness2/adapters/general.py`.

---

## A. The general path never ran

### F-01 — the adapter ran the verification command, not the agent

**Measurement.** `GeneralTaskAdapter.execute()` returned the *verifier's*
output as the deliverable. The agent was never invoked; the diff a user
reviewed was one they had to have written themselves.

**Root cause.** The adapter was written as a stand-in for a benchmark task
with a known answer, and never grew a solver.

**Solution.** The adapter now drives the substrate: materialise the harness
tree, render it into the workspace the way OpenCode loads it
(`AGENTS.md`, `.opencode/{skills,agent,plugin}`, `scripts/`), run the
solver there under a permission fence, and read the event stream back as
the trajectory. **Fixed** — `harness2/adapters/general.py`.

> **Reopened by the second round.** `47b7c19` replaced the substrate-driven
> rollout with a direct model call and dropped the harness staging on the way,
> so the harness stopped reaching the solver altogether. See **F-34**.

### F-02 — no entry point reached the engine

**Measurement.** `make_adapter` knew `jb`, `lab` and `wb`. Nothing reached
`core.schedule.run_domain` for a general task.

**Solution.** `harness2-general` (CLI) and `harness2_recursion` (MCP) both
drive the real engine, so the general path gets the anonymised selection
step and the cross-task library for free. **Fixed** —
`harness2/cli/general.py`, `harness2/mcp_server.py`.

### F-03 — the harness counted as the agent's work

**Measurement.** The improver's `AGENTS.md`, `.opencode/` and `scripts/`
are written into the workspace so OpenCode loads them, so they appeared in
every diff as if the agent had created them.

**Root cause.** The diff was computed over the whole worktree, and nothing
knew which paths the harness had authored.

**Solution.** The launcher records the paths it authored, and
`WorktreeManager.get_diff` excludes exactly those. **Fixed** —
`harness2/adapters/worktree.py`.

> **Reopened by the second round.** The `exclude` argument was never supplied
> in production, and the launcher it depended on was dead. The general path
> now stages the harness under `.harness_staged/` and excludes those paths
> itself. See **F-34**.

### F-04 — worktrees lived in a global temp directory

**Measurement.** `WorktreeManager` defaulted to `/tmp/harness2_worktrees`,
shared by every repository on the machine.

**Root cause.** The benchmark path only ever had one repository at a time.

**Solution.** The general path puts its worktrees under
`<repo>/.harness2/worktrees`, so two repositories cannot collide and the
state sits next to the rest of the project's harness state. **Fixed** —
`harness2/adapters/general.py`.

### F-05 — the channel named a model that does not exist

**Measurement.** Every selection session died with
`rc=1: empty reply`, and behind it: *"Model unavailable:
general/solver"*.

**Root cause.** The scheduler builds the selector's proposer from
`adapter.spec.channels["selector"]`, and the general adapter set that to
the placeholder `"general/solver"`.

**Solution.** The adapter takes the model explicitly, with a separate
selector model, and both the CLI and the MCP tool pass the improver model
through. A test asserts the channel names a servable model, so the
placeholder cannot come back. **Fixed** —
`harness2/adapters/general.py`, `tests/test_general_harness.py`.

### F-06 — the base harness was excluded from the selection pool

**Measurement.** `no candidate produced an artifact`, while the base
rollouts' diffs were 237 B, 193 B and 209 B and every *composition* was
0 B. The proposed harnesses had made the agent produce less than the base.

**Root cause.** The pool was exactly the k composed finals. That is right
for the benchmarks, where a judge arbitrates afterwards — but the general
path has no judge, so the base must be selectable.

**Solution.** `selection_pool` gained `include_base`, and the scheduler
accepts a `"finals+base"` pool policy. The benchmark path keeps `"finals"`
unchanged; the general CLI and the MCP tool opt in. **Fixed** —
`harness2/core/selection.py`, `harness2/core/schedule.py`.

---

## B. Isolation

A rollout is a model-driven program with the operator's authority. Four
routes out of the workspace were open. All four are now closed; three are
closed *and* covered by tests.

### F-07 — the operator's MCP servers were inherited

**Measurement.** A rollout had the user's global MCP servers available:
`github` (with a PAT), `notion`, `playwright`, `context7`.

**Root cause.** OpenCode loads the user's global configuration, and
`OPENCODE_CONFIG_CONTENT` merges over it rather than replacing it.

**Solution.** The configuration document a rollout runs under declares no
servers (`{"mcp": {"servers": {}}}`), which empties the inherited list.
Opt back in with `--allow-mcp`. **Fixed** —
`harness2/substrate/opencode.py`, `tests/test_fence.py`.

### F-08 — Code Mode reached those servers by a second route

**Measurement.** Denying the MCP tools was not enough: the model reached
`tools.opencode.worktree_create` through the `execute` tool (Code Mode).

**Root cause.** Code Mode is a route to every MCP tool by a different
path.

**Solution.** `execute` is denied alongside the servers. **Fixed** —
`harness2/substrate/opencode.py`, `tests/test_fence.py`.

### F-09 — OpenCode's own worktree and session tools

**Measurement.** A stray `fix-calc/` directory appeared **inside the
harness2 repository**, containing a fixed `calc.py` and a copy of the
repository's own `assets/`. `git worktree list` showed ~25 registered
worktrees of the harness2 repo. The agent had used
`opencode.worktree_create` and `opencode.session_move`.

**Root cause.** `worktree_create` operates on the **process cwd's**
repository, not the workspace it was given, so a rollout could register a
worktree of an unrelated project and write into it.

**Solution.** The four OpenCode session and worktree tools are denied by
name, for every rollout — including one that opts into the operator's MCP
servers, because they are OpenCode's own escape hatch rather than one of
the operator's integrations. **Fixed** —
`harness2/substrate/opencode.py`, `tests/test_fence.py`.

### F-10 — a path fence is not a boundary

**Measurement.** With every permission rule in place, a rollout still
escaped:

```
mkdir -p /home/dzakirm/AI-Tools/harness2/escape-proof && echo leaked > ...
→ "Successfully created the directory"
```

and `cat ~/.ssh/id_rsa` returned the operator's **SSH private keys**.
A read-only mount was not containment either: with `--ro-bind / /` the
agent could read every key and config on the machine.

**Root cause.** `external_directory` applies to `read`, `edit`, `write` and
`patch`. It does **not** apply to a shell command that names an absolute
path — OpenCode's own documentation says the shell runs with the host
user's filesystem authority and that directory inference from command text
is best effort.

**Solution.** A filesystem sandbox (`harness2/adapters/sandbox.py`), using
`bwrap`: the system directories read-only, `/tmp` a private tmpfs, the
operator's home an empty tmpfs, and the workspace the one writable place.
Measured against a live model:

| probe | outcome |
|---|---|
| fix the bug, write the test, run pytest | works — `1 passed` |
| `ls /home/dzakirm/.ssh` | *No such file or directory* |
| `mkdir -p <repo>/escape` | *Read-only file system* |

**Mitigated** — the sandbox needs `bwrap`, which needs user namespaces.
Where those are unavailable the fence still applies and the leak check is
the backstop, but a shell command keeps the operator's filesystem
authority. **Mitigated** — the sandbox does not unshare the network: a
rollout can still reach any host the operator can. That is deliberate, or
the model call could not happen.

### F-11 — the proposer was the leak, not the solver

**Measurement.** `MISSION.md`, `context/` and `output/answer.md` appeared
in the harness2 repository. The leaked `output/answer.md` contained
literally `base1` — the *selector's* answer.

**Root cause.** Not the fence. The installed OpenCode resolves a **relative**
path against the **process cwd**, not against the directory it is given:

| process cwd | positional directory | relative write landed in |
|---|---|---|
| `/tmp` | `/tmp/harness2-prop-T` | `/tmp` |
| the repository | the scratch directory | the repository |

**Solution.** The proposer is sandboxed exactly like the solver. The
sandboxed proposer reads its context, answers correctly
(` ```choice\nA\n``` `), and leaks nothing. **Fixed** —
`harness2/core/proposer.py`.

### F-12 — a relative path lands in the process cwd

**Measurement.** The same table as F-11. It is a property of the runtime,
not of the harness.

**Root cause.** OpenCode 2.1.0 resolves relative paths against the
process cwd. Every caller must therefore run the process with its working
directory set to the workspace it means to write in.

**Solution.** The sandbox does this with `--chdir`; the unsandboxed paths
rely on `subprocess.run(cwd=...)`. **Mitigated** — correct today, but easy
to lose in a new call site; the experiment log records it as an open
question.

### F-13 — the rollout could not read the operator's home

**Measurement.** `ls ~/.ssh` inside the sandbox → *No such file or
directory*.

**Root cause.** The sandbox hides the operator's home on purpose.

**Solution.** Nothing to do — this is the intended behaviour, recorded here
because it also *broke* legitimate work (F-20). **Fixed**.

### F-14 — the sandbox hid the project's own virtualenv

**Measurement.** `.venv/bin/python: No such file or directory` inside the
sandbox. The rollout could not run the project's tests at all.

**Root cause.** The project's virtualenv is under the operator's home,
which the sandbox replaces with an empty tmpfs. Worse, the interpreter
behind it is usually a **uv or pyenv build outside the project entirely**
— here a symlink into `~/.local/share/uv/python/...` — and it needs its
whole prefix, not just `bin/`, to find its standard library.

**Solution.** The sandbox is told what to expose read-only: the project's
checkout, its virtualenv (presented at the path a worktree expects to find
it), and the interpreter prefix behind it. **Fixed** —
`harness2/adapters/sandbox.py`, `harness2/adapters/general.py`.

### F-15 — the user's home was readable through the *runtime*, not the fence

**Measurement.** A rollout in the unsandboxed proposer path could read
`~/.config/opencode/opencode.json`, which holds a GitHub PAT.

**Root cause.** The credential store is the operator's, and the process
runs as the operator.

**Solution.** The sandbox's empty-home tmpfs, plus the credential store
being bound in only where OpenCode needs it to make the model call.
**Mitigated** — inside the sandbox the state directory is bound read-write
because OpenCode writes its session database there.

### F-33 — host credentials were still inherited through the environment

**Measurement.** A sandboxed rollout could enumerate host environment
variable names matching `KEY|TOKEN|SECRET|PASSWORD` and saw
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `MLFLOW_TRACKING_PASSWORD`,
and `OPENAI_API_KEY`. Hiding the home directory did not hide values already
copied into the process environment.

**Root cause.** `sandbox_env` started by copying all of `os.environ` and
overriding `HOME`. The shell tool inherits the OpenCode process environment,
so a path sandbox alone did not protect env-injected credentials.

**Solution.** Sandboxed processes now receive an allowlist of runtime-safe
variables (PATH, locale, selected CUDA/thread/W&B settings, and the
generated OpenCode config). Names containing `KEY`, `TOKEN`, `SECRET`,
`PASSWORD`, `PASSWD`, `CREDENTIAL`, or `AUTH` are not passed. OpenCode
authenticates from its mounted state database; a Mercury 3 model-call smoke
test still passed after filtering. **Fixed** — `harness2/adapters/sandbox.py`,
`tests/test_sandbox.py`.

---

## C. Memory and disk

### F-16 — a verification command was worth nothing

**Measurement.** Two separate defects.

1. `score()` looked for the string `(no failures)` in the verifier's
   output. **No test runner prints that string**, so every rollout scored
   zero, whatever it did.
2. The verifier's output never reached the selector at all. A candidate
   that passed and one that failed looked identical in a diff.

**Root cause.** The score guessed at the output format instead of using
the exit status, and the evidence tree carried the diff but not the
verdict.

**Solution.** The verdict is the verification command's **exit status**,
recorded when the rollout ran. The output travels with the candidate as an
asset, and the run's `test_output.txt` opens with an explicit
`VERIFICATION: PASSED/FAILED` line, because with `-q` a test runner may
print no summary of its own. **Fixed** —
`harness2/adapters/general.py`, `tests/test_general_evidence.py`.

> **Reopened by the second round.** The status was recorded in a dict that
> nothing ever wrote to, so every general rollout still scored zero. It is now
> recorded as the rollout runs and read back out of the artifact. See
> **F-39**.

### F-17 — the evidence tree copied the repository, and then itself

**Measurement.** The worst bottleneck found. On the `histodae` repository
(78 GB of working tree, 1.8 GB of `.git`):

* the run duplicated **310 GB** and filled the disk to **99%** (31 GB
  free);
* after recovery the same run's artifacts were **116 MB for a toy
  repository**.

**Root cause.** Two bugs compounding.

1. The general path handed the evidence builder the whole repository as
   the task's source material. The benchmarks never hit this because
   their task material is a small data room — but the general path's task
   *is* the repository.
2. The evidence tree lives **inside** the repository
   (`.harness2/runs/...`), so `copy_source` copied the tree *including the
   copy it was making*. The recursion is visible in the artifact tree:

   ```
   selection/agg2/source/.harness2/runs/.../selection/agg2/source/.harness2/...
   ```

   Every stage copied the previous stage's copy. Exponential, not linear.

**Solution.** Two independent guards.

* The source is now a **bounded room**: the repository's tracked *text*
  files, prepared once per session. On `histodae` that is **229 files,
  1.5 MB** instead of 78 GB, built in 0.1 s.
* `copy_source` itself is **capped** — 1 MB per file, 64 MB in total — so
  no adapter can do this again, whatever it hands over.

The recursion is also impossible now because the room is built from
`git ls-files`, and the state directory is not tracked. **Fixed** —
`harness2/adapters/general.py`, `harness2/core/context.py`.

### F-18 — the same tree was copied four times

**Measurement.** In one run, the evidence existed in four places:

| copy | size |
|---|---|
| `context/<stage>/source` | 19 MB |
| `selection/<mode>/source` | 20 MB |
| `reflection/<mode>/source` | 39 MB |
| `reflection/<mode>.experience/source` | 40 MB |

**Root cause.** Each stage builds its own evidence tree, and the selection
and reflection stages copy the previous stage's tree again.

**Solution.** The copy is now bounded (F-17), and the compression below
shrinks what is copied. **Mitigated** — the duplication still exists; what
changed is that each copy is small.

### F-19 — trajectories were stored raw

**Measurement.** The OpenCode event stream is written verbatim as
`trajectory.jsonl`: 250–370 KB per rollout on a real repository, and it is
copied into every evidence tree that references the rollout.

**Root cause.** The runner wrote the raw stream and nothing compressed it.

**Solution.** Transcripts are stored gzipped (`trajectory.jsonl.gz`), and
the readers are transparent about it: a path ending in `.gz` is
decompressed, and one that does not is read as-is, so a directory written
by an older version still reads. Measured on a live run: **4.4x** smaller
(71 KB → 16 KB), and the evidence tree still renders the trajectory
verbatim. **Fixed** — `harness2/core/compress.py`,
`harness2/adapters/general_runner.py`.

### F-20 — the SQLite memory duplicated the run directory

**Measurement.** The `rollouts` table stores the full `git_diff` and
`test_output` inline, while the same content also exists as files in the
run directory.

**Root cause.** The record was written for convenience.

**Solution.** The record stores the large fields compressed, marked with a
prefix so a reader knows, and decompressed transparently on the way out. An
older plain row still reads. Measured with a realistic 11 KB diff: a
**4 KB** database file. **Fixed** — `harness2/core/memory_db.py`,
`harness2/core/compress.py`.

### F-21 — the library grew without bound

**Measurement.** One entry per task, each carrying a full harness tree
and its evidence, plus an `INDEX.md` rebuilt from disk every time.

**Root cause.** Nothing ever retired an entry.

**Solution.** Two mechanisms.

* **Compaction.** Two entries with the same *cue* are the same lesson
  found twice; the later one is kept, because it has seen more evidence,
  and the earlier one is retired. Distinct cues are all kept, up to a
  limit.
* **Retention.** A policy for the run directory: the recent runs keep
  their evidence trees and their transcripts, older runs keep their
  conclusions and lose the rest, and runs beyond the limit are dropped
  entirely. What is never pruned is the library, the memory database, and
  the report of the most recent run.

The policy is applied at the end of every general run, and is also
reachable directly through the `harness2_memory_status` MCP tool, which
reports the sizes before and after and what was removed. **Fixed** —
`harness2/core/library.py`, `harness2/core/retention.py`,
`harness2/mcp_server.py`.

---

## D. The runtime

### F-22 — the pinned OpenCode checkout was never fetched

**Measurement.** The general path could not run at all: the pinned
checkout (`dependencies/opencode`, ~1 GB) was absent, and the Proposer's
argv had **no fallback whatsoever** — the first selection session failed
with *"no OpenCode checkout"*.

**Root cause.** The Proposer and the solver each built their own argv.

**Solution.** Both now build their command from one place,
`substrate.opencode.opencode_command`. The pinned checkout still wins when
present — it is the build the paper's numbers came from — and an installed
`opencode` binary is used otherwise. **Fixed** —
`harness2/substrate/opencode.py`.

### F-23 — the managed service held a fixed port

**Measurement.** `Managed service port 49374 on 127.0.0.1 is already in
use by another process` — because a session was already running.

**Root cause.** OpenCode 2.1.0 talks to a managed background service on a
fixed port; the pinned source build ran a private server.

**Solution.** `--standalone`, giving every rollout a private server.
**Fixed** — `harness2/substrate/opencode.py`.

### F-24 — redirecting XDG_DATA_HOME locked the provider out

**Measurement.** `Model unavailable: mistral/mistral-large-latest` when
the Proposer isolated its session state.

**Root cause.** V1 kept credentials in `auth.json`; **V2 keeps them in the
SQLite `opencode.db`** under `XDG_DATA_HOME`. Redirecting that directory
away from the operator's took the credentials with it. The 5 GB database
cannot be copied per session.

**Solution.** The redirect is applied only to the self-contained source
build. The installed build uses the operator's real state directory.
**Fixed** — `harness2/core/proposer.py`.

### F-25 — a reasoning effort was passed as a model variant

**Measurement.** `Variant unavailable for mistral/mistral-large-latest:
high`, and then `reasoning_effort is not enabled for this model`.

**Root cause.** The Proposer's `effort` field is a *reasoning* effort. The
pinned build took it as a model variant; V2 rejects both the variant and
the effort for models that do not offer them.

**Solution.** Neither is forced on the binary path. A caller who wants a
named variant asks for it in the model id (`provider/model#variant`).
**Fixed** — `harness2/substrate/opencode.py`.

### F-26 — `--dir` does not exist in OpenCode 2.1.0

**Measurement.** The flag is documented nowhere in 2.1.0 and is silently
ignored: the directory is a **top-level positional**, before the message.

**Root cause.** The pinned build's CLI shape was carried over.

**Solution.** The binary path builds its argv for its own CLI shape.
**Fixed** — `harness2/substrate/opencode.py`.

### F-27 — a worktree is not a checkout

**Measurement.** The verifier reported PASSED, and it did pass — but it ran
**161 passed, 4 skipped** in the worktree against **166 passed, 1
skipped** in the user's own checkout. Three tests need the *data
checkout*, which is untracked and 78 GB of it, so a worktree cannot carry
it.

**Root cause.** A worktree holds the repository's tracked files, and
nothing else.

**Solution.** Nothing yet — the exit status is still zero, so a passing
verification does not mean the user's own checkout would pass the same
way. **Open** — a path with data to spare should either bind the data
into the worktree or say so in the report.

---

## E. Evidence and grading

### F-28 — the self-evolution engine emitted templated non-lessons

**Measurement.** Every lesson was the same string with the goal
interpolated: *"Adopt contrastive changes observed in winning rollout X
over Y"* — which is not a lesson.

**Root cause.** The engine was written to produce *a* lesson, and never
looked at the diffs.

**Solution.** Lessons, facts and skills are derived from the diffs the run
actually produced. The engine now names the files the winning rollout
touched, and says *"verdict: no observed effect"* when an edit changed
nothing. **Fixed** — `harness2/core/evolution.py`.

### F-29 — a selector's answer could be discarded

**Measurement.** `chose 'base1', which is not one of A, B` — the run
failed after the selector had made a perfectly good choice.

**Root cause.** The selector answers with an anonymous id. With the
reference executions in the pool their labels (`base1`) are short and
guessable, so a model reaching for a label rather than an id is likelier.

**Solution.** The parser accepts a label that is in the pool and maps it
back to its id, rather than throwing away a decision the model did make.
**Fixed** — `harness2/core/selection.py`, `tests/test_selection_choice.py`.

### F-30 — the renderers lived in a benchmark

**Measurement.** The tree-rendering helpers — how a harness tree becomes
`AGENTS.md`, `.opencode/skills/`, `.opencode/agent/` — were private to
`benchmarks/jobbench.py`.

**Root cause.** They were written for one benchmark and generalised later.

**Solution.** They moved to `substrate/render.py`, where they belong: they
are a property of the OpenCode *surface*, not of a task set. Both paths now
render identically. **Fixed** — `harness2/substrate/render.py`.

### F-31 — no leak was detected after the fact

**Measurement.** Nothing noticed that a rollout had written outside its
workspace. The stray `fix-calc/` was found by `git status`, days later.

**Root cause.** The fence was the only check, and it is best-effort by
design.

**Solution.** `GeneralTaskAdapter._detect_leaks` compares the user's
working tree before and after the rollout, and inspects the worktree
registry for entries the rollout added. A leak is reported in
`<run>/leaks.txt`. **Fixed** — `harness2/adapters/general.py`.

> **Reopened by the second round.** `_detect_leaks` was removed with the
> substrate-driven rollout and never came back. The leak detection is worth
> restoring; until then the `harness2-finish` guard (see **F-36**) is what
> stands between a candidate and the repository.
### F-32 — the training script was rewritten twice

**Measurement.** Not a bottleneck but a cost: the training script went
875 → 374 lines and the analyzer 705 → 155 during the earlier
canonicalisation.

**Root cause.** God modules.

**Solution.** The canonical `src/histodae/<layer>/` namespace, with the
rendering helpers and the module-independence test guarding it.
**Fixed** — see the `histodae` repository.

---

## Compression

The memory system had three parts, and each grew without bound:

| part | what it stores | bound |
|---|---|---|
| the **run directory** | transcripts, diffs, evidence trees | transcripts gzipped (4.4x); retention by count and age |
| the **SQLite memory** | sessions, rollouts, facts, lessons | the large fields compressed; facts deduplicated by `(category, fact)` |
| the **experience library** | one entry per task, each with a harness tree | entries with a superseded cue retired; the index rebuilt from a compact summary |

The rule throughout: **the memory keeps the conclusions, not the
transcripts.** A trajectory is compressed and kept for audit; a lesson is a
sentence; a fact is a line. What the harness reads back into the next run's
harness is the *lesson* and the *fact* — a few hundred bytes — not the
evidence that produced them.

The policy is applied at the end of every general run, and can be applied
or inspected directly:

```bash
# through the CLI: the policy runs at the end of every general run
harness2-general --workspace <repo> --goal "..."

# through MCP, from any agent
harness2_memory_status(workspace="<repo>", prune=true)
```

`harness2_memory_status` reports the size of each part before and after
and what was removed, so the memory can be watched rather than guessed at.

---

## Reproducing the measurements

Every number above came from a run that can be repeated:

```bash
export HARNESS2_SOLVER_MODEL=inception/mercury-3
export HARNESS2_IMPROVER_MODEL=inception/mercury-3

harness2-general --workspace <repo> --goal "<the goal>" \
  --k 2 --mode parallel --verify "<the verification command>"
```

The escape probes and the sandbox behaviour are in
[the experiment log](GENERAL_HARNESS_EXPERIMENTS.md); the disk blow-up
(F-17) is reproducible by pointing the general path at a repository with a
large working tree and watching `.harness2/runs`.
