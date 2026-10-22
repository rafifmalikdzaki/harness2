"""The model client: the cases that turn a small mistake into a lost run.

Every test here is hermetic: the HTTP layer is stubbed, so nothing reaches
the network and nothing is billed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from harness2 import llm

# This module tests the client itself, so the autouse "no model calls" block
# is lifted here. The HTTP layer is stubbed in every test below, so nothing
# reaches the network.
pytestmark = pytest.mark.allow_model_calls


def _response(payload: dict) -> BytesIO:
    return BytesIO(json.dumps(payload).encode())


def _ok(content: str, finish: str = "stop", usage: dict | None = None) -> dict:
    out = {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
    }
    if usage:
        out["usage"] = usage
    return out


class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = _response(payload)

    def read(self):
        return self._raw.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x/v1/chat/completions", code, "boom", {}, BytesIO(b"bad request")
    )


PROVIDER = llm.Provider("test", "http://test.invalid/v1", "key", "test-model")


def test_a_truncated_reply_says_so(monkeypatch):
    """A reply cut off by the output budget is not a JSON error.

    The caller needs to know to ask for less, not to retry the same request.
    """
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _FakeResponse(_ok('{"plan": "do it", "edits": [', finish="length"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMTruncated) as exc:
        llm.chat([{"role": "user", "content": "x"}], provider=PROVIDER, max_tokens=100)
    assert "output budget" in str(exc.value)
    assert len(calls) == 1, "a truncated reply must not be retried as-is"


def test_an_unparseable_reply_is_reported_as_truncation(monkeypatch):
    """A reply that stops mid-object is almost always the output budget."""

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_ok('{"plan": "do it", "edits": [{"path": "a.py"'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMTruncated):
        llm.extract_json('{"plan": "do it", "edits": [{"path": "a.py"')


def test_a_400_is_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise _http_error(400)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "x"}], provider=PROVIDER, retries=3)
    assert len(calls) == 1, "a bad request must not be retried"


def test_a_500_is_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) < 3:
            raise _http_error(500)
        return _FakeResponse(_ok("done"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.chat([{"role": "user", "content": "x"}], provider=PROVIDER, retries=3) == "done"
    assert len(calls) == 3


def test_the_last_attempt_does_not_sleep(monkeypatch):
    """Waiting after the final attempt wastes seconds on every hard failure."""
    slept = []

    def fake_urlopen(req, timeout=None):
        raise _http_error(500)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "x"}], provider=PROVIDER, retries=3)
    assert len(slept) == 2, slept


def test_usage_is_reported_when_the_endpoint_gives_it(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_ok("ok", usage={"prompt_tokens": 10, "completion_tokens": 5}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    usage: dict = {}
    llm.chat([{"role": "user", "content": "x"}], provider=PROVIDER, usage=usage)
    assert usage["prompt_tokens"] == 10
    assert usage["model"] == "test-model"


def test_extract_json_tolerates_a_fenced_reply():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('here you go: {"a": 2} -- done') == {"a": 2}


def test_extract_json_without_an_object_says_so():
    with pytest.raises(llm.LLMError) as exc:
        llm.extract_json("I could not do that.")
    assert "No JSON object" in str(exc.value)


def test_no_provider_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm, "resolve_provider", lambda *_a, **_kw: None)
    with pytest.raises(llm.LLMError) as exc:
        llm.chat([{"role": "user", "content": "x"}], provider=None)
    assert "INCEPTION_API_KEY" in str(exc.value)
