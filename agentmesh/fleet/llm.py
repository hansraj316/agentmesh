"""Pluggable model layer for the example fleet.

Two models share one tiny interface, ``complete(role, prompt) -> LLMResult``:

* :class:`ScriptedLLM` (the default) needs no network and no key. It is
  deterministic for a given seed, sleeps a few seconds per call so the live
  dashboard has something to watch, and fails now and then so Failed rows and
  ``agentmesh flaky`` have data.
* :class:`ClaudeLLM` calls the Claude Messages API through the stdlib
  ``urllib``, so AgentMesh keeps zero runtime dependencies. It is opt in: it
  reads ``ANTHROPIC_API_KEY`` from the environment and never stores it.
"""

import json
import os
import random
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
"""Model used when ``AGENTMESH_FLEET_MODEL`` is unset."""

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION = "2026-10-21"
# Server-side fallback: a request declined by a safety classifier is re-run on
# Anthropic's recommended fallback model instead of coming back as a refusal.
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens (input, output), for cost views. A model that is not
# listed records tokens but no cost.
CLAUDE_PRICES: Dict[str, Tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

SCRIPTED_MODEL = "scripted-fleet-1"
# Notional price for the scripted model so cost views show believable numbers.
SCRIPTED_PRICE = (2.0, 10.0)

ROLES = ("planner", "coder", "reviewer")

SYSTEM_PROMPTS = {
    "planner": (
        "You are the Planner in a three-agent software team (Planner, Coder, Reviewer). "
        "Break the goal you are given into two to four small, concrete engineering tasks "
        "a single developer could finish in one pull request each. Reply with one task per "
        "line and nothing else: no numbering, no bullets, no preamble."
    ),
    "coder": (
        "You are the Coder in a three-agent software team. Describe the change you would "
        "make for the task you are given, as a short summary of the patch: the files you "
        "touch and what changes in each. Keep it under 120 words."
    ),
    "reviewer": (
        "You are the Reviewer in a three-agent software team. Review the proposed patch for "
        "the task. Start your reply with exactly APPROVE or CHANGES on the first line, then "
        "give one or two sentences of reasoning."
    ),
}


class LLMError(RuntimeError):
    """A model call failed (network, API error, refusal, or a scripted failure)."""


@dataclass(frozen=True)
class LLMResult:
    """One model reply plus the usage the fleet records on the agent span."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Optional[float] = None


def _price(input_tokens: int, output_tokens: int, prices: Tuple[float, float]) -> float:
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# Scripted replies, keyed by role. Planner output depends on the goal, so it is
# built in ScriptedLLM._planner; coder and reviewer pick from these.
_CODER_REPLIES = [
    "Touches {area}: wraps the call in a bounded retry with jittered backoff, adds a "
    "unit test for the timeout path, and updates the changelog.",
    "Touches {area}: extracts the parsing into a helper, adds type hints, and covers "
    "the empty-input case with a regression test.",
    "Touches {area}: adds the missing guard, logs the rejected payload at debug level, "
    "and adds two table-driven tests.",
]

_REVIEW_APPROVE = [
    "APPROVE\nSmall, well-tested change. Backoff bounds look sane.",
    "APPROVE\nTests cover the failure path; naming matches the rest of the module.",
]

_REVIEW_CHANGES = [
    "CHANGES\nThe retry loop swallows the last exception; re-raise it after the final attempt.",
    "CHANGES\nPlease add a test for the empty response before this merges.",
]

_SCRIPTED_FAILURES = [
    "RateLimitError: 429 Too Many Requests from GitHub API",
    "TimeoutError: test suite exceeded 120s",
    "AssertionError: test_sync_retries expected 3 attempts, saw 1",
]


class ScriptedLLM:
    """Deterministic offline model for demos and tests.

    ``seed`` fixes every choice (task split, reviewer verdicts, failures,
    delays). ``delay_s`` is the (min, max) seconds each call sleeps; pass
    ``(0, 0)`` in tests. ``failure_rate`` is the chance that a coder call
    raises :class:`LLMError`. ``review_changes_rate`` is the chance the
    reviewer sends work back. ``fail_on`` forces a failure on the coder call
    whose task text contains that substring, for tests that need one.
    """

    model = SCRIPTED_MODEL

    def __init__(
        self,
        seed: Optional[int] = None,
        delay_s: Tuple[float, float] = (1.5, 4.0),
        failure_rate: float = 0.08,
        review_changes_rate: float = 0.3,
        fail_on: Optional[str] = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.delay_s = delay_s
        self.failure_rate = failure_rate
        self.review_changes_rate = review_changes_rate
        self.fail_on = fail_on
        self.stop: Optional[threading.Event] = None
        """Set by ``run_forever`` so a sleeping call wakes up on shutdown."""

    def reseed(self, seed: Optional[int]) -> None:
        with self._lock:
            self._rng = random.Random(seed)

    def _sleep(self) -> None:
        low, high = self.delay_s
        if high <= 0:
            return
        with self._lock:
            seconds = self._rng.uniform(low, high)
        if self.stop is not None:
            self.stop.wait(seconds)
        else:
            threading.Event().wait(seconds)

    def complete(self, role: str, prompt: str) -> LLMResult:
        if role not in ROLES:
            raise ValueError("unknown role %r" % (role,))
        self._sleep()
        with self._lock:
            if role == "planner":
                text = self._planner(prompt)
            elif role == "coder":
                text = self._coder(prompt)
            else:
                text = self._reviewer(prompt)
        input_tokens = _rough_tokens(SYSTEM_PROMPTS[role]) + _rough_tokens(prompt)
        output_tokens = _rough_tokens(text) * 3
        return LLMResult(
            text=text,
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_price(input_tokens, output_tokens, SCRIPTED_PRICE),
        )

    def _planner(self, goal: str) -> str:
        from agentmesh.fleet.backlog import tasks_for_goal

        tasks = tasks_for_goal(goal)
        count = self._rng.randint(2, min(3, len(tasks)))
        return "\n".join(tasks[:count])

    def _coder(self, prompt: str) -> str:
        task = prompt.splitlines()[0] if prompt else ""
        if self.fail_on is not None and self.fail_on in task:
            raise LLMError(_SCRIPTED_FAILURES[0])
        if self.failure_rate > 0 and self._rng.random() < self.failure_rate:
            raise LLMError(self._rng.choice(_SCRIPTED_FAILURES))
        area = _area_for(task)
        return self._rng.choice(_CODER_REPLIES).format(area=area)

    def _reviewer(self, prompt: str) -> str:
        final = "FINAL REVIEW" in prompt
        if not final and self._rng.random() < self.review_changes_rate:
            return self._rng.choice(_REVIEW_CHANGES)
        return self._rng.choice(_REVIEW_APPROVE)


def _area_for(task: str) -> str:
    lowered = task.lower()
    if "github" in lowered or "sync" in lowered:
        return "agentmesh/ingest_gha.py"
    if "otlp" in lowered or "export" in lowered:
        return "agentmesh/otlp.py"
    if "alert" in lowered or "webhook" in lowered:
        return "agentmesh/alerts.py"
    if "cost" in lowered or "token" in lowered:
        return "agentmesh/costs.py"
    if "board" in lowered or "html" in lowered:
        return "agentmesh/board.py"
    return "agentmesh/store.py"


Opener = Callable[..., Any]


class ClaudeLLM:
    """Calls the Claude Messages API with the stdlib ``urllib``.

    The API key comes from ``api_key`` or the ``ANTHROPIC_API_KEY``
    environment variable and is only ever sent in the request header. The
    model comes from ``model``, then ``AGENTMESH_FLEET_MODEL``, then
    :data:`DEFAULT_CLAUDE_MODEL`. ``opener`` replaces ``urllib.request.urlopen``
    in tests so nothing touches the network.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 16000,
        timeout_s: float = 120.0,
        opener: Optional[Opener] = None,
        base_url: str = CLAUDE_API_URL,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set; export it to use --llm claude, "
                "or use the default scripted model"
            )
        self._api_key = key
        self.model = model or os.environ.get("AGENTMESH_FLEET_MODEL") or DEFAULT_CLAUDE_MODEL
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.base_url = base_url
        self._open = opener if opener is not None else urllib.request.urlopen

    def __repr__(self) -> str:
        # Never include the key in reprs or logs.
        return "ClaudeLLM(model=%r)" % (self.model,)

    def request_body(self, role: str, prompt: str) -> Dict[str, Any]:
        if role not in ROLES:
            raise ValueError("unknown role %r" % (role,))
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPTS[role],
            "messages": [{"role": "user", "content": prompt}],
            # Short, routine steps: low effort keeps the fleet quick and cheap.
            "output_config": {"effort": "low"},
            "fallbacks": "default",
        }

    def complete(self, role: str, prompt: str) -> LLMResult:
        body = json.dumps(self.request_body(role, prompt)).encode("utf-8")
        request = urllib.request.Request(
            self.base_url,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": CLAUDE_API_VERSION,
                "anthropic-beta": CLAUDE_FALLBACK_BETA,
            },
        )
        try:
            with self._open(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError("Claude API error %s: %s" % (exc.code, _error_message(exc))) from None
        except (urllib.error.URLError, OSError) as exc:
            raise LLMError("Claude API unreachable: %s" % (exc,)) from None
        except ValueError:
            raise LLMError("Claude API returned a response that is not JSON") from None
        return self._parse(data)

    def _parse(self, data: Dict[str, Any]) -> LLMResult:
        if data.get("stop_reason") == "refusal":
            details = data.get("stop_details") or {}
            category = details.get("category") or "unspecified"
            raise LLMError("Claude declined the request (refusal, category %s)" % category)
        blocks: List[Dict[str, Any]] = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not text:
            raise LLMError("Claude returned no text (stop_reason %r)" % data.get("stop_reason"))
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        # With a server-side fallback the reply may come from another model.
        model = data.get("model") or self.model
        prices = CLAUDE_PRICES.get(model)
        cost = _price(input_tokens, output_tokens, prices) if prices else None
        return LLMResult(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )


def _error_message(exc: "urllib.error.HTTPError") -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        return str(payload.get("error", {}).get("message") or exc.reason)
    except Exception:  # noqa: BLE001 - best effort, never mask the HTTP status
        return str(exc.reason)


def make_llm(kind: str = "scripted", seed: Optional[int] = None) -> Any:
    """Build the model named on the command line (``scripted`` or ``claude``)."""
    if kind == "scripted":
        return ScriptedLLM(seed=seed)
    if kind == "claude":
        return ClaudeLLM()
    raise ValueError("unknown llm %r (expected scripted or claude)" % (kind,))
