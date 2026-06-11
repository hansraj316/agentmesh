# AMP — AgentMesh Protocol v0.3

**Status:** Stable draft
**Format:** JSON

AMP is the open event format that AgentMesh components (SDK, store, CLI, and the
future daemon/dashboard) exchange. Every observable thing an agent does is one
AMP event.

v0.2 is backward compatible with v0.1: it only adds the optional `span_id` and
`parent_span_id` payload fields (see [Spans](#spans-v02)). v0.3 is backward
compatible with v0.2: it only adds the optional usage payload fields (see
[Usage](#usage-v03)). Every valid v0.1 or v0.2 event is a valid v0.3 event.

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

No other top-level fields are defined in v0.3. Consumers MUST ignore unknown
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

## Spans (v0.2)

v0.2 adds two **optional** payload fields to `agent_start`, `agent_end`,
`agent_error`, and `tool_call` events:

| Payload key      | Type   | Description |
|------------------|--------|-------------|
| `span_id`        | string | Identifies one agent invocation (one span) within a run. UUIDv4 recommended. Non-empty. The `agent_start` and matching `agent_end`/`agent_error` events of one invocation share the same `span_id`. |
| `parent_span_id` | string | The `span_id` of the enclosing invocation, if any. Omitted on root spans. |

Together these link nested agent invocations into a trace tree: consumers pair
start and terminal events by `span_id` and attach spans to their parents by
`parent_span_id`.

v0.1 events — events without span fields — remain valid: consumers MUST accept
them and SHOULD fall back to pairing start and terminal events by `agent` and
event order.

## Usage (v0.3)

v0.3 adds four **optional** payload fields to `agent_end` and `tool_call`
events, so consumers can roll up token consumption and spend per run, agent,
or model:

| Payload key     | Type   | Description |
|-----------------|--------|-------------|
| `input_tokens`  | int    | Prompt/input tokens consumed. Must be ≥ 0. |
| `output_tokens` | int    | Completion/output tokens produced. Must be ≥ 0. |
| `cost_usd`      | float  | Cost of this invocation/call in US dollars. Must be ≥ 0. |
| `model`         | string | Model identifier (e.g. `"claude-sonnet-4"`). Non-empty. |

Each field is independent: an event may carry any subset of them. Validation
applies **only when a key is present** — an `agent_end` or `tool_call` event
without usage fields is valid (so every v0.2 event remains valid), but a
present key with the wrong type or a negative value makes the event invalid.
Usage keys on other event types are not defined by this spec and are ignored
by consumers (payloads stay free-form there).

## Example

```json
{
  "event_id": "5f6b2c3a-9d1e-4d7b-8f0a-2c4e6a8b0d1f",
  "run_id": "run-7d3a1b9c",
  "agent": "researcher",
  "type": "agent_end",
  "ts": "2026-06-10T08:15:30.123456+00:00",
  "payload": {
    "function": "researcher_agent",
    "duration_ms": 1532.7,
    "span_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "parent_span_id": "9f8e7d6c-5b4a-4f3e-8d2c-1b0a9f8e7d6c",
    "input_tokens": 1200,
    "output_tokens": 350,
    "cost_usd": 0.0123,
    "model": "claude-sonnet-4"
  }
}
```

## Validation Rules

An event is valid iff:

1. All six fields are present.
2. `event_id`, `run_id`, and `agent` are non-empty strings.
3. `type` is one of `agent_start`, `agent_end`, `agent_error`, `message`, `tool_call`.
4. `ts` parses as ISO-8601 and is timezone-aware with a UTC offset of zero.
5. `payload` is a JSON object (string keys).
6. On `agent_end` and `tool_call` events, **if present**: `input_tokens` and
   `output_tokens` are non-negative integers, `cost_usd` is a non-negative
   number, and `model` is a non-empty string. Absent usage keys are always
   fine — v0.1/v0.2 events validate unchanged.
