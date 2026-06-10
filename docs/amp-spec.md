# AMP — AgentMesh Protocol v0.1

**Status:** Stable draft
**Format:** JSON

AMP is the open event format that AgentMesh components (SDK, store, CLI, and the
future daemon/dashboard) exchange. Every observable thing an agent does is one
AMP event.

## Event Envelope

Every event is a single JSON object with exactly these fields:

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `event_id` | string | yes      | Unique id for this event. UUIDv4 recommended. Non-empty. |
| `run_id`   | string | yes      | Groups all events of one logical run (e.g. one orchestration). Non-empty. |
| `agent`    | string | yes      | Human-readable agent name (e.g. `"researcher"`). Non-empty. |
| `type`     | string | yes      | One of the event types below. |
| `ts`       | string | yes      | Event time, ISO-8601 in UTC (e.g. `2026-06-10T08:15:30.123456+00:00`; a trailing `Z` is also accepted). |
| `payload`  | object | yes      | Type-specific data. Must be a JSON object; may be empty (`{}`). |

No other top-level fields are defined in v0.1. Consumers MUST ignore unknown
top-level fields to stay forward-compatible.

## Event Types

| `type`        | Emitted when | Conventional payload keys |
|---------------|--------------|---------------------------|
| `agent_start` | An agent invocation begins | `function` |
| `agent_end`   | An agent invocation returns successfully | `function`, `duration_ms` |
| `agent_error` | An agent invocation raises | `function`, `duration_ms`, `error`, `error_type` |
| `message`     | An agent sends/receives a message | free-form |
| `tool_call`   | An agent invokes a tool | free-form (e.g. `tool`, `input`) |

Payload keys are conventions, not requirements: any JSON object is valid.

## Example

```json
{
  "event_id": "5f6b2c3a-9d1e-4d7b-8f0a-2c4e6a8b0d1f",
  "run_id": "run-7d3a1b9c",
  "agent": "researcher",
  "type": "agent_end",
  "ts": "2026-06-10T08:15:30.123456+00:00",
  "payload": {"function": "researcher_agent", "duration_ms": 1532.7}
}
```

## Validation Rules

An event is valid iff:

1. All six fields are present.
2. `event_id`, `run_id`, and `agent` are non-empty strings.
3. `type` is one of `agent_start`, `agent_end`, `agent_error`, `message`, `tool_call`.
4. `ts` parses as ISO-8601 and is timezone-aware with a UTC offset of zero.
5. `payload` is a JSON object (string keys).
