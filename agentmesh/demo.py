"""Demo sandbox: seed a believable multi-agent dataset and print a guided tour.

``agentmesh demo`` seeds a self-contained demo database that every consumer
(board, tail, trace, diff, costs, alerts, serve) renders something
interesting from:

* ``demo-run-001`` — a healthy orchestrator → researcher/writer/reviewer
  pipeline with token & cost usage on every span.
* ``demo-run-002`` — the same pipeline, but the writer fails with a timeout
  and is ~40% slower, so ``agentmesh diff demo-run-001 demo-run-002`` shows
  both a duration regression and a status change.
* ``gha-demo-123`` — a flat GitHub-Actions-shaped run (start + end with a
  conclusion payload), so the board shows a mixed fleet.
* ``silent-agent`` — a single heartbeat 3 hours old, so the demo silent
  alert rule fires immediately.

Seeding is fully deterministic: every event id, span id, timestamp,
duration, and usage number is either a fixed value or derived from the
injectable ``now``. Events are appended manually with explicit timestamps
(like the test fixtures) rather than through the SDK, so reseeding with the
same ``now`` reproduces the exact same event set, event ids included.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentmesh.events import Event
from agentmesh.store import EventStore

DEMO_DB_PATH = Path.home() / ".agentmesh" / "demo.db"

DEMO_RULES_FILENAME = "demo-alerts.json"

DEMO_MODEL = "claude-sonnet-4-6"

RUN_BASELINE = "demo-run-001"
RUN_REGRESSED = "demo-run-002"
RUN_GHA = "gha-demo-123"
RUN_SILENT = "demo-silent-001"

GHA_AGENT = "agentmesh/Daily CI"
SILENT_AGENT = "silent-agent"

# Rules sized so that `agentmesh alerts` fires on the freshly seeded demo db:
# the silent rule catches silent-agent (3h old), and the failure-streak rule
# catches the writer's demo-run-002 failure.
DEMO_RULES = [
    {"name": "demo-fleet-quiet", "type": "silent", "agent": "*", "max_silence_minutes": 60},
    {"name": "demo-writer-broken", "type": "failure_streak", "agent": "writer", "min_streak": 1},
]

_WRITER_ERROR = "Request timed out after 60.0s while streaming completion from %s" % DEMO_MODEL
_WRITER_ERROR_TYPE = "APITimeoutError"

_GHA_URL = "https://github.com/hansraj316/agentmesh/actions/runs/demo-123"

# Pipeline steps: (agent, start offset s, duration s, usage payload or None).
# A step with usage=None fails with the writer timeout instead of ending ok.
_BASELINE_STEPS = [
    ("researcher", 0.4, 12.5, {"input_tokens": 18200, "output_tokens": 2400, "cost_usd": 0.0712}),
    ("writer", 13.4, 8.0, {"input_tokens": 9400, "output_tokens": 3100, "cost_usd": 0.0518}),
    ("reviewer", 21.9, 4.5, {"input_tokens": 6200, "output_tokens": 900, "cost_usd": 0.0231}),
]

# Same shape, but the writer fails and is 40% slower (8.0s -> 11.2s).
_REGRESSED_STEPS = [
    ("researcher", 0.4, 13.1, {"input_tokens": 18650, "output_tokens": 2520, "cost_usd": 0.0734}),
    ("writer", 14.0, 11.2, None),
    ("reviewer", 25.7, 4.4, {"input_tokens": 6400, "output_tokens": 870, "cost_usd": 0.0236}),
]

# Orchestrator root span: (duration s, end offset s, usage payload).
_BASELINE_ROOT = (27.0, 27.0, {"input_tokens": 3800, "output_tokens": 600, "cost_usd": 0.0144})
_REGRESSED_ROOT = (30.0, 30.7, {"input_tokens": 3900, "output_tokens": 640, "cost_usd": 0.0150})


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


def _event(
    event_id: str,
    run_id: str,
    agent: str,
    type: str,
    ts: str,
    payload: Dict[str, Any],
) -> Event:
    return Event(event_id=event_id, run_id=run_id, agent=agent, type=type, ts=ts, payload=payload)


def _pipeline_run(run_id: str, base: datetime, steps: List[tuple], root: tuple) -> List[Event]:
    """Events of one orchestrator → researcher/writer/reviewer pipeline run."""
    root_span = "%s-orchestrator" % run_id
    events = [
        _event(
            "%s-orchestrator-start" % run_id,
            run_id,
            "orchestrator",
            "agent_start",
            _iso(base),
            {"function": "orchestrator", "span_id": root_span},
        )
    ]
    for agent, start_offset, duration, usage in steps:
        span_id = "%s-%s" % (run_id, agent)
        start = base + timedelta(seconds=start_offset)
        events.append(
            _event(
                "%s-%s-start" % (run_id, agent),
                run_id,
                agent,
                "agent_start",
                _iso(start),
                {"function": agent, "span_id": span_id, "parent_span_id": root_span},
            )
        )
        payload = {"function": agent, "span_id": span_id, "duration_seconds": duration}
        if usage is None:
            payload["error"] = _WRITER_ERROR
            payload["error_type"] = _WRITER_ERROR_TYPE
            type_ = "agent_error"
        else:
            payload.update(usage)
            payload["model"] = DEMO_MODEL
            type_ = "agent_end"
        events.append(
            _event(
                "%s-%s-end" % (run_id, agent),
                run_id,
                agent,
                type_,
                _iso(start + timedelta(seconds=duration)),
                payload,
            )
        )
    duration, end_offset, usage = root
    end_payload = {"function": "orchestrator", "span_id": root_span, "duration_seconds": duration}
    end_payload.update(usage)
    end_payload["model"] = DEMO_MODEL
    events.append(
        _event(
            "%s-orchestrator-end" % run_id,
            run_id,
            "orchestrator",
            "agent_end",
            _iso(base + timedelta(seconds=end_offset)),
            end_payload,
        )
    )
    return events


def _gha_run(base: datetime) -> List[Event]:
    """One flat GitHub-Actions-shaped run, in the ingest-gha event shape."""
    start_payload = {"html_url": _GHA_URL, "event": "schedule", "head_branch": "main"}
    end_payload = {
        "conclusion": "success",
        "html_url": _GHA_URL,
        "event": "schedule",
        "head_branch": "main",
        "duration_seconds": 95.0,
    }
    return [
        _event("%s-start" % RUN_GHA, RUN_GHA, GHA_AGENT, "agent_start", _iso(base), start_payload),
        _event(
            "%s-end" % RUN_GHA,
            RUN_GHA,
            GHA_AGENT,
            "agent_end",
            _iso(base + timedelta(seconds=95.0)),
            end_payload,
        ),
    ]


def demo_events(now: Optional[datetime] = None) -> List[Event]:
    """The full demo event set, oldest first; ``now`` is injectable for tests."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    events = [
        _event(
            "%s-heartbeat" % RUN_SILENT,
            RUN_SILENT,
            SILENT_AGENT,
            "message",
            _iso(now - timedelta(hours=3)),
            {"text": "heartbeat"},
        )
    ]
    events.extend(
        _pipeline_run(RUN_BASELINE, now - timedelta(hours=2), _BASELINE_STEPS, _BASELINE_ROOT)
    )
    events.extend(
        _pipeline_run(RUN_REGRESSED, now - timedelta(minutes=45), _REGRESSED_STEPS, _REGRESSED_ROOT)
    )
    events.extend(_gha_run(now - timedelta(minutes=30)))
    return events


def seed_demo(store: EventStore, now: Optional[datetime] = None) -> Dict[str, int]:
    """Seed the demo dataset into ``store``; returns {runs, agents, events}.

    Deterministic: all timestamps derive from ``now`` (injectable; defaults
    to the current UTC time) and everything else is fixed, including the
    event ids — reseeding the same events into one db therefore fails on the
    unique event_id constraint, which is why the CLI refuses to reseed a
    non-empty database without ``--fresh``.
    """
    events = demo_events(now)
    for event in events:
        store.append(event)
    return {
        "runs": len({event.run_id for event in events}),
        "agents": len({event.agent for event in events}),
        "events": len(events),
    }


def write_demo_rules(path: Union[str, Path]) -> Path:
    """Write the demo alert rules file (see ``DEMO_RULES``) to ``path``."""
    rules_path = Path(path)
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(json.dumps(DEMO_RULES, indent=2) + "\n", encoding="utf-8")
    return rules_path


def demo_tour(db_path: Union[str, Path]) -> str:
    """The guided-tour text printed after seeding: commands to try, in order."""
    db = str(db_path)
    rules = str(Path(db_path).parent / DEMO_RULES_FILENAME)
    steps = [
        (
            "agentmesh board --db %s" % db,
            "Fleet status board: one row per agent with status, errors, streak, and cost.",
        ),
        (
            "agentmesh tail --run %s -n 20 --db %s" % (RUN_REGRESSED, db),
            "Raw events of the failing run, one per line.",
        ),
        (
            "agentmesh trace %s --db %s" % (RUN_REGRESSED, db),
            "Span tree of the failing run — see exactly where the writer timed out.",
        ),
        (
            "agentmesh diff %s %s --db %s" % (RUN_BASELINE, RUN_REGRESSED, db),
            "Compare the good run against the bad one: slowdowns and status flips.",
        ),
        (
            "agentmesh costs --by agent --db %s" % db,
            "Token & cost rollup per agent (also try --by run or --by model).",
        ),
        (
            "agentmesh alerts --rules %s --db %s" % (rules, db),
            "Evaluate the demo alert rules — needs the demo rules file written next to the db.",
        ),
        (
            "agentmesh serve --db %s" % db,
            "Live board + JSON API at http://127.0.0.1:7777/ (Ctrl-C to stop).",
        ),
    ]
    lines = ["Guided tour — try these:", ""]
    for index, (command, blurb) in enumerate(steps, start=1):
        lines.append("  %d. %s" % (index, command))
        lines.append("     %s" % blurb)
    return "\n".join(lines)
