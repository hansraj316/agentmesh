"""Flakiness report: rank agents by how erratically their runs fail.

The board answers "how is each agent doing right now?"; this module answers
"which agents can I not trust?" by looking at each agent's *history* of
completed run outcomes:

* ``agent_outcomes`` derives, per agent, the chronological list of completed
  run outcomes (ok/failed) from terminal ``agent_end``/``agent_error``
  events. Still-running runs are ignored.
* ``flakiness_metrics`` computes per-agent reliability numbers and a
  classification (see ``_classify`` and the ``flakiness_metrics`` docstring
  for the heuristics).
* ``render_flaky`` renders a ranked markdown table, flakiest agents first.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from agentmesh.board import _format_duration, _humanize_age, _TERMINAL_TYPES
from agentmesh.events import _parse_ts
from agentmesh.store import EventStore

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"

CLASS_STABLE = "stable"
CLASS_FAILING = "failing"
CLASS_FLAKY = "flaky"
CLASS_DEGRADED = "degraded"

# failure_rate at or above this is "failing" (mostly broken, not flaky).
FAILING_RATE_THRESHOLD = 0.8
# intermittency at or above this (with >= 2 failures) is "flaky".
FLAKY_INTERMITTENCY_THRESHOLD = 0.3
FLAKY_MIN_FAILURES = 2

_MARKDOWN_HEADER = [
    "| Agent | Class | Runs | Fail% | Intermittency | MTBF | Last failure |",
    "|-------|-------|------|-------|---------------|------|--------------|",
]

_LEGEND = (
    "Legend: stable = no failures · failing = failure rate ≥ %d%% · "
    "flaky = ≥%d failures flip-flopping with outcomes (intermittency ≥ %.1f) · "
    "degraded = failures without strong flip-flopping."
    % (
        round(FAILING_RATE_THRESHOLD * 100),
        FLAKY_MIN_FAILURES,
        FLAKY_INTERMITTENCY_THRESHOLD,
    )
)


@dataclass
class RunOutcome:
    """The outcome of one completed run for one agent."""

    run_id: str
    outcome: str  # OUTCOME_OK or OUTCOME_FAILED
    ts: str  # ISO-8601 UTC ts of the agent's first terminal event in the run


@dataclass
class AgentFlakiness:
    """Reliability metrics of one agent over its completed runs."""

    agent: str
    runs: int
    failures: int
    failure_rate: float
    intermittency: Optional[float]  # None with fewer than 2 runs
    mtbf_seconds: Optional[float]  # None with fewer than 2 failures
    last_failure_ts: Optional[str]  # None if the agent never failed
    classification: str  # one of CLASS_STABLE/FAILING/FLAKY/DEGRADED


def agent_outcomes(
    store: EventStore,
    window: Optional[int] = None,
    since: Optional[Union[str, datetime]] = None,
) -> Dict[str, List[RunOutcome]]:
    """Per agent, the chronological list of completed run outcomes.

    A run counts as completed for an agent once that agent has a terminal
    event (``agent_end`` or ``agent_error``) in it; still-running runs are
    ignored. The run's outcome for the agent is ``failed`` if the agent
    emitted *any* ``agent_error`` in that run, else ``ok``. Each outcome is
    timestamped with the agent's first terminal event in the run and the
    list is sorted chronologically.

    ``window=N`` keeps only the N most recent outcomes per agent. ``since``
    is an ISO-8601 UTC string (or datetime) compared against the event
    ``ts``; only events at or after it are considered.
    """
    if window is not None and window < 1:
        raise ValueError("window must be a positive integer, got %r" % (window,))
    if isinstance(since, datetime):
        since = since.isoformat()
    per_agent: Dict[str, Dict[str, Dict[str, object]]] = {}
    for _, event in store.events_after(0):
        if since is not None and event.ts < since:
            continue
        if event.type not in _TERMINAL_TYPES:
            continue
        runs = per_agent.setdefault(event.agent, {})
        state = runs.setdefault(event.run_id, {"ts": event.ts, "failed": False})
        if event.type == "agent_error":
            state["failed"] = True
    result: Dict[str, List[RunOutcome]] = {}
    for agent, runs in sorted(per_agent.items()):
        outcomes = [
            RunOutcome(
                run_id=run_id,
                outcome=OUTCOME_FAILED if state["failed"] else OUTCOME_OK,
                ts=str(state["ts"]),
            )
            for run_id, state in runs.items()
        ]
        outcomes.sort(key=lambda outcome: (outcome.ts, outcome.run_id))
        if window is not None:
            outcomes = outcomes[-window:]
        result[agent] = outcomes
    return result


def flakiness_metrics(outcomes: Dict[str, List[RunOutcome]]) -> List[AgentFlakiness]:
    """Per-agent reliability metrics from ``agent_outcomes`` output.

    Heuristics:

    * ``failure_rate`` — failures / total completed runs.
    * ``intermittency`` — alternations / (n - 1), where an alternation is an
      adjacent pair of outcomes that differ. 0.0 means stable behaviour (all
      outcomes the same), 1.0 means perfectly alternating ok/failed — the
      signature of a flaky agent. ``None`` with fewer than 2 runs (no
      adjacent pairs to compare).
    * ``mtbf_seconds`` — mean time between failures: the mean of the deltas
      between consecutive failure timestamps. ``None`` with fewer than 2
      failures (no deltas).
    * ``last_failure_ts`` — ts of the most recent failure, ``None`` if the
      agent never failed.
    * ``classification`` (first match wins):
        - ``stable`` — zero failures.
        - ``failing`` — failure_rate >= 0.8: mostly broken, not flaky.
        - ``flaky`` — at least 2 failures and intermittency >= 0.3: failures
          interleaved with successes.
        - ``degraded`` — everything else: some failures, but neither mostly
          broken nor strongly alternating (e.g. one recent failure burst).
    """
    metrics = []
    for agent, history in sorted(outcomes.items()):
        if not history:
            continue
        runs = len(history)
        failure_ts = [outcome.ts for outcome in history if outcome.outcome == OUTCOME_FAILED]
        failures = len(failure_ts)
        failure_rate = failures / runs
        intermittency = None
        if runs >= 2:
            alternations = sum(
                1
                for previous, current in zip(history, history[1:])
                if previous.outcome != current.outcome
            )
            intermittency = alternations / (runs - 1)
        mtbf_seconds = None
        if failures >= 2:
            moments = [_parse_ts(ts) for ts in failure_ts]
            deltas = [
                (current - previous).total_seconds()
                for previous, current in zip(moments, moments[1:])
            ]
            mtbf_seconds = sum(deltas) / len(deltas)
        metrics.append(
            AgentFlakiness(
                agent=agent,
                runs=runs,
                failures=failures,
                failure_rate=failure_rate,
                intermittency=intermittency,
                mtbf_seconds=mtbf_seconds,
                last_failure_ts=failure_ts[-1] if failure_ts else None,
                classification=_classify(failures, failure_rate, intermittency),
            )
        )
    return metrics


def _classify(failures: int, failure_rate: float, intermittency: Optional[float]) -> str:
    """Classify an agent's reliability; see ``flakiness_metrics`` for the rules."""
    if failures == 0:
        return CLASS_STABLE
    if failure_rate >= FAILING_RATE_THRESHOLD:
        return CLASS_FAILING
    if (
        failures >= FLAKY_MIN_FAILURES
        and intermittency is not None
        and intermittency >= FLAKY_INTERMITTENCY_THRESHOLD
    ):
        return CLASS_FLAKY
    return CLASS_DEGRADED


def _rank_key(metric: AgentFlakiness):
    """Flakiest first: intermittency desc (nulls last), failure_rate desc, name."""
    return (
        metric.intermittency is None,
        -(metric.intermittency if metric.intermittency is not None else 0.0),
        -metric.failure_rate,
        metric.agent,
    )


def render_flaky(metrics: List[AgentFlakiness], now: Optional[datetime] = None) -> str:
    """Render metrics as a ranked markdown table; ``now`` is injectable for tests."""
    if not metrics:
        return "No completed runs."
    if now is None:
        now = datetime.now(timezone.utc)
    lines = list(_MARKDOWN_HEADER)
    for metric in sorted(metrics, key=_rank_key):
        lines.append(
            "| %s | %s | %d | %.0f%% | %s | %s | %s |"
            % (
                metric.agent,
                metric.classification,
                metric.runs,
                metric.failure_rate * 100,
                "—" if metric.intermittency is None else "%.2f" % metric.intermittency,
                _format_duration(metric.mtbf_seconds),
                (
                    "—"
                    if metric.last_failure_ts is None
                    else "%s ago" % _humanize_age(metric.last_failure_ts, now)
                ),
            )
        )
    lines.append("")
    lines.append(_LEGEND)
    return "\n".join(lines)
