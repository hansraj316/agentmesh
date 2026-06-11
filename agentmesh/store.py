"""SQLite-backed AMP event store."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from agentmesh.events import Event

DEFAULT_DB_PATH = Path.home() / ".agentmesh" / "events.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

_RUNS_ENDED_BEFORE = "SELECT run_id FROM events GROUP BY run_id HAVING MAX(ts) < ?"


def default_db_path() -> Path:
    """Resolve the event db path: $AGENTMESH_DB or ~/.agentmesh/events.db."""
    env = os.environ.get("AGENTMESH_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


class EventStore:
    """Append-only SQLite store for AMP events.

    A short-lived connection is opened per operation, so a single store
    instance is safe to share across threads and processes.
    """

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def append(self, event: Event) -> None:
        """Validate and persist one event."""
        event.validate()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_id, run_id, agent, type, ts, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.agent,
                    event.type,
                    event.ts,
                    json.dumps(event.payload),
                ),
            )

    def append_ignore_duplicates(self, event: Event) -> bool:
        """Persist one event, skipping it if its event_id is already stored.

        Returns True if the event was inserted, False if it was a duplicate.
        Used by ingestors with deterministic event ids to stay idempotent.
        """
        event.validate()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events (event_id, run_id, agent, type, ts, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.agent,
                    event.type,
                    event.ts,
                    json.dumps(event.payload),
                ),
            )
        return cursor.rowcount == 1

    def query(
        self,
        run_id: Optional[str] = None,
        agent: Optional[str] = None,
        since: Optional[Union[str, datetime]] = None,
        limit: int = 50,
    ) -> List[Event]:
        """Return the most recent matching events in chronological order.

        ``since`` is an ISO-8601 UTC string (or datetime) compared against
        the event ``ts``; only events at or after it are returned.
        """
        where = []
        params: List[object] = []
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if agent is not None:
            where.append("agent = ?")
            params.append(agent)
        if since is not None:
            if isinstance(since, datetime):
                since = since.isoformat()
            where.append("ts >= ?")
            params.append(since)
        sql = "SELECT event_id, run_id, agent, type, ts, payload FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in reversed(rows)]

    def events_after(
        self,
        last_id: int,
        run_id: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> List[Tuple[int, Event]]:
        """Return (rowid, event) pairs newer than ``last_id``, oldest first.

        Used by ``agentmesh tail --follow`` to poll for new events.
        """
        where = ["id > ?"]
        params: List[object] = [last_id]
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if agent is not None:
            where.append("agent = ?")
            params.append(agent)
        sql = (
            "SELECT id, event_id, run_id, agent, type, ts, payload FROM events "
            "WHERE " + " AND ".join(where) + " ORDER BY id ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(row[0], self._row_to_event(row[1:])) for row in rows]

    def max_id(self) -> int:
        """Highest rowid currently in the store (0 if empty)."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
        return int(row[0])

    def count_events(self) -> int:
        """Total number of stored events."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def count_by_type(self) -> Dict[str, int]:
        """Event counts keyed by event type."""
        with self._connect() as conn:
            rows = conn.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def count_distinct_agents(self) -> int:
        """Number of distinct agents in the store."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT agent) FROM events").fetchone()
        return int(row[0])

    def count_distinct_runs(self) -> int:
        """Number of distinct run_ids in the store."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT run_id) FROM events").fetchone()
        return int(row[0])

    def ts_range(self) -> Optional[Tuple[str, str]]:
        """(oldest ts, newest ts) of stored events, or None if the store is empty."""
        with self._connect() as conn:
            row = conn.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        if row[0] is None:
            return None
        return (row[0], row[1])

    def count_events_in_runs_ended_before(self, cutoff: str) -> int:
        """How many events ``delete_runs_ended_before(cutoff)`` would delete."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id IN (%s)" % _RUNS_ENDED_BEFORE,
                (cutoff,),
            ).fetchone()
        return int(row[0])

    def delete_runs_ended_before(self, cutoff: str) -> int:
        """Delete all events of runs whose newest event ts is before ``cutoff``.

        Whole runs only: a run with any event at or after ``cutoff`` is left
        intact. ``cutoff`` is an ISO-8601 UTC string compared against the
        event ``ts``. Returns the number of deleted events. Used by
        ``agentmesh.maintenance.prune_events``.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM events WHERE run_id IN (%s)" % _RUNS_ENDED_BEFORE,
                (cutoff,),
            )
        return cursor.rowcount

    def vacuum(self) -> None:
        """Rebuild the database file to reclaim space freed by deletions."""
        with self._connect() as conn:
            conn.execute("VACUUM")

    def db_size_bytes(self) -> Optional[int]:
        """Size of the database file in bytes, or None if it is not a regular file."""
        try:
            if not self.path.is_file():
                return None
            return self.path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _row_to_event(row: Tuple) -> Event:
        return Event(
            event_id=row[0],
            run_id=row[1],
            agent=row[2],
            type=row[3],
            ts=row[4],
            payload=json.loads(row[5]),
        )
