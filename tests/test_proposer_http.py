"""The http proposer channel.

Two kinds of session meet this channel. A PROPOSAL session edits the harness
tree. An ANSWER session -- the selector, the lesson distiller -- reads evidence
and answers in the format its mission specifies. Both have to work, and both
have to be safe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness2.core.proposer import Proposer
from harness2.core.spec import resolve_channel_model


def _proposer() -> Proposer:
    return Proposer(resolve_channel_model("http", None))


def _ctx(tmp_path: Path, with_harness: bool = True) -> Path:
    ctx = tmp_path / "ctx"
    ctx.mkdir(parents=True, exist_ok=True)
    if with_harness:
        (ctx / "harness").mkdir()
        (ctx / "harness" / "guardrails.md").write_text("old rules\n", encoding="utf-8")
        (ctx / "harness" / "systemprompt.md").write_text("old prompt\n", encoding="utf-8")
    else:
        (ctx / "candidates").mkdir()
        (ctx / "candidates" / "A.md").write_text("candidate A\n", encoding="utf-8")
    return ctx


def test_a_proposal_edits_the_harness_tree(tmp_path, scripted_llm):
    scripted_llm(
        [
            json.dumps(
                {
                    "notes": "the run duplicated the check, so the rule goes in",
                    "edits": [
                        {
                            "path": "guardrails.md",
                            "action": "write",
                            "content": "run the check once\n",
                        }
                    ],
                }
            )
        ]
    )
    ctx = _ctx(tmp_path)
    reply = _proposer().call("propose something", ctx, "inv1", harvest="context/harness")

    assert reply.ok, reply.error
    assert (reply.workspace / "guardrails.md").read_text(encoding="utf-8") == "run the check once\n"
    assert "run the check" not in (ctx / "harness" / "guardrails.md").read_text(encoding="utf-8"), (
        "the caller's tree was edited in place"
    )
    assert "the run duplicated the check" in reply.text


def test_a_proposal_that_deletes_a_file(tmp_path, scripted_llm):
    scripted_llm(
        [json.dumps({"notes": "drop it", "edits": [{"path": "systemprompt.md", "action": "delete"}]})]
    )
    ctx = _ctx(tmp_path)
    reply = _proposer().call("propose something", ctx, "inv1", harvest="context/harness")
    assert reply.ok, reply.error
    assert not (reply.workspace / "systemprompt.md").exists()


def test_an_edit_that_escapes_the_harness_is_refused(tmp_path, scripted_llm):
    scripted_llm(
        [
            json.dumps(
                {
                    "notes": "sneaky",
                    "edits": [
                        {"path": "../../escape.md", "action": "write", "content": "x"},
                        {"path": "/etc/passwd", "action": "write", "content": "x"},
                    ],
                }
            )
        ]
    )
    ctx = _ctx(tmp_path)
    reply = _proposer().call("propose something", ctx, "inv1", harvest="context/harness")
    assert reply.ok, reply.error
    assert not (tmp_path / "escape.md").exists()
    assert "escapes the harness" in reply.text


def test_an_answer_session_returns_the_reply_verbatim(tmp_path, scripted_llm):
    """The selector's reply is parsed by regexes, so it has to come back as sent."""
    answer = "<why>\nit is the only complete one\n</why>\n\n```choice\nA\n```"
    calls = scripted_llm([answer])
    ctx = _ctx(tmp_path, with_harness=False)
    reply = _proposer().call("choose one", ctx, "select.agg1")
    assert reply.ok, reply.error
    assert "```choice" in reply.text
    assert "it is the only complete one" in reply.text
    # The evidence was inlined: the model has no filesystem of its own.
    user = calls[0]["messages"][1]["content"]
    assert "candidate A" in user


def test_a_proposal_that_comes_back_as_prose_is_repaired(tmp_path, scripted_llm):
    """The common failure is prose instead of JSON. One correction fixes it."""
    calls = scripted_llm(
        [
            "Looking at the mission, I think we should change the guardrails to say X.",
            json.dumps(
                {
                    "notes": "changed the guardrails",
                    "edits": [
                        {"path": "guardrails.md", "action": "write", "content": "say X\n"}
                    ],
                }
            ),
        ]
    )
    ctx = _ctx(tmp_path)
    reply = _proposer().call("propose something", ctx, "inv1", harvest="context/harness")
    assert reply.ok, reply.error
    assert len(calls) == 2
    assert "CORRECTION" in calls[1]["messages"][1]["content"]
    assert (reply.workspace / "guardrails.md").read_text(encoding="utf-8") == "say X\n"


def test_a_proposal_with_no_edits_is_still_a_reply(tmp_path, scripted_llm):
    """Nothing to change is a legitimate answer, not a failure."""
    scripted_llm([json.dumps({"notes": "the harness is fine as it is", "edits": []})])
    ctx = _ctx(tmp_path)
    reply = _proposer().call("propose something", ctx, "inv1", harvest="context/harness")
    assert reply.ok, reply.error
    assert "the harness is fine" in reply.text


def test_no_provider_is_a_clear_error(tmp_path):
    """The channel says what is missing instead of failing obscurely."""
    ctx = _ctx(tmp_path)
    with pytest.raises(Exception) as exc:
        _proposer().call("propose something", ctx, "inv1", harvest="context/harness")
    assert "INCEPTION_API_KEY" in str(exc.value) or "no LLM configured" in str(exc.value)
