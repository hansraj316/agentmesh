"""SQLite-backed AMP event store."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

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
