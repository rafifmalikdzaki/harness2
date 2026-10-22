# Troubleshooting

Start here:

```bash
harness2-doctor --workspace ~/your-repo
```

It checks the model service, git, a real worktree round trip, the sandbox, the
state directory and the `.env` file, and prints the line that fixes whatever is
broken. This document covers the failures it cannot see.

---

## The run does nothing, or fails immediately

### "no model service configured"

The general path needs one of these:

```bash
INCEPTION_API_KEY=sk-...
# or
HARNESS2_LLM_BASE_URL=... HARNESS2_LLM_API_KEY=... HARNESS2_LLM_MODEL=...
```

`harness2-general --dry-run` prints the provider and model it resolved, so you
can check it without spending anything.

### "the opencode channel needs a provider-prefixed model id"

The `opencode` channel runs the improver through the OpenCode CLI, which names
models as `provider/model` (`anthropic/claude-sonnet-4-5`). Either set
`HARNESS2_IMPROVER_MODEL` to such an id, or use the default:

```bash
harness2-general ... --channel http
```

`http` runs the improver and the selector on the same model service as the
solver, so one API key is enough.

---

## The run dies part-way with a grader-verdict error

> `grader verdict would enter model context: 'source/scripts/x.py' contains marker '"passed_count"'`

This was a real bug, and it killed whole runs: on a general task the evidence
**is** your repository, and `"passed_count"` is an ordinary JSON key in an
ordinary script. The tripwire was built for benchmarks, where the grader's
output must never reach the model.

The general path now declares no markers and quarantines a hit instead of
aborting: the file is moved to `.harness2/runs/<session>/quarantine/` and a
`WARNINGS.md` in the evidence tree says what happened. The run continues.

If you still see this on a **benchmark** path, it is a real leak: find the
read path that produced it rather than widening the marker list.

---

## The run finishes but nothing was selected

```
[general] 1/1 partial  sess_xxx :: agg: the selector failed, so the run fell
back to the reference execution (base1). Nothing was selected on merit...
```

The rollouts ran and their artifacts are on disk; the *selector* is what failed.
The audit trail says why:

```bash
cat .harness2/audit/select.agg*.response.md
cat .harness2/audit/select.agg*.meta.json
```

Common causes: the reply had no `choice` block, the model named a rollout
instead of an anonymous id, or the endpoint was unreachable.

---

## Every candidate produced the same thing

Check that the harness actually reached the solver:

```bash
cat .harness2/runs/<session>/<label>/candidate.json | grep -A6 '"harness"'
```

`"applied": true` with different `digest` values per candidate means the loop
is working. If every candidate has the same digest, the proposals were
identical — read `.harness2/audit/<label>*.response.md` to see what the
improver proposed.

---

## The verification passes but the run is still reported as failed

The verdict is the **exit status** of the verification command. A command that
times out is not a failing test — it is a command that did not finish, and it
is recorded as such:

```
VERIFICATION: FAILED (the command did not complete)
```

Check the exit status yourself:

```bash
cd .harness2/worktrees/<session>_<label> && <your verify command>; echo $?
```

---

## `harness2-finish` says "Nothing applied"

Two reasons, and the message says which:

- *"the candidate produced no changes to apply"* — the candidate's worktree was
  identical to HEAD. Nothing was lost; there was nothing to apply.
- *"refused: it deletes N file(s)"* — the patch would remove files from your
  repository. Read the list it prints and decide. To apply it anyway:

```bash
harness2-finish <session> --workspace <repo> --label <candidate> --allow-destructive
```

A verification command that exited 0 does not make a mass deletion safe: a
candidate once passed its tests after removing 151 of the project's files.

---

## The worktrees are still there after `harness2-finish`

Check that the command was pointed at the right workspace. The worktrees live
in `<workspace>/.harness2/worktrees/`, and the command looks there only when
`<workspace>/.harness2/worktrees/` exists:

```bash
git worktree list
ls .harness2/worktrees/
```

Remove them by hand with `git worktree remove --force <path>` and
`git worktree prune`.

---

## The run works from HEAD, not from my working tree

That is by design: a candidate runs in a worktree built from `HEAD`, so
uncommitted changes are invisible to it — and the candidate's patch is applied
on top of them, which can conflict.

`harness2-general` warns about this before it starts, and `harness2-doctor`
warns about it too. Commit or stash first.

---

## The test suite is making network calls

It is not supposed to, and it cannot: `tests/conftest.py` makes every model
call fail loudly and blocks the agent runtimes. If you see a paid call, it
came from a test that was written before the rails existed.

---

## The model says the reply was cut off

> `the reply from inception/mercury-3 was cut off by the 16000-token output
> budget`

The request was too big for one reply. Two fixes:

- **Slice the task.** Ask for one or two files at a time. This is the
  difference between a run that completes and one that dies mid-object.
- **Raise the budget.** The channel uses `max_tokens=16000`; a provider that
  supports more can be given more.

---

## Getting more detail

```bash
# Every mission, every reply, every harness that came back
ls .harness2/audit/

# The run's own record
cat .harness2/runs/general-*/<session>/summary.json

# As JSON lines, for a script
harness2-general ... --progress json 2>progress.jsonl
```
