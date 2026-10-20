# Experiment log: the general task harness

This is the working record of building and exercising Harness²'s **general
task path** — running the real Propose–Probe–Compose loop on an arbitrary
repository and goal, with no benchmark, no dataset and no judge.

Each entry records what was tried, what happened, and what was concluded. The
failures are the useful part: every one of them turned into a change in the
code, and the change is named at the end of the entry.

---

## 0. Starting point

The repository had a `GeneralTaskAdapter`, an MCP tool `harness2_solve`, and
a `harness2-general` console script. None of them ran the loop.

`GeneralTaskAdapter.execute()` ran the *verification command* and returned
its output as the "deliverable". The agent was never invoked. The diff the
user reviewed was one they had to have written themselves. There was also no
CLI or MCP entry point that reached `core.schedule.run_domain` — `make_adapter`
only knew `jb`, `lab` and `wb`.

The engine itself (proposer, worktree isolation, anonymised selection,
cross-task library) was sound and was reused unchanged.

**Goal:** make the general path drive the same engine the benchmarks use, on
any repository, with evidence a reviewer can check.

---

## 1. Environment

| | |
|---|---|
| Repo | `/home/dzakirm/AI-Tools/harness2`, branch `feat/general-harness` |
| Python | 3.12, `.venv`, `uv pip install -e . --no-deps` |
| OpenCode | 2.1.0 installed at `~/.opencode/bin/opencode` (no pinned source checkout) |
| Model | `mistral/mistral-large-latest` for the early runs, `inception/mercury-3` for the final one |
| Sandbox | `bwrap` (bubblewrap) at `/usr/bin/bwrap`, kernel 7.2.11 |
| Task | A one-function repo: `calc.py` with `add()` subtracting, plus a request to write `test_calc.py` and a `--verify "python3 -m pytest -q test_calc.py"` gate |

The OpenCode Console key was expired and the Anthropic account had no
credit, so the early runs used Mistral. It is a weaker improver than the
paper's models, which turned out to matter (entry 9).

---

## 2. Experiment 1 — the general path, wired to the engine

**Change.** `GeneralTaskAdapter` was rewritten to use the substrate properly:
materialise the harness tree, render it into the workspace the way OpenCode
loads it (`AGENTS.md`, `.opencode/{skills,agent,plugin}`, `scripts/`), run the
solver there, and read the event stream back as the trajectory. The
`harness2-general` CLI and the `harness2_recursion` MCP tool were added, both
driving `schedule.run_domain`.

Two details mattered:

* **The harness is scaffolding, not the work.** The improver's `AGENTS.md`,
  `.opencode/` and `scripts/` are written into the workspace so OpenCode
  loads them, but they must not read as the agent's changes. The launcher
  records the paths it authored, and `WorktreeManager.get_diff` excludes
  exactly those.
* **The tree-rendering helpers moved** from `benchmarks/jobbench.py` to
  `substrate/render.py`. They are a property of the OpenCode surface, not of
  a task set, and both paths now render identically.

**Result.** 22 tests pass, including an end-to-end test that drives the whole
engine against a real repository with a stub solver at the process boundary
— no model call, no network, but the production code path throughout.

**Live run** (`live`, k=1, parallel): the solver fixed the bug correctly
(`a - b` → `a + b`) and wrote `test_calc.py`. The verifier did not run, so
nothing checked the result — and the test the agent wrote was **broken**: it
referenced `add` without importing it, and failed under pytest with
`NameError`.

**Conclusion.** The loop works. But a general task needs a way to *check* the
result, and the k=1 selection is trivial: with one final there is nothing to
choose between.

---

## 3. Experiment 2 — making the loop exercisable

The pinned OpenCode checkout (`dependencies/opencode`, ~1 GB) was not
fetched, so the general path could not run at all.

**Finding.** The Proposer and the solver each built their own argv, and the
Proposer's version had no fallback whatsoever — the general path failed at
the first selection session with *"no OpenCode checkout"*.

**Change.** Both now build their command from one place,
`substrate.opencode.opencode_command`. The pinned checkout still wins when
present (it is the build the paper's numbers came from); otherwise an
installed `opencode` binary on `PATH` is used.

The two builds differ in more than the path:

| | pinned source | installed binary |
|---|---|---|
| working directory | `--dir <workspace>` flag | positional, **before** the message |
| permission fence | `OPENCODE_PERMISSION` env var (V1) | `OPENCODE_CONFIG_CONTENT` (V2) |
| variant / effort | `--variant <effort>` flag | rejected by most models |

**Result.** `--dir` does not exist in 2.1.0 (the directory is a top-level
positional), and the proposer's "effort" is a *reasoning* effort that the
pinned build took as a model variant. V2 rejects both the variant and the
effort for models that do not offer them, so neither is forced; a caller who
wants a named variant asks for it in the model id (`provider/model#variant`).

**Conclusion.** The general path now runs on the OpenCode a developer
actually has.

---

## 4. Experiment 3 — the selector could not run

**Symptom.** Every selection session died with `rc=1: empty reply`.

**Diagnosis, in order:**

1. The installed build talks to a **managed service on a fixed port (49374)**,
   which was already taken by a running session. Fixed with `--standalone`,
   which gives every rollout a private server.
2. The Proposer redirected `XDG_DATA_HOME` so each session's state was
   disposable. V2 keeps its **credentials in the SQLite `opencode.db`** under
   that directory, so the redirect locked the provider out entirely —
   *"Model unavailable"*. The redirect now applies only to the self-contained
   source build.
3. The real cause: `adapter.spec.channels["selector"]` was set to the
   placeholder **`"general/solver"`**. The scheduler builds the selector's
   proposer from that channel, so the selection session asked for a model
   that does not exist. The adapter now takes the model explicitly, with a
   separate selector model, and both the CLI and the MCP tool pass the
   improver model through.

**Conclusion.** The loop cannot choose between candidates without a real
model on the selector channel. Two of these three defects were invisible
without a live run.

---

## 5. Experiment 4 — the pool excluded the base

**Symptom.** `live5`: *"no candidate produced an artifact"*.

**Diagnosis.** The selection pool was exactly the k composed finals, and the
reference executions were excluded from it. The rollout diffs showed:

```
base1  237B     base2  193B     dom2  209B      <- the base harness solves it
inv1     0B     inv2     0B     dom1    0B      <- every composition regressed
agg1     0B     agg2     0B
```

Every proposed harness made the agent do *less* than the base. With the base
excluded from the pool, the pool came back empty and the run finished with
nothing — even though the base harness had produced the correct fix.

That exclusion is right for the benchmarks, where a judge arbitrates
afterwards. The general path has **no judge**: selection is the only
arbitration. So the base must be selectable.

**Change.** `selection_pool` gained `include_base`, and the scheduler accepts
a `"finals+base"` pool policy. The benchmark path keeps `"finals"` unchanged;
the general CLI and the MCP tool opt in.

**Result.** `live6` (k=2, parallel, with a verifier): **ok**. Eight rollouts
in isolated worktrees, the solver fixed the bug and wrote a test that passes
under the verifier (`1 passed`), the selector chose `agg1` from three
candidates with a reason, and the library recorded an evidence-backed lesson:

> *Candidates that moved the session to the worktree directory before
> editing succeeded, and created `test_calc.py`. Candidates that did not
> attempt this step failed to read the file due to permission errors.*

The repository itself was left untouched; the result sat in the worktree for
review.

---

## 6. Experiment 5 — a rollout escaped the workspace

**Symptom.** A stray `fix-calc/` directory appeared **inside the harness2
repository**, containing a fixed `calc.py` and a copy of the repository's own
`assets/`. It was picked up by `git add -A` and committed.

**Diagnosis.** The mechanism was not the one assumed. The path fence did its
job — a probe confirmed a write to `/home/dzakirm/AI-Tools/harness2/` was
refused with *"permission restrictions"*. The escape came from elsewhere:

* the rollout had access to **OpenCode's own MCP tools**, and used
  `opencode.worktree_create` and `opencode.session_move`;
* `worktree_create` operates on **the process cwd's repository**, so it
  registered worktrees *of the harness2 repo* — one of them landing inside
  the repo directory;
* the rollout also inherited the **operator's global MCP servers** —
  `github` (with a PAT), `notion`, `playwright`, `context7` — because
  OpenCode loads the user's global configuration.

**What was measured.** Three distinct routes out of the workspace:

| route | blocked by |
|---|---|
| file tools (`read`/`edit`/`write`) outside the workspace | `external_directory: deny` |
| the operator's MCP servers | `mcp.servers: {}` in the config document |
| Code Mode (`execute`), which reaches MCP tools by a second route | `execute: deny` |
| OpenCode's own `worktree_create` / `session_move` | `opencode_worktree_create: deny`, `opencode_session_move: deny` |

**Change.** The V2 fence became a single builder, `substrate.opencode._fence_v2`,
and now denies all four. `allow_mcp` opts back into the operator's servers —
but never into OpenCode's own session and worktree tools, which are an escape
hatch rather than one of the operator's integrations.

**Result.** `live7` — the fence held for the file tools, and every rollout
produced an empty diff. Cause: `bwrap: execvp /home/dzakirm/.opencode/bin/opencode:
No such file or directory`. The sandbox (below) hid the directory the binary
lives in.

---

## 7. Experiment 6 — the fence is not a boundary

**The uncomfortable finding.** With only the permission rules in place, a
rollout still escaped:

```
mkdir -p /home/dzakirm/AI-Tools/harness2/escape-proof && echo leaked > ...
→ "Successfully created the directory"
```

`external_directory` applies to `read`, `edit`, `write` and `patch`. It does
**not** apply to a shell command that names an absolute path — the V2
documentation is explicit that the shell runs with the host user's
filesystem, process and network authority, and that directory inference from
command text is best effort.

Two further probes measured how far that went:

* `cd /home/dzakirm && mkdir -p x` — the `cd` is not caught either.
* `cat ~/.ssh/id_rsa` — the operator's **SSH private keys are readable**.

A read-only mount is not containment either: with `--ro-bind / /` the agent
could still *read* every key and config on the machine, and could exfiltrate
them through the model provider.

**Change.** A filesystem sandbox, `harness2/adapters/sandbox.py`, using
`bwrap`. The filesystem is assembled from scratch:

* `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`, `/var` — bound
  read-only, because the agent needs a shell, a Python and a git;
* `/tmp` — a private tmpfs;
* the operator's **home** — an empty tmpfs, so it does not exist;
* OpenCode's binary directory and its state directory — bound back in, so
  the model call can authenticate;
* the **workspace** — the one writable place.

Network access is deliberately *not* unshared: the rollout has to reach the
model provider.

**Result.** Measured against a live model:

| probe | outcome |
|---|---|
| fix `calc.py`, write `test_calc.py`, run pytest | works — `1 passed` |
| `ls /home/dzakirm/.ssh` | *No such file or directory* |
| `mkdir -p /home/dzakirm/AI-Tools/harness2/<x>` | *Read-only file system* |

The agent cannot see the operator's keys, cannot write outside its
workspace, and cannot see the operator's home at all.

**And still.** The fence is not enough on its own, so the rollout is also
**checked afterwards**: `GeneralTaskAdapter._detect_leaks` compares the
user's working tree before and after the rollout, and inspects the worktree
registry for entries the rollout added. A leak is reported in
`<run>/leaks.txt` rather than discovered later.

---

## 8. Experiment 7 — the proposer was the leak, not the solver

With the sandbox in place, the solver was safe: a relative write landed in
its workspace, and the harness2 repository was untouched. But `live8` still
left `MISSION.md`, `context/` and `output/answer.md` in the repository.

**Diagnosis.** The writer was not the solver. It was the **proposer** -- the
session that proposes harness edits -- and the mechanism was not the fence
either:

```
$ opencode run --standalone ... /tmp/harness2-prop-XXXX "write rel.txt"
Created file successfully: /tmp/rel.txt
```

The installed build resolves a **relative** path against the *process* cwd,
not against the directory passed as the positional argument. The proposer
runs with its working directory in a scratch directory, but it was launched
from the repository, so a relative write landed in the repository. The
evidence: the leaked `output/answer.md` contained literally `base1` -- the
selector's answer -- and the leaked `context/` held the candidate pool the
selector was choosing from.

The same behaviour, confirmed by isolating the variable:

| process cwd | positional directory | relative write landed in |
|---|---|---|
| `/tmp` | `/tmp/harness2-prop-T` | `/tmp` |
| the repository | the scratch directory | the repository |

**Change.** The proposer is sandboxed too, exactly like the solver. It has
to be: it reads a context tree and writes an answer, and both are relative
writes.

**Result.** The sandboxed proposer reads its context, answers correctly
(` ```choice\nA\n``` `), and the repository stays clean.

A second defect surfaced in the same run: the selector answered `base1`
where an anonymous id was expected, and the choice was thrown away. The
reason is structural -- with the reference executions in the pool their
labels are short and guessable, so a model reaching for a label rather than
an id is likelier. The parser now accepts a label that is in the pool and
maps it back to its id, rather than discarding a decision the model did
make.

---

## 9. Experiment 8 — a weak improver regresses the harness

With a stronger model this is the effect the paper measures. With Mistral it
is the opposite, and the loop showed it plainly: the base harness solved the
task and **every** proposed harness made the agent produce nothing.

This is a finding about the improver, not a bug in the loop -- and it is
exactly why the base belongs in the selection pool (entry 5). With the base
selectable, a regressed composition loses to the harness it started from,
which is the honest outcome, and the library records it.

The V2 fence also changed the agent's environment materially: the MCP
servers it had been relying on were gone, and Code Mode with them. The
`live6` lesson -- *"candidates that moved the session to the worktree
directory before editing succeeded; the others failed on permission
errors"* -- is a lesson about that, learned from real contrasts between
winning and losing rollouts.

**Repeating the run with `inception/mercury-3`** (a stronger improver) made
the difference visible. `live10`, launched from `/tmp` so that the working
directory could not be the repository:

* all six stages produced a harness, none fell back;
* every candidate produced the **same correct fix** -- `return a - b` ->
  `return a + b`, plus a `test_calc.py` that passes under the verifier
  (`1 passed`);
* the selector chose `agg2` from four candidates and said why: *"All four
  candidates are byte-identical in the artifact content ... they differ only
  in the label comment. The fix is correct and consistent with the source,
  so no criterion distinguishes them."*;
* the repository was untouched, and nothing leaked into it.

The library entry is worth reading in full, because it shows the
self-evolution engine being honest. For each placeholder component the
improver added, it recorded:

> *No rollout loads or references this component. Base harness (without it)
> and all six candidates produce the identical trajectory ... No divergence
> anywhere; the bundle change had no observable effect on execution or
> output.* -- **verdict: confounded**

The earlier version of that engine emitted the same template with the goal
interpolated ("Avoid common failure modes seen in early probes"). This one
names what it observed and admits when an edit did nothing.

---

## 10. What changed, in order

| commit | change |
|---|---|
| `8cef354` | the general path drives the real engine; CLI and MCP entry points; the harness is excluded from the deliverable; render helpers moved to `substrate/render.py`; the self-evolution engine derives its lessons from the actual diffs |
| `8094af9` | OpenCode runs from an installed binary when the pinned checkout is absent |
| `3012e6d` | the installed build is usable: `--standalone`, no credential-losing XDG redirect, no forced variant |
| `d3d828c` | the general path's channel names a real model |
| `ba74f1b` | the selection pool can include the base, for paths with no judge behind them |
| `f5f3522` | the artifacts that escaped into this repository are removed |
| — | the fence: the inherited MCP servers are dropped, Code Mode and OpenCode's session and worktree tools are denied, the rollout runs in a filesystem sandbox, and leaks are detected after the fact |
| — | the proposer is sandboxed too, and the selector's answer may name a label |

---

## 11. Reproducing it

```bash
export HARNESS2_SOLVER_MODEL=inception/mercury-3
export HARNESS2_IMPROVER_MODEL=inception/mercury-3

harness2-general --workspace /path/to/a/repo \
  --goal "calc.py has a bug: add() subtracts. Fix it so add() adds, and \
write test_calc.py that checks add(2,3)==5 and passes under pytest" \
  --k 2 --mode parallel \
  --verify "python3 -m pytest -q test_calc.py"
```

The result is left in an isolated worktree under
`<workspace>/.harness2/worktrees/`; the repository itself is not modified.
The report is at `<workspace>/.harness2/general_parallel_k2.json`, the
experience library at `<workspace>/.harness2/library/k2/`, and the run
artifacts (diff, trajectory, verifier output, leak report) under
`<workspace>/.harness2/runs/`.

Useful flags:

* `--no-sandbox` — run without `bwrap` (the sandbox is on by default when
  it is available; it is what keeps a shell command from writing outside the
  workspace or reading your home directory).
* `--allow-mcp` — let the rollout use your OpenCode's MCP servers. Off by
  default: they may hold credentials, and they are a route out of the
  workspace that a path fence does not close.

The same run is available to any MCP client as the `harness2_recursion`
tool.

---

## 12. Open questions

* The improver's model is the limiting factor. The Mistral runs produced
  harness edits that were *worse* than the base; the `inception/mercury-3`
  run produced edits that were indistinguishable from it, on a task simple
  enough that no edit could show an effect. The paper's claim -- that a
  single recursion step consistently improves on the base harness -- needs
  a task set where an edit *can* show an effect, and a model that can find
  one.
* The sandbox needs `bwrap`, and `bwrap` needs user namespaces. Where those
  are unavailable the fence still applies, but a shell command keeps the
  operator's filesystem authority. The leak check is the backstop.
* `bwrap` binds the system directories read-only but does not unshare the
  network: a rollout can still reach any host the operator can.
* The installed OpenCode resolves a relative path against the *process*
  cwd, not the directory it is given. Every caller must therefore run the
  process with its working directory set to the workspace it means to write
  in. The sandbox does this with `--chdir`; the unsandboxed paths rely on
  `subprocess.run(cwd=...)`, which is right but easy to lose.
