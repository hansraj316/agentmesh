"""Tests for the OTLP/JSON export (agentmesh/otlp.py and the CLI command)."""

import hashlib
import json

import pytest

from agentmesh import __version__
from agentmesh.cli import main
from agentmesh.demo import seed_demo
from agentmesh.events import Event
from agentmesh.otlp import derive_span_id, derive_trace_id, export_otlp, trace_to_otlp
from agentmesh.trace import build_trace

RUN = "run-otlp"
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:00:01+00:00"
T2 = "2026-01-01T00:00:02.250000+00:00"
T0_NANOS = 1767225600 * 10**9


def _event(store, agent, type, ts, payload, run_id=RUN, n=[0]):
    n[0] += 1
    store.append(
        Event(
            event_id="evt-%03d" % n[0],
            run_id=run_id,
            agent=agent,
            type=type,
            ts=ts,
            payload=payload,
        )
    )


def _seed_run(store):
    """orchestrator -> researcher (ok, usage) + writer (failed)."""
    _event(store, "orchestrator", "agent_start", T0, {"span_id": "s-root"})
    _event(
        store,
        "researcher",
        "agent_start",
        T0,
        {"span_id": "s-research", "parent_span_id": "s-root"},
    )
    _event(
        store,
        "researcher",
        "agent_end",
        T1,
        {
            "span_id": "s-research",
            "duration_seconds": 1.0,
            "input_tokens": 1200,
            "output_tokens": 340,
            "cost_usd": 0.0125,
            "model": "claude-opus-4",
        },
    )
    _event(
        store,
        "writer",
        "agent_start",
        T1,
        {"span_id": "s-write", "parent_span_id": "s-root"},
    )
    _event(
        store,
        "writer",
        "agent_error",
        T2,
        {
            "span_id": "s-write",
            "duration_seconds": 1.25,
            "error": "bad draft\nstack detail",
            "error_type": "ValueError",
        },
    )
    _event(store, "orchestrator", "agent_end", T2, {"span_id": "s-root", "duration_seconds": 2.25})


def _spans(document):
    return document["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _by_name(document):
    return {span["name"]: span for span in _spans(document)}


def _attrs(span):
    return {attr["key"]: attr["value"] for attr in span["attributes"]}


# --- id derivation -------------------------------------------------------


def test_trace_id_is_deterministic_md5_of_run_id():
    assert derive_trace_id(RUN) == hashlib.md5(RUN.encode("utf-8")).hexdigest()
    assert derive_trace_id(RUN) == derive_trace_id(RUN)
    assert len(derive_trace_id(RUN)) == 32
    int(derive_trace_id(RUN), 16)  # valid hex


def test_span_id_is_deterministic_truncated_md5():
    assert derive_span_id("s-root") == hashlib.md5(b"s-root").hexdigest()[:16]
    assert derive_span_id("s-root") == derive_span_id("s-root")
    assert len(derive_span_id("s-root")) == 16
    int(derive_span_id("s-root"), 16)  # valid hex


def test_same_store_exports_identical_documents(store):
    _seed_run(store)
    assert export_otlp(store, RUN) == export_otlp(store, RUN)


def test_parent_linkage_matches_trace_tree(store):
    _seed_run(store)
    spans = _by_name(export_otlp(store, RUN))
    root = spans["orchestrator"]
    assert "parentSpanId" not in root
    assert root["spanId"] == derive_span_id("s-root")
    for child in ("researcher", "writer"):
        assert spans[child]["parentSpanId"] == root["spanId"]
    assert all(span["traceId"] == derive_trace_id(RUN) for span in spans.values())


# --- status & timestamps -------------------------------------------------


def test_ok_and_failed_status_mapping(store):
    _seed_run(store)
    spans = _by_name(export_otlp(store, RUN))
    assert spans["researcher"]["status"] == {"code": "STATUS_CODE_OK"}
    assert spans["writer"]["status"] == {
        "code": "STATUS_CODE_ERROR",
        "message": "ValueError: bad draft",  # first line of the error only
    }


def test_nano_timestamps_are_exact_strings(store):
    _seed_run(store)
    spans = _by_name(export_otlp(store, RUN))
    researcher = spans["researcher"]
    assert researcher["startTimeUnixNano"] == str(T0_NANOS)
    assert researcher["endTimeUnixNano"] == str(T0_NANOS + 10**9)  # 1.0s later
    writer = spans["writer"]
    assert writer["startTimeUnixNano"] == str(T0_NANOS + 10**9)
    assert writer["endTimeUnixNano"] == str(T0_NANOS + 10**9 + 1_250_000_000)


# --- attributes ----------------------------------------------------------


def test_usage_attributes_use_otlp_typed_values(store):
    _seed_run(store)
    attrs = _attrs(_by_name(export_otlp(store, RUN))["researcher"])
    assert attrs["agentmesh.run_id"] == {"stringValue": RUN}
    assert attrs["agentmesh.span_id"] == {"stringValue": "s-research"}
    assert attrs["agentmesh.parent_span_id"] == {"stringValue": "s-root"}
    # OTLP JSON encodes intValue as a decimal *string*.
    assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "1200"}
    assert attrs["gen_ai.usage.output_tokens"] == {"intValue": "340"}
    assert attrs["agentmesh.cost_usd"] == {"doubleValue": 0.0125}
    assert attrs["gen_ai.request.model"] == {"stringValue": "claude-opus-4"}


def test_usage_attributes_absent_without_usage(store):
    _seed_run(store)
    attrs = _attrs(_by_name(export_otlp(store, RUN))["writer"])
    for key in (
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "agentmesh.cost_usd",
        "gen_ai.request.model",
    ):
        assert key not in attrs
    assert attrs["agentmesh.run_id"] == {"stringValue": RUN}


# --- running spans -------------------------------------------------------


def test_running_spans_are_skipped(store):
    _seed_run(store)
    _event(store, "straggler", "agent_start", T2, {"span_id": "s-open", "parent_span_id": "s-root"})
    names = set(_by_name(export_otlp(store, RUN)))
    assert names == {"orchestrator", "researcher", "writer"}


# --- document shape ------------------------------------------------------


def test_document_top_level_shape(store):
    _seed_run(store)
    document = export_otlp(store, RUN)
    assert set(document) == {"resourceSpans"}
    (resource_spans,) = document["resourceSpans"]
    assert resource_spans["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "agentmesh"}}
    ]
    (scope_spans,) = resource_spans["scopeSpans"]
    assert scope_spans["scope"] == {"name": "agentmesh", "version": __version__}
    assert len(scope_spans["spans"]) == 3
    for span in scope_spans["spans"]:
        assert {"traceId", "spanId", "name", "kind", "status"} <= set(span)
        assert span["kind"] == "SPAN_KIND_INTERNAL"


def test_document_round_trips_through_json(store):
    _seed_run(store)
    document = export_otlp(store, RUN)
    assert json.loads(json.dumps(document)) == document


def test_unknown_run_raises_value_error(store):
    with pytest.raises(ValueError):
        export_otlp(store, "no-such-run")


def test_trace_to_otlp_accepts_a_prebuilt_trace(store):
    _seed_run(store)
    assert trace_to_otlp(build_trace(store, RUN)) == export_otlp(store, RUN)


# --- CLI -----------------------------------------------------------------


def test_cli_export_otlp_prints_json(store, db_path, capsys):
    _seed_run(store)
    assert main(["export-otlp", RUN, "--db", str(db_path)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert len(_spans(document)) == 3


def test_cli_export_otlp_writes_out_file(store, db_path, tmp_path, capsys):
    _seed_run(store)
    out = tmp_path / "trace.json"
    assert main(["export-otlp", RUN, "--db", str(db_path), "--out", str(out)]) == 0
    assert capsys.readouterr().out == "Wrote %s\n" % out
    document = json.loads(out.read_text(encoding="utf-8"))
    assert len(_spans(document)) == 3


def test_cli_export_otlp_unknown_run_exits_1(db_path, capsys):
    assert main(["export-otlp", "no-such-run", "--db", str(db_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err and "no-such-run" in captured.err


# --- demo data integration ------------------------------------------------


def test_demo_runs_export_valid_otlp(store):
    seed_demo(store)
    run_ids = {event.run_id for _, event in store.events_after(0)}
    assert {"demo-run-001", "demo-run-002"} <= run_ids
    for run_id in sorted(run_ids):
        document = export_otlp(store, run_id)
        spans = _spans(document)
        # demo-silent-001 is a still-running span: skipped, so no spans.
        assert all(span["traceId"] == derive_trace_id(run_id) for span in spans)
        json.dumps(document)  # serializable
    healthy = _by_name(export_otlp(store, "demo-run-001"))
    assert "orchestrator" in healthy and len(healthy) >= 3
    assert any("gen_ai.usage.input_tokens" in _attrs(span) for span in healthy.values())
    regressed = _by_name(export_otlp(store, "demo-run-002"))
    assert any(span["status"]["code"] == "STATUS_CODE_ERROR" for span in regressed.values())
