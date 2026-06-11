"""Store maintenance: stats and whole-run-safe pruning for long-lived event stores."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from agentmesh.store import EventStore

_TYPE_TABLE_HEADER = [
    "| Type | Count |",
    "|------|-------|",
]


def store_stats(store: EventStore) -> Dict[str, Any]:
    """Summarize the store: event counts, distinct agents/runs, ts range, db size.

    ``oldest_ts``/``newest_ts`` are None for an empty store, and
    ``db_size_bytes`` is None when the database is not a regular file
    (e.g. in-memory or missing).
    """
    ts_range = store.ts_range()
    return {
        "total_events": store.count_events(),
        "events_by_type": store.count_by_type(),
        "distinct_agents": store.count_distinct_agents(),
        "distinct_runs": store.count_distinct_runs(),
        "oldest_ts": ts_range[0] if ts_range else None,
        "newest_ts": ts_range[1] if ts_range else None,
        "db_size_bytes": store.db_size_bytes(),
    }


def _format_size(size_bytes: Optional[int]) -> str:
    """Human-readable byte size: "512 B", "1.2 MB"; "unknown" for None."""
    if size_bytes is None:
        return "unknown"
    if size_bytes < 1024:
        return "%d B" % size_bytes
    value = float(size_bytes)
    unit = "B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0:
            break
    return "%.1f %s" % (value, unit)


def render_stats(stats: Dict[str, Any]) -> str:
    """Render ``store_stats()`` output as a short markdown block."""
    lines = [
        "## AgentMesh store stats",
        "",
        "- Events: %d" % stats["total_events"],
        "- Agents: %d" % stats["distinct_agents"],
        "- Runs: %d" % stats["distinct_runs"],
        "- DB size: %s" % _format_size(stats["db_size_bytes"]),
    ]
    if stats["oldest_ts"] is not None:
        lines.append("- Range: %s → %s" % (stats["oldest_ts"], stats["newest_ts"]))
    else:
        lines.append("- Range: — (no events)")
    if stats["events_by_type"]:
        lines.append("")
        lines.extend(_TYPE_TABLE_HEADER)
        for event_type, count in sorted(stats["events_by_type"].items()):
            lines.append("| %s | %d |" % (event_type, count))
    return "\n".join(lines)


def prune_events(
    store: EventStore,
    older_than_days: int,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Delete events older than ``older_than_days`` days, whole runs at a time.

    Integrity rule — never split a run: a run is pruned only when ALL of its
    events are older than the cutoff (``now - older_than_days``). If ANY event
    of a run is at or after the cutoff, every event of that run is kept, even
    the old ones, so traces always stay complete. After deleting, the database
    is VACUUMed to reclaim disk space.

    ``now`` defaults to the current UTC time and is injectable for tests.
    With ``dry_run=True`` nothing is deleted (and VACUUM is skipped); the
    counts report what a real run would do.

    Returns ``{"deleted": n, "remaining": m}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=older_than_days)).isoformat(timespec="microseconds")
    if dry_run:
        deleted = store.count_events_in_runs_ended_before(cutoff)
        remaining = store.count_events() - deleted
    else:
        deleted = store.delete_runs_ended_before(cutoff)
        store.vacuum()
        remaining = store.count_events()
    return {"deleted": deleted, "remaining": remaining}
