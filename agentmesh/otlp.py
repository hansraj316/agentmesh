"""Export a run's trace as OTLP/JSON for Jaeger, Grafana Tempo, etc.

Emits the JSON encoding of an OTLP ``ExportTraceServiceRequest``
(https://opentelemetry.io/docs/specs/otlp/) by hand — no OpenTelemetry SDK
dependency. The OTLP JSON mapping quirks handled here:

* ``traceId``/``spanId`` are lowercase hex strings (not base64).
* 64-bit integer fields (``startTimeUnixNano``, ``endTimeUnixNano``, and
  ``intValue`` attribute values) are encoded as decimal *strings*.
* Attribute values use the typed form ``{"stringValue": ...}`` /
  ``{"intValue": "..."}`` / ``{"doubleValue": ...}``.

Ids are derived deterministically (md5 of stable inputs), not generated
randomly: exporting the same run twice yields byte-identical documents, and
re-exports after new events arrive keep prior span ids stable. Only
completed spans (status ``ok`` or ``failed``) are exported; spans still
``running`` have no end time and are skipped.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agentmesh import __version__
from agentmesh.events import _parse_ts
from agentmesh.store import EventStore
from agentmesh.trace import STATUS_FAILED, STATUS_RUNNING, Span, Trace, build_trace

_SERVICE_NAME = "agentmesh"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def export_otlp(store: EventStore, run_id: str) -> Dict[str, Any]:
    """Build the OTLP/JSON document for one run.

    Raises ValueError if the store has no events for ``run_id`` (same
    contract as :func:`agentmesh.trace.build_trace`).
    """
    return trace_to_otlp(build_trace(store, run_id))


def trace_to_otlp(trace: Trace) -> Dict[str, Any]:
    """Map a :class:`~agentmesh.trace.Trace` to an OTLP/JSON dict.

    The document is a JSON-encoded ``ExportTraceServiceRequest`` with one
    resource (``service.name = "agentmesh"``) and one instrumentation scope.
    The ``traceId`` is a stable derivation from the run id (md5 hex digest of
    ``run_id``), so re-exports of the same run always share a trace id.
    """
    spans: List[Dict[str, Any]] = []
    counter = [0]  # mutable ordinal for spans without an AMP span_id (v0.1)
    trace_id = derive_trace_id(trace.run_id)
    for root in trace.roots:
        _append_span(spans, root, trace, trace_id, None, counter)
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_attr_str("service.name", _SERVICE_NAME)],
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "agentmesh", "version": __version__},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def derive_trace_id(run_id: str) -> str:
    """32-hex-char OTLP trace id, derived deterministically from the run id."""
    return hashlib.md5(run_id.encode("utf-8")).hexdigest()


def derive_span_id(key: str) -> str:
    """16-hex-char OTLP span id: md5 of the AMP span id, truncated."""
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _append_span(
    out: List[Dict[str, Any]],
    span: Span,
    trace: Trace,
    trace_id: str,
    parent_hex_id: Optional[str],
    counter: List[int],
) -> None:
    """Append ``span`` (if completed) and its subtree to ``out``, depth-first.

    Running spans are skipped (no end time yet) but their completed children
    are still exported, keeping their parent reference so the link is
    restored once the parent completes and the run is re-exported.
    """
    hex_id = derive_span_id(span.span_id if span.span_id else _v01_span_key(span, trace, counter))
    counter[0] += 1
    if span.status != STATUS_RUNNING:
        out.append(_otlp_span(span, trace, trace_id, hex_id, parent_hex_id))
    for child in span.children:
        _append_span(out, child, trace, trace_id, hex_id, counter)


def _v01_span_key(span: Span, trace: Trace, counter: List[int]) -> str:
    """Stable id input for v0.1 spans that carry no AMP ``span_id``.

    Uses the run id, agent, start ts, and the span's position in the
    depth-first walk, so it is deterministic for a given stored run.
    """
    return "%s|%s|%s|%d" % (trace.run_id, span.agent, span.started_ts, counter[0])


def _otlp_span(
    span: Span,
    trace: Trace,
    trace_id: str,
    hex_id: str,
    parent_hex_id: Optional[str],
) -> Dict[str, Any]:
    start_nanos = _unix_nanos(span.started_ts)
    end_nanos = start_nanos + int(round((span.duration_seconds or 0.0) * 1e9))
    otlp: Dict[str, Any] = {
        "traceId": trace_id,
        "spanId": hex_id,
        "name": span.agent,
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": str(start_nanos),
        "endTimeUnixNano": str(end_nanos),
        "attributes": _span_attributes(span, trace),
        "status": _span_status(span),
    }
    if parent_hex_id is not None:
        otlp["parentSpanId"] = parent_hex_id
    return otlp


def _span_status(span: Span) -> Dict[str, Any]:
    if span.status == STATUS_FAILED:
        status: Dict[str, Any] = {"code": "STATUS_CODE_ERROR"}
        first_line = span.error.splitlines()[0] if span.error else ""
        if span.error_type:
            message = "%s: %s" % (span.error_type, first_line) if first_line else span.error_type
        else:
            message = first_line
        if message:
            status["message"] = message
        return status
    return {"code": "STATUS_CODE_OK"}


def _span_attributes(span: Span, trace: Trace) -> List[Dict[str, Any]]:
    attributes = [_attr_str("agentmesh.run_id", trace.run_id)]
    if span.span_id is not None:
        attributes.append(_attr_str("agentmesh.span_id", span.span_id))
    if span.parent_span_id is not None:
        attributes.append(_attr_str("agentmesh.parent_span_id", span.parent_span_id))
    # Usage attributes follow the OTel GenAI semantic conventions.
    if span.input_tokens is not None:
        attributes.append(_attr_int("gen_ai.usage.input_tokens", span.input_tokens))
    if span.output_tokens is not None:
        attributes.append(_attr_int("gen_ai.usage.output_tokens", span.output_tokens))
    if span.cost_usd is not None:
        attributes.append(_attr_double("agentmesh.cost_usd", span.cost_usd))
    if span.model is not None:
        attributes.append(_attr_str("gen_ai.request.model", span.model))
    return attributes


def _unix_nanos(ts: str) -> int:
    """Exact nanoseconds since the Unix epoch for an ISO-8601 UTC string."""
    delta = _parse_ts(ts) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def _attr_str(key: str, value: str) -> Dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _attr_int(key: str, value: int) -> Dict[str, Any]:
    # OTLP JSON encodes 64-bit ints as decimal strings.
    return {"key": key, "value": {"intValue": str(value)}}


def _attr_double(key: str, value: float) -> Dict[str, Any]:
    return {"key": key, "value": {"doubleValue": value}}
