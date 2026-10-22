"""Test-wide safety rails.

No test may reach a model service. The solver's prompt carries the repository
under test, so a stray call is both a bill and a disclosure of someone else's
code. These fixtures make that impossible rather than merely discouraged.

Tests that need a model install their own double with the ``scripted_llm``
fixture; tests that need the harness to reach a solver use the same fixture and
then assert on the recorded call.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from harness2 import llm

# The agent runtimes a test must never launch. Resolved once, so a stub in a
# tmp_path (which is what the end-to-end test uses) is still allowed.
_AGENT_BINARIES = {
    str(Path(p).resolve())
    for p in (shutil.which("opencode"), shutil.which("claude"), shutil.which("codex"))
    if p
}


@pytest.fixture(autouse=True)
def _no_paid_model_calls(monkeypatch):
    """Make every model call in a test fail loudly instead of leaving the machine."""

    def refuse(*_a, **_kw):
        raise AssertionError(
            "a test tried to call the model service; install the `scripted_llm` fixture"
        )

    monkeypatch.setattr(llm, "chat", refuse)
    monkeypatch.setattr(llm, "resolve_provider", lambda *_a, **_kw: None)


@pytest.fixture(autouse=True)
def _no_agent_binaries(monkeypatch):
    """Refuse to launch a real agent runtime from a test."""
    real_run = subprocess.run

    def guarded(cmd, *args, **kwargs):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        if argv:
            try:
                head = str(Path(str(argv[0])).resolve())
            except OSError:
                head = ""
            if head in _AGENT_BINARIES:
                raise AssertionError(
                    f"a test tried to launch the agent runtime {argv[0]!r}; "
                    f"stub it instead"
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture
def scripted_llm(monkeypatch) -> Callable[..., List[Dict[str, Any]]]:
    """Install a scripted model and return the list it records calls into.

    `install(replies)` queues the replies, or takes a callable
    ``(messages, **kwargs) -> str`` that decides per call. Each call appends
    ``{"messages": [...], "kwargs": {...}}`` to the returned list, so a test can
    assert on what the model was actually asked.
    """
    calls: List[Dict[str, Any]] = []

    def install(replies) -> List[Dict[str, Any]]:
        queue = list(replies) if isinstance(replies, (list, tuple)) else None

        def fake_chat(messages, **kwargs):
            calls.append({"messages": messages, "kwargs": kwargs})
            if queue is not None:
                if not queue:
                    raise AssertionError("the model was called more often than the test scripted")
                return queue.pop(0)
            return replies(messages, **kwargs)

        monkeypatch.setattr(llm, "chat", fake_chat)
        monkeypatch.setattr(
            llm,
            "resolve_provider",
            lambda *_a, **_kw: llm.Provider(
                "scripted", "http://scripted.invalid/v1", "scripted-key", "scripted-model"
            ),
        )
        return calls

    return install
