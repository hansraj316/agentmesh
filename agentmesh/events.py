"""AMP v0.3 event model. See docs/amp-spec.md."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

EVENT_TYPES = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_error",
        "message",
        "tool_call",
    }
)

# Event types that may carry the optional AMP v0.3 usage payload fields.
USAGE_EVENT_TYPES = frozenset({"agent_end", "tool_call"})


def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(ts: str) -> datetime:
    # Python 3.9's fromisoformat does not accept a trailing "Z".
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _validate_usage(payload: Dict[str, Any]) -> None:
    """Validate the optional AMP v0.3 usage payload keys, if present.

    Absent keys are always fine (v0.1/v0.2 events validate unchanged); a
    present key with the wrong type or a negative value raises ValueError.
    """
    for name in ("input_tokens", "output_tokens"):
        if name in payload:
            value = payload[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("%s must be a non-negative integer, got %r" % (name, value))
    if "cost_usd" in payload:
        value = payload["cost_usd"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError("cost_usd must be a non-negative number, got %r" % (value,))
    if "model" in payload:
        value = payload["model"]
        if not isinstance(value, str) or not value:
            raise ValueError("model must be a non-empty string, got %r" % (value,))


@dataclass(frozen=True)
class Event:
    """A single AMP v0.3 event."""

    event_id: str
    run_id: str
    agent: str
    type: str
    ts: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Raise ValueError if this event violates the AMP v0.3 spec."""
        for name in ("event_id", "run_id", "agent"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError("%s must be a non-empty string, got %r" % (name, value))
        if self.type not in EVENT_TYPES:
            raise ValueError("type must be one of %s, got %r" % (sorted(EVENT_TYPES), self.type))
        if not isinstance(self.ts, str):
            raise ValueError("ts must be an ISO-8601 string, got %r" % (self.ts,))
        try:
            parsed = _parse_ts(self.ts)
        except ValueError:
            raise ValueError("ts is not valid ISO-8601: %r" % (self.ts,))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("ts must be timezone-aware UTC: %r" % (self.ts,))
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict, got %r" % (self.payload,))
        if self.type in USAGE_EVENT_TYPES:
            _validate_usage(self.payload)

    @classmethod
    def new(
        cls,
        run_id: str,
        agent: str,
        type: str,
        payload: Dict[str, Any],
    ) -> "Event":
        """Create a validated event with a fresh event_id and current UTC ts."""
        return cls(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type=type,
            ts=utc_now_iso(),
            payload=payload,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "agent": self.agent,
            "type": self.type,
            "ts": self.ts,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        return cls(
            event_id=data["event_id"],
            run_id=data["run_id"],
            agent=data["agent"],
            type=data["type"],
            ts=data["ts"],
            payload=data["payload"],
        )
