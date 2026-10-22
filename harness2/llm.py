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

"""Minimal OpenAI-compatible chat client (stdlib only) for the general-task path.

Provider resolution (first match wins):
  1. HARNESS2_LLM_BASE_URL + HARNESS2_LLM_API_KEY + HARNESS2_LLM_MODEL (any OpenAI-compatible API)
  2. INCEPTION_API_KEY -> Inception Labs (https://api.inceptionlabs.ai/v1), model
     HARNESS2_INCEPTION_MODEL (default "mercury-3")
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import config  # noqa: F401  (importing config loads .env)
from .config import env

INCEPTION_BASE_URL = "https://api.inceptionlabs.ai/v1"


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """The reply was cut off by the model's output budget.

    Distinct from a generic failure because the fix is different: the caller has to
    ask for less, not retry the same request. A truncated JSON body fails to parse
    with a message about a comma, which says nothing about the real cause.
    """

    def __init__(self, message: str, *, model: str = ""):
        super().__init__(message)
        self.model = model


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str

    def public(self) -> Dict[str, str]:
        return {"provider": self.name, "base_url": self.base_url, "model": self.model}


def resolve_provider() -> Optional[Provider]:
    base, key, model = env("HARNESS2_LLM_BASE_URL"), env("HARNESS2_LLM_API_KEY"), env("HARNESS2_LLM_MODEL")
    if base and key and model:
        return Provider("custom", base.rstrip("/"), key, model)
    key = env("INCEPTION_API_KEY")
    if key:
        return Provider(
            "inception",
            env("HARNESS2_INCEPTION_BASE_URL", INCEPTION_BASE_URL).rstrip("/"),
            key,
            env("HARNESS2_INCEPTION_MODEL", "mercury-3"),
        )
    return None


def chat(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 8000,
    provider: Optional[Provider] = None,
    retries: int = 3,
    timeout: int = 180,
    usage: Optional[Dict[str, Any]] = None,
) -> str:
    p = provider or resolve_provider()
    if p is None:
        raise LLMError(
            "No LLM configured. Set INCEPTION_API_KEY in harness2/.env, or "
            "HARNESS2_LLM_BASE_URL/HARNESS2_LLM_API_KEY/HARNESS2_LLM_MODEL."
        )
    payload = json.dumps(
        {"model": p.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    ).encode()
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{p.base_url}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {p.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data: Dict[str, Any] = json.loads(resp.read().decode())
            if usage is not None:
                usage.update(data.get("usage") or {})
                usage.setdefault("model", p.model)
            return _content(data, p)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            last = LLMError(f"HTTP {e.code} from {p.name}: {body}")
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last = e
        # Back off, but never after the final attempt: there is nothing to wait for.
        if attempt < retries - 1:
            time.sleep(2**attempt)
    raise LLMError(f"LLM call failed: {last}")


def _content(data: Dict[str, Any], p: Provider) -> str:
    """The assistant text, with the truncation case raised as its own error.

    A reply that hit the output budget arrives as valid text that stops mid-sentence.
    Returning it makes the caller fail later, in a JSON parser, with a message about a
    missing comma. Saying so here is the difference between "the model was cut off" and
    "the model wrote invalid JSON".
    """
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"LLM call to {p.name} returned no choices: {str(data)[:200]}")
    choice = choices[0] or {}
    finish = choice.get("finish_reason")
    if finish == "length":
        raise LLMTruncated(
            f"the reply from {p.name}/{p.model} was cut off by the {max_tokens}-token "
            f"output budget. Ask for less in one call, or raise max_tokens.",
            model=p.model,
        )
    message = choice.get("message") or {}
    return str(message.get("content") or "")


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a reply (tolerates ```json fences and prose)."""
    t = text.strip()
    if "```" in t:
        for chunk in t.split("```"):
            c = chunk.removeprefix("json").strip()
            if c.startswith("{"):
                t = c
                break
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"No JSON object in model reply: {text[:300]!r}")
    body = t[start : end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        # The raw decoder message points at a comma, which says nothing about the
        # real cause: a reply cut off mid-object fails exactly here.
        raise LLMTruncated(
            f"the model's reply is not valid JSON ({exc.msg} at line {exc.lineno} "
            f"column {exc.colno}). It was probably cut off mid-object; ask for less "
            f"in one call, or check the model's output budget.",
        ) from exc
