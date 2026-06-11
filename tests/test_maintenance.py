import uuid
from datetime import datetime, timedelta, timezone

import pytest

from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.maintenance import _format_size, prune_events, render_stats, store_stats

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    """datetime stand-in whose now() is pinned to the seeded timeline."""

    @classmethod
    def now(cls, tz=None):
        return NOW


def _ts(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="microseconds")


def _event(run_id, agent, type, days_ago):
    return Event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        agent=agent,
        type=type,
        ts=_ts(days_ago),
        payload={},
    )


@pytest.fixture
def seeded_store(store):
    # run-old: fully older than a 30-day cutoff; run-straddle: spans the cutoff.
    store.append(_event("run-old", "researcher", "agent_start", 40))
    store.append(_event("run-old", "researcher", "agent_end", 39))
    store.append(_event("run-straddle", "researcher", "agent_start", 35))
    store.append(_event("run-straddle", "researcher", "agent_end", 5))
    store.append(_event("run-new", "writer", "agent_start", 2))
    store.append(_event("run-new", "writer", "agent_error", 1))
    return store


def test_store_stats_counts(seeded_store):
    stats = store_stats(seeded_store)
    assert stats["total_events"] == 6
    assert stats["events_by_type"] == {
        "agent_start": 3,
        "agent_end": 2,
        "agent_error": 1,
    }
    assert stats["distinct_agents"] == 2
    assert stats["distinct_runs"] == 3


def test_store_stats_ts_range(seeded_store):
    stats = store_stats(seeded_store)
    assert stats["oldest_ts"] == _ts(40)
    assert stats["newest_ts"] == _ts(1)


def test_store_stats_db_size_present(seeded_store):
    stats = store_stats(seeded_store)
    assert isinstance(stats["db_size_bytes"], int)
    assert stats["db_size_bytes"] > 0


def test_store_stats_empty_store(store):
    stats = store_stats(store)
    assert stats["total_events"] == 0
    assert stats["events_by_type"] == {}
    assert stats["oldest_ts"] is None
    assert stats["newest_ts"] is None


def test_db_size_none_for_missing_file(tmp_path, store):
    store.path = tmp_path / "does-not-exist.db"
    assert store.db_size_bytes() is None


def test_render_stats(seeded_store):
    markdown = render_stats(store_stats(seeded_store))
    assert "- Events: 6" in markdown
    assert "- Agents: 2" in markdown
    assert "- Runs: 3" in markdown
    assert "| Type | Count |" in markdown
    assert "| agent_start | 3 |" in markdown
    assert "- Range: %s → %s" % (_ts(40), _ts(1)) in markdown
    assert "- DB size:" in markdown
    assert "unknown" not in markdown


def test_render_stats_empty(store):
    markdown = render_stats(store_stats(store))
    assert "- Events: 0" in markdown
    assert "no events" in markdown
    assert "| Type | Count |" not in markdown


def test_format_size():
    assert _format_size(None) == "unknown"
    assert _format_size(512) == "512 B"
    assert _format_size(2048) == "2.0 KB"
    assert _format_size(int(1.2 * 1024 * 1024)) == "1.2 MB"
    assert _format_size(3 * 1024**3) == "3.0 GB"


def test_prune_deletes_only_fully_old_runs(seeded_store):
    result = prune_events(seeded_store, 30, now=NOW)
    assert result == {"deleted": 2, "remaining": 4}
    remaining_runs = {e.run_id for e in seeded_store.query(limit=100)}
    assert remaining_runs == {"run-straddle", "run-new"}


def test_prune_never_splits_a_straddling_run(seeded_store):
    # run-straddle has an event 35 days old (past the cutoff) and one 5 days
    # old (within it): the whole run, including the old event, must survive.
    prune_events(seeded_store, 30, now=NOW)
    straddle = seeded_store.query(run_id="run-straddle", limit=100)
    assert len(straddle) == 2
    assert {e.ts for e in straddle} == {_ts(35), _ts(5)}


def test_prune_cutoff_math(seeded_store):
    # With a 41-day window nothing is old enough; with 0 days everything goes.
    assert prune_events(seeded_store, 41, now=NOW) == {"deleted": 0, "remaining": 6}
    assert prune_events(seeded_store, 0, now=NOW) == {"deleted": 6, "remaining": 0}
    assert seeded_store.count_events() == 0


def test_prune_dry_run_reports_but_deletes_nothing(seeded_store):
    result = prune_events(seeded_store, 30, now=NOW, dry_run=True)
    assert result == {"deleted": 2, "remaining": 4}
    assert seeded_store.count_events() == 6
    # A real run then deletes exactly what the dry run reported.
    assert prune_events(seeded_store, 30, now=NOW) == {"deleted": 2, "remaining": 4}


def test_vacuum_does_not_error(seeded_store):
    seeded_store.vacuum()
    assert seeded_store.count_events() == 6


def test_cli_stats(seeded_store, db_path, capsys):
    assert main(["stats", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "- Events: 6" in out
    assert "| agent_start | 3 |" in out


def test_cli_prune_with_day_suffix(seeded_store, db_path, monkeypatch, capsys):
    monkeypatch.setattr("agentmesh.maintenance.datetime", _FixedDatetime)
    assert main(["prune", "--older-than", "30d", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "deleted 2 event(s), 4 remaining" in out
    assert seeded_store.count_events() == 4


def test_cli_prune_with_bare_int(seeded_store, db_path, monkeypatch, capsys):
    monkeypatch.setattr("agentmesh.maintenance.datetime", _FixedDatetime)
    assert main(["prune", "--older-than", "30", "--db", str(db_path)]) == 0
    assert "deleted 2 event(s)" in capsys.readouterr().out


def test_cli_prune_dry_run(seeded_store, db_path, monkeypatch, capsys):
    monkeypatch.setattr("agentmesh.maintenance.datetime", _FixedDatetime)
    assert main(["prune", "--older-than", "30d", "--dry-run", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "would delete 2 event(s)" in out
    assert "4 would remain" in out
    assert seeded_store.count_events() == 6


@pytest.mark.parametrize("bad", ["abc", "30x", "-1d", ""])
def test_cli_prune_invalid_older_than_exits_nonzero(db_path, bad):
    with pytest.raises(SystemExit) as excinfo:
        main(["prune", "--older-than", bad, "--db", str(db_path)])
    assert excinfo.value.code != 0
