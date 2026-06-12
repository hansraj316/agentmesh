import uuid
from datetime import datetime, timezone

import pytest

from agentmesh.cli import main
from agentmesh.demo import seed_demo
from agentmesh.events import Event
from agentmesh.flaky import (
    OUTCOME_FAILED,
    OUTCOME_OK,
    RunOutcome,
    agent_outcomes,
    flakiness_metrics,
    render_flaky,
)

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minute, second=0):
    """A UTC timestamp within the hour before NOW."""
    return "2026-06-10T11:%02d:%02d+00:00" % (minute, second)


def _append(store, run_id, agent, type, ts, payload=None):
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type=type,
            ts=ts,
            payload=payload if payload is not None else {},
        )
    )


def _run(store, run_id, agent, minute, ok=True):
    """One completed run: agent_start, then agent_end or agent_error."""
    _append(store, run_id, agent, "agent_start", _ts(minute))
    if ok:
        _append(store, run_id, agent, "agent_end", _ts(minute, 30))
    else:
        _append(store, run_id, agent, "agent_error", _ts(minute, 30), {"error": "boom"})


def _history(pattern, agent="solver"):
    """Outcomes dict from a pattern string: "O" = ok run, "F" = failed run."""
    outcomes = [
        RunOutcome(
            run_id="r%d" % index,
            outcome=OUTCOME_FAILED if char == "F" else OUTCOME_OK,
            ts=_ts(index),
        )
        for index, char in enumerate(pattern)
    ]
    return {agent: outcomes}


def _metric(pattern):
    return flakiness_metrics(_history(pattern))[0]


# ---------------------------------------------------------------- outcomes


def test_outcomes_multi_run_multi_agent(store):
    _run(store, "r1", "researcher", 0, ok=True)
    _run(store, "r2", "researcher", 5, ok=False)
    _run(store, "r1", "writer", 1, ok=False)
    outcomes = agent_outcomes(store)
    assert sorted(outcomes) == ["researcher", "writer"]
    researcher = outcomes["researcher"]
    assert [(o.run_id, o.outcome) for o in researcher] == [("r1", "ok"), ("r2", "failed")]
    assert researcher[0].ts == _ts(0, 30)  # the terminal event's ts, not the start's
    assert [(o.run_id, o.outcome) for o in outcomes["writer"]] == [("r1", "failed")]


def test_outcomes_are_chronological_even_when_runs_interleave(store):
    _append(store, "slow", "solver", "agent_start", _ts(0))
    _append(store, "fast", "solver", "agent_start", _ts(1))
    _append(store, "fast", "solver", "agent_end", _ts(2))
    _append(store, "slow", "solver", "agent_error", _ts(3), {"error": "boom"})
    assert [o.run_id for o in agent_outcomes(store)["solver"]] == ["fast", "slow"]


def test_outcomes_ignore_still_running(store):
    _run(store, "r1", "solver", 0, ok=True)
    _append(store, "r2", "solver", "agent_start", _ts(5))
    _append(store, "w1", "writer", "agent_start", _ts(6))  # writer never completes
    outcomes = agent_outcomes(store)
    assert [o.run_id for o in outcomes["solver"]] == ["r1"]
    assert "writer" not in outcomes


def test_outcomes_failed_if_agent_errored_anywhere_in_run(store):
    _append(store, "r1", "solver", "agent_end", _ts(0))
    _append(store, "r1", "solver", "agent_error", _ts(1), {"error": "late boom"})
    assert agent_outcomes(store)["solver"][0].outcome == "failed"


def test_outcomes_window_keeps_most_recent(store):
    _run(store, "r1", "solver", 0, ok=False)
    _run(store, "r2", "solver", 5, ok=True)
    _run(store, "r3", "solver", 10, ok=False)
    outcomes = agent_outcomes(store, window=2)
    assert [o.run_id for o in outcomes["solver"]] == ["r2", "r3"]


def test_outcomes_window_must_be_positive(store):
    with pytest.raises(ValueError):
        agent_outcomes(store, window=0)


def test_outcomes_since_filters_out_older_events(store):
    _run(store, "r1", "solver", 0, ok=False)
    _run(store, "r2", "solver", 30, ok=True)
    outcomes = agent_outcomes(store, since=_ts(20))
    assert [o.run_id for o in outcomes["solver"]] == ["r2"]


# ------------------------------------------------------- intermittency math


def test_intermittency_all_same_is_zero():
    assert _metric("FFF").intermittency == 0.0
    assert _metric("OOO").intermittency == 0.0


def test_intermittency_perfect_alternation_is_one():
    assert _metric("OFOF").intermittency == 1.0


def test_intermittency_none_for_single_run():
    assert _metric("F").intermittency is None


def test_intermittency_counts_adjacent_changes():
    # O F F O -> changes at positions 0->1 and 2->3: 2 / 3.
    assert _metric("OFFO").intermittency == pytest.approx(2 / 3)


# ----------------------------------------------------------------- MTBF math


def test_mtbf_is_mean_of_consecutive_failure_deltas():
    outcomes = {
        "solver": [
            RunOutcome("r1", OUTCOME_FAILED, _ts(0)),  # 11:00:00
            RunOutcome("r2", OUTCOME_OK, _ts(2)),
            RunOutcome("r3", OUTCOME_FAILED, _ts(5)),  # +300s
            RunOutcome("r4", OUTCOME_FAILED, _ts(20)),  # +900s
        ]
    }
    metric = flakiness_metrics(outcomes)[0]
    assert metric.mtbf_seconds == pytest.approx(600.0)  # mean(300, 900)


def test_mtbf_none_with_fewer_than_two_failures():
    assert _metric("OFO").mtbf_seconds is None
    assert _metric("OOO").mtbf_seconds is None


def test_last_failure_ts():
    metric = _metric("FOFO")
    assert metric.last_failure_ts == _ts(2)
    assert _metric("OOO").last_failure_ts is None


# ----------------------------------------------------------- classifications


def test_classify_stable_with_zero_failures():
    metric = _metric("OOOO")
    assert metric.classification == "stable"
    assert metric.failure_rate == 0.0


def test_classify_failing_at_failure_rate_exactly_point_eight():
    metric = _metric("FFFFO")  # 4 failures / 5 runs = 0.8 exactly -> failing (>=)
    assert metric.failure_rate == pytest.approx(0.8)
    assert metric.classification == "failing"


def test_classify_not_failing_just_below_point_eight():
    metric = _metric("FFFOO")  # 3/5 = 0.6; intermittency 1/4 = 0.25 -> degraded
    assert metric.classification == "degraded"


def test_classify_failing_single_failed_run():
    assert _metric("F").classification == "failing"  # rate 1.0, intermittency None


def test_classify_flaky_at_intermittency_exactly_point_three():
    # 11 runs, alternations F->O, O->F, F->O = 3 -> 3/10 = 0.3 exactly -> flaky (>=)
    metric = _metric("FOFOOOOOOOO")
    assert metric.intermittency == pytest.approx(0.3)
    assert metric.failures == 2
    assert metric.classification == "flaky"


def test_classify_degraded_just_below_intermittency_point_three():
    # 2 failures but only 2/10 alternations -> a burst, not flakiness.
    metric = _metric("OFFOOOOOOOO")
    assert metric.intermittency == pytest.approx(0.2)
    assert metric.classification == "degraded"


def test_classify_single_failure_is_degraded_even_when_alternating():
    assert _metric("OF").classification == "degraded"  # intermittency 1.0, but 1 failure


def test_failing_takes_precedence_over_flaky():
    metric = _metric("OFOFF")  # 3/5 failures = 0.6 -> not failing; 3/4 alternations
    assert metric.classification == "flaky"
    always_broken = _metric("FFFFF")  # rate 1.0, intermittency 0.0
    assert always_broken.classification == "failing"


# ------------------------------------------------------------------- render


def test_render_ranks_flakiest_first():
    outcomes = {}
    outcomes.update(_history("OOO", agent="steady"))  # intermittency 0.0
    outcomes.update(_history("OFOF", agent="flapper"))  # intermittency 1.0
    outcomes.update(_history("F", agent="newbie"))  # intermittency None -> last
    markdown = render_flaky(flakiness_metrics(outcomes), now=NOW)
    rows = [line for line in markdown.splitlines() if line.startswith("| ")][1:]
    assert [row.split(" | ")[0].lstrip("| ") for row in rows] == ["flapper", "steady", "newbie"]


def test_render_breaks_intermittency_ties_by_failure_rate_then_name():
    outcomes = {}
    outcomes.update(_history("FFFF", agent="bravo"))  # 0.0, rate 1.0
    outcomes.update(_history("OOOO", agent="alpha"))  # 0.0, rate 0.0
    outcomes.update(_history("OOOO", agent="aaa"))  # 0.0, rate 0.0 -> before alpha
    markdown = render_flaky(flakiness_metrics(outcomes), now=NOW)
    rows = [line for line in markdown.splitlines() if line.startswith("| ")][1:]
    assert [row.split(" | ")[0].lstrip("| ") for row in rows] == ["bravo", "aaa", "alpha"]


def test_render_columns_and_values():
    markdown = render_flaky(flakiness_metrics(_history("OFOF")), now=NOW)
    lines = markdown.splitlines()
    assert lines[0] == "| Agent | Class | Runs | Fail% | Intermittency | MTBF | Last failure |"
    # Failures at minutes 1 and 3 -> MTBF 120s = "2m 0s"; last failure 11:03 -> 57m ago.
    assert "| solver | flaky | 4 | 50% | 1.00 | 2m 0s | 57m ago |" in lines


def test_render_uses_dashes_for_missing_values():
    markdown = render_flaky(flakiness_metrics(_history("F")), now=NOW)
    assert "| solver | failing | 1 | 100% | — | — | 1h ago |" in markdown


def test_render_includes_legend():
    markdown = render_flaky(flakiness_metrics(_history("OF")), now=NOW)
    legend = markdown.splitlines()[-1]
    assert legend.startswith("Legend:")
    for name in ("stable", "failing", "flaky", "degraded"):
        assert name in legend


def test_render_empty_state():
    assert render_flaky([]) == "No completed runs."
    assert render_flaky(flakiness_metrics({})) == "No completed runs."


# ---------------------------------------------------------------------- CLI


def test_cli_flaky_prints_ranked_table(store, capsys):
    _run(store, "r1", "solver", 0, ok=True)
    _run(store, "r2", "solver", 5, ok=False)
    _run(store, "r3", "solver", 10, ok=True)
    _run(store, "r4", "solver", 15, ok=False)
    assert main(["flaky"]) == 0
    out = capsys.readouterr().out
    assert "| Agent | Class | Runs | Fail% | Intermittency | MTBF | Last failure |" in out
    assert "| solver | flaky | 4 | 50% | 1.00 |" in out
    assert "Legend:" in out


def test_cli_flaky_window_trims_history(store, capsys):
    _run(store, "r1", "solver", 0, ok=False)
    _run(store, "r2", "solver", 5, ok=True)
    _run(store, "r3", "solver", 10, ok=True)
    assert main(["flaky", "--window", "2"]) == 0
    out = capsys.readouterr().out
    assert "| solver | stable | 2 | 0% |" in out


def test_cli_flaky_since_filters(store, capsys):
    _run(store, "r1", "solver", 0, ok=False)
    _run(store, "r2", "solver", 30, ok=True)
    assert main(["flaky", "--since", _ts(20)]) == 0
    out = capsys.readouterr().out
    assert "| solver | stable | 1 | 0% |" in out


def test_cli_flaky_rejects_non_positive_window(store, capsys):
    assert main(["flaky", "--window", "0"]) == 1
    assert "window must be a positive integer" in capsys.readouterr().err


def test_cli_flaky_empty_store(store, capsys):
    assert main(["flaky"]) == 0
    assert "No completed runs." in capsys.readouterr().out


def test_cli_flaky_on_demo_data(db_path, capsys):
    from agentmesh.store import EventStore

    seed_demo(EventStore(db_path), now=NOW)
    assert main(["flaky", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    # The demo writer fails 1 of its 2 runs (ok then failed -> alternating).
    assert "| writer | degraded | 2 | 50% | 1.00 | — |" in out
    assert "| orchestrator | stable | 2 | 0% | 0.00 | — | — |" in out
    assert "silent-agent" not in out  # heartbeat only, never completed a run
