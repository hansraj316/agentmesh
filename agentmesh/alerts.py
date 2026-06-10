"""Alerting hooks: configurable rules evaluated against the event store.

Rules live in a JSON file (default ``~/.agentmesh/alerts.json``, override with
``$AGENTMESH_ALERTS`` or ``--rules``) and produce webhook/email-ready alert
payloads. Two rule types are supported:

* ``silent`` — fires if an agent's latest event is older than
  ``max_silence_minutes``.
* ``failure_streak`` — fires if an agent's consecutive-failure streak is at
  least ``min_streak``.
"""

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from agentmesh.board import AgentSummary, agent_summaries
from agentmesh.events import _parse_ts
from agentmesh.store import EventStore

DEFAULT_RULES_PATH = Path.home() / ".agentmesh" / "alerts.json"

RULE_SILENT = "silent"
RULE_FAILURE_STREAK = "failure_streak"

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

_RULE_TYPES = frozenset({RULE_SILENT, RULE_FAILURE_STREAK})

# The required threshold parameter for each rule type.
_RULE_PARAMS = {
    RULE_SILENT: "max_silence_minutes",
    RULE_FAILURE_STREAK: "min_streak",
}

_SEVERITY_EMOJI = {
    SEVERITY_WARNING: "🟡",
    SEVERITY_CRITICAL: "🔴",
}

_MARKDOWN_HEADER = [
    "| Rule | Severity | Agent | Message |",
    "|------|----------|-------|---------|",
]

_USER_AGENT = "agentmesh"

Poster = Callable[[str, bytes, Dict[str, str]], None]


def default_rules_path() -> Path:
    """Resolve the rules file path: $AGENTMESH_ALERTS or ~/.agentmesh/alerts.json."""
    env = os.environ.get("AGENTMESH_ALERTS")
    if env:
        return Path(env)
    return DEFAULT_RULES_PATH


@dataclass(frozen=True)
class Rule:
    """One alert rule; ``threshold`` is the rule's required parameter
    (``max_silence_minutes`` for ``silent``, ``min_streak`` for
    ``failure_streak``). ``agent`` is an agent name or ``"*"`` for all."""

    name: str
    type: str
    agent: str
    threshold: Union[int, float]


@dataclass(frozen=True)
class Alert:
    """One fired rule, ready to render as markdown or a webhook payload."""

    rule_name: str
    rule_type: str
    agent: str
    severity: str
    message: str
    details: Dict[str, Any]
    fired_at: str


def load_rules(path: Optional[Union[str, Path]] = None) -> List[Rule]:
    """Load alert rules from a JSON file; a missing file means no rules.

    Raises ValueError naming the offending rule on malformed input
    (unknown type, missing or non-positive threshold params).
    """
    rules_path = Path(path) if path is not None else default_rules_path()
    if not rules_path.exists():
        return []
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("%s is not valid JSON: %s" % (rules_path, exc))
    if not isinstance(data, list):
        raise ValueError(
            "%s must contain a JSON list of rules, got %s" % (rules_path, type(data).__name__)
        )
    return [_parse_rule(item) for item in data]


def _parse_rule(data: object) -> Rule:
    """Validate one raw rule dict and build a Rule from it."""
    if not isinstance(data, dict):
        raise ValueError("each rule must be a JSON object, got %r" % (data,))
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("rule name must be a non-empty string, got %r" % (name,))
    type_ = data.get("type")
    if type_ not in _RULE_TYPES:
        raise ValueError(
            "rule %r: type must be one of %s, got %r" % (name, sorted(_RULE_TYPES), type_)
        )
    agent = data.get("agent", "*")
    if not isinstance(agent, str) or not agent:
        raise ValueError("rule %r: agent must be a non-empty string, got %r" % (name, agent))
    param = _RULE_PARAMS[type_]
    threshold = data.get(param)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
        raise ValueError("rule %r: %s must be a positive number, got %r" % (name, param, threshold))
    return Rule(name=name, type=type_, agent=agent, threshold=threshold)


def evaluate(
    store: EventStore,
    rules: List[Rule],
    now: Optional[datetime] = None,
) -> List[Alert]:
    """Evaluate rules against the store's agent summaries; ``now`` is injectable.

    Returns one Alert per (rule, matching agent) that fired, in rule order
    then agent-name order. Aggregation (streaks, last event) comes from
    ``board.agent_summaries``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    summaries = agent_summaries(store)
    alerts = []
    for rule in rules:
        for summary in summaries:
            if rule.agent != "*" and summary.agent != rule.agent:
                continue
            alert = _check(rule, summary, now)
            if alert is not None:
                alerts.append(alert)
    return alerts


def _check(rule: Rule, summary: AgentSummary, now: datetime) -> Optional[Alert]:
    """Apply one rule to one agent summary; None when it does not fire."""
    if rule.type == RULE_SILENT:
        silence_minutes = (now - _parse_ts(summary.last_ts)).total_seconds() / 60.0
        if silence_minutes <= rule.threshold:
            return None
        return Alert(
            rule_name=rule.name,
            rule_type=rule.type,
            agent=summary.agent,
            severity=SEVERITY_WARNING,
            message="agent %s has been silent for %dm (max %dm)"
            % (summary.agent, silence_minutes, rule.threshold),
            details={
                "silence_minutes": round(silence_minutes, 1),
                "max_silence_minutes": rule.threshold,
                "last_ts": summary.last_ts,
            },
            fired_at=now.isoformat(),
        )
    if summary.streak < rule.threshold:
        return None
    return Alert(
        rule_name=rule.name,
        rule_type=rule.type,
        agent=summary.agent,
        severity=SEVERITY_CRITICAL,
        message="agent %s has failed %d runs in a row (min %d)"
        % (summary.agent, summary.streak, rule.threshold),
        details={
            "streak": summary.streak,
            "min_streak": rule.threshold,
            "last_ts": summary.last_ts,
        },
        fired_at=now.isoformat(),
    )


def render_alerts_markdown(alerts: List[Alert]) -> str:
    """Render fired alerts as a markdown table, one row per alert."""
    lines = list(_MARKDOWN_HEADER)
    for alert in alerts:
        lines.append(
            "| %s | %s %s | %s | %s |"
            % (
                alert.rule_name,
                _SEVERITY_EMOJI[alert.severity],
                alert.severity,
                alert.agent,
                alert.message,
            )
        )
    return "\n".join(lines)


def to_webhook_payloads(alerts: List[Alert]) -> List[Dict[str, Any]]:
    """Shape alerts as JSON-serializable dicts for a generic webhook POST."""
    return [
        {
            "source": "agentmesh",
            "severity": alert.severity,
            "title": "%s: %s" % (alert.rule_name, alert.agent),
            "body": alert.message,
            "details": alert.details,
            "fired_at": alert.fired_at,
        }
        for alert in alerts
    ]


def _default_poster(url: str, data: bytes, headers: Dict[str, str]) -> None:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request) as response:
        response.read()


def post_webhook(url: str, payload: Dict[str, Any], poster: Optional[Poster] = None) -> None:
    """POST one alert payload to ``url`` as JSON; raises on HTTP errors.

    ``poster(url, data_bytes, headers)`` is injectable for tests. No retries.
    """
    if poster is None:
        poster = _default_poster
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    poster(url, data, headers)
