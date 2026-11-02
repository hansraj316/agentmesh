"""Serve daemon: live board + JSON API over stdlib ``http.server``.

The server binds to 127.0.0.1 only — by design it is a local dashboard. The
event store may contain private repo names and error details, so exposing it
remotely is opt-in via your own reverse proxy.

GET routes (read-only):

* ``/`` — the HTML board from ``board.render_html`` with a 5-second
  auto-refresh meta tag injected.
* ``/api/agents`` — JSON list of agent summaries.
* ``/api/runs/<run_id>`` — JSON span tree from ``trace.build_trace``; runs
  with annotations carry an extra ``annotations`` key (chronological).
* ``/api/annotations`` — JSON list of recent annotations across runs,
  newest first; ``?since=ISO`` filters (400 on an unparsable value).
* ``/api/alerts`` — JSON webhook payloads for fired alert rules.
* ``/api/events?after=<id>&limit=<n>`` — JSON array of stored events with
  row id greater than ``after`` (default 0), oldest first; each object is
  the AMP event plus ``"id"`` (the store's row id). ``limit`` defaults to
  200 and is capped at 1000.
* ``/api/stream?after=<id>`` — the same objects pushed live as
  Server-Sent Events (``text/event-stream``): one ``id:`` and one ``data:``
  line per event, and a ``: ping`` comment every 15 seconds. The
  ``Last-Event-ID`` header (sent by ``EventSource`` on reconnect) wins over
  ``after``. The store is polled every 250 ms; the stream closes cleanly
  when the client disconnects or the server shuts down.
* ``/api/runs`` — JSON list of recent runs, newest activity first, each
  with ``run_id``, ``started_at``, ``ended_at`` (null while running),
  ``status`` (``running``, ``failed`` or ``ok``), ``span_count``,
  ``failed_count`` and ``event_count``. ``?limit=`` defaults to 50, max 500.
* ``/app/`` — the committed dashboard bundle from ``agentmesh/static/app/``.
  Unknown paths without a file extension fall back to ``index.html`` so
  client-side routes work; a missing bundle answers a plain-text 404 that
  says how to build it.

With ``AGENTMESH_DEV=1`` every response also carries
``Access-Control-Allow-Origin: http://localhost:5173`` so the Vite dev
server can call the API directly. Without it no CORS header is sent.

POST routes (webhook ingestion):

* ``/api/events`` — accepts a single AMP event JSON object or a JSON array
  of up to 1000 events (``Content-Type: application/json``, body at most
  5 MB). Every event is validated via ``Event`` *before* anything is
  written, so a request is all-or-nothing: one bad event rejects the whole
  batch with a 400 naming the offending index. Valid events are appended
  with ``EventStore.append_ignore_duplicates`` and the response is
  ``202 {"accepted": n, "skipped": m}`` where ``skipped`` counts duplicate
  event_ids.

The localhost-only default remains for ingestion: POST ``/api/events`` is a
trusted-local-network feature with no authentication in v1. Anyone who can
reach the socket can write events, so only expose it beyond 127.0.0.1 behind
a reverse proxy that adds its own auth.
"""

import json
import mimetypes
import os
import select
import socket
import sqlite3
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union
from urllib.parse import parse_qs, unquote, urlsplit

from agentmesh.alerts import evaluate, load_rules, to_webhook_payloads
from agentmesh.annotations import annotations_for_run, annotations_summary
from agentmesh.board import agent_summaries, render_html
from agentmesh.events import Event
from agentmesh.store import EventStore
from agentmesh.trace import STATUS_FAILED, STATUS_OK, STATUS_RUNNING, build_trace

HOST = "127.0.0.1"
DEFAULT_PORT = 7777

MAX_EVENTS_PER_REQUEST = 1000
MAX_BODY_BYTES = 5 * 1024 * 1024

DEFAULT_EVENTS_LIMIT = 200
MAX_EVENTS_LIMIT = 1000
DEFAULT_RUNS_LIMIT = 50
MAX_RUNS_LIMIT = 500

# Live stream timing; module-level so tests can shrink them.
STREAM_POLL_SECONDS = 0.25
STREAM_PING_SECONDS = 15.0
STREAM_BATCH = 500  # events per store read while a stream catches up

STATIC_APP_DIR = Path(__file__).resolve().parent / "static" / "app"
DEV_ORIGIN = "http://localhost:5173"

_AUTO_REFRESH_META = '<meta http-equiv="refresh" content="5">'
_RUNS_PREFIX = "/api/runs/"
_EVENTS_ROUTE = "/api/events"
_STREAM_ROUTE = "/api/stream"
_RUNS_ROUTE = "/api/runs"
_APP_ROUTE = "/app"
_MISSING_BUNDLE = (
    "AgentMesh dashboard bundle not found at %s.\n"
    "Build it with: cd dashboard && npm ci && npm run build\n"
    "The JSON API under /api/ and the board at / still work.\n"
)
_EXTRA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".map": "application/json",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}

PathLike = Optional[Union[str, Path]]


def _parse_event(data: Any) -> Event:
    """Build a validated Event from decoded JSON; ValueError on any problem."""
    if not isinstance(data, dict):
        raise ValueError("event must be a JSON object, got %r" % (data,))
    try:
        return Event.from_dict(data)
    except KeyError as exc:
        raise ValueError("missing required field %s" % (exc,))


def _with_auto_refresh(page: str) -> str:
    """Inject the auto-refresh meta tag into a rendered board page."""
    return page.replace("<head>", "<head>\n" + _AUTO_REFRESH_META, 1)


def _event_json(row_id: int, event: Event) -> Dict[str, Any]:
    """The wire shape of a stored event: the AMP event plus its row ``id``."""
    data = event.to_dict()
    data["id"] = row_id
    return data


def events_page(store: EventStore, after: int, limit: int) -> List[Tuple[int, Event]]:
    """Up to ``limit`` (row id, event) pairs with id > ``after``, oldest first.

    ``EventStore.events_after`` has no limit, so a reader starting from 0 on
    a large store would load everything; this reads one bounded page.
    """
    with sqlite3.connect(str(store.path)) as conn:
        rows = conn.execute(
            "SELECT id, event_id, run_id, agent, type, ts, payload FROM events "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after, limit),
        ).fetchall()
    return [
        (
            row[0],
            Event(
                event_id=row[1],
                run_id=row[2],
                agent=row[3],
                type=row[4],
                ts=row[5],
                payload=json.loads(row[6]),
            ),
        )
        for row in rows
    ]


def recent_runs(store: EventStore, limit: int = DEFAULT_RUNS_LIMIT) -> List[Dict[str, Any]]:
    """Summaries of the ``limit`` runs with the newest activity, newest first.

    ``status`` is ``running`` while any span is open, else ``failed`` if any
    span failed, else ``ok`` (a run with no spans, only messages, is ``ok``).
    ``ended_at`` is the run's last event ts, or None while it is running.
    """
    with sqlite3.connect(str(store.path)) as conn:
        rows = conn.execute(
            "SELECT run_id, MIN(ts), MAX(ts), COUNT(*) FROM events "
            "GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    runs = []
    for run_id, started_at, last_ts, event_count in rows:
        trace = build_trace(store, run_id)
        if _has_running_span(trace.roots):
            status = STATUS_RUNNING
        elif trace.failed_count:
            status = STATUS_FAILED
        else:
            status = STATUS_OK
        runs.append(
            {
                "run_id": run_id,
                "started_at": started_at,
                "ended_at": None if status == STATUS_RUNNING else last_ts,
                "status": status,
                "span_count": trace.span_count,
                "failed_count": trace.failed_count,
                "event_count": int(event_count),
            }
        )
    return runs


def _has_running_span(spans: List[Any]) -> bool:
    return any(span.status == STATUS_RUNNING or _has_running_span(span.children) for span in spans)


def _int_param(raw: Optional[str], name: str, default: int, minimum: int) -> int:
    """Parse an integer query/header value; ValueError with a readable message."""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("%s must be an integer, got %r" % (name, raw))
    if value < minimum:
        raise ValueError("%s must be at least %d, got %d" % (name, minimum, value))
    return value


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _EXTRA_TYPES:
        return _EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _dev_mode() -> bool:
    return os.environ.get("AGENTMESH_DEV") == "1"


class AgentMeshServer(ThreadingHTTPServer):
    """Threading server whose live streams end when it shuts down."""

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.stopping = threading.Event()
        super().__init__(*args, **kwargs)

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


def create_handler(
    db_path: PathLike,
    rules_path: PathLike = None,
    static_dir: PathLike = None,
) -> Type[BaseHTTPRequestHandler]:
    """Build a request handler class serving the board and the JSON API.

    Every request opens a fresh ``EventStore`` (which itself opens a
    short-lived SQLite connection per operation), so the handler is safe
    under ``ThreadingHTTPServer``. ``static_dir`` overrides where ``/app/``
    is served from (default ``agentmesh/static/app/``).
    """
    app_root = Path(static_dir) if static_dir is not None else STATIC_APP_DIR

    class AgentMeshHandler(BaseHTTPRequestHandler):
        server_version = "agentmesh"

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if path != "/":
                path = path.rstrip("/")
            try:
                self._route(path)
            except Exception as exc:  # keep the daemon alive on bad input
                self._send_json({"error": str(exc)}, status=500)

        def do_POST(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if path != "/":
                path = path.rstrip("/")
            try:
                self._route_post(path)
            except Exception as exc:  # keep the daemon alive on bad input
                self._send_json({"error": str(exc)}, status=500)

        def _route(self, path: str) -> None:
            if path == _APP_ROUTE or path.startswith(_APP_ROUTE + "/"):
                self._serve_app(path[len(_APP_ROUTE) :])
                return
            store = EventStore(db_path)
            if path == _STREAM_ROUTE:
                self._stream(store)
            elif path == _EVENTS_ROUTE:
                params = parse_qs(urlsplit(self.path).query)
                try:
                    after = _int_param(params.get("after", [None])[0], "after", 0, 0)
                    limit = _int_param(
                        params.get("limit", [None])[0], "limit", DEFAULT_EVENTS_LIMIT, 1
                    )
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                page = events_page(store, after, min(limit, MAX_EVENTS_LIMIT))
                self._send_json([_event_json(row_id, event) for row_id, event in page])
            elif path == _RUNS_ROUTE:
                params = parse_qs(urlsplit(self.path).query)
                try:
                    limit = _int_param(
                        params.get("limit", [None])[0], "limit", DEFAULT_RUNS_LIMIT, 1
                    )
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json(recent_runs(store, min(limit, MAX_RUNS_LIMIT)))
            elif path == "/":
                page = _with_auto_refresh(render_html(agent_summaries(store)))
                self._send(page.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/agents":
                self._send_json([asdict(summary) for summary in agent_summaries(store)])
            elif path.startswith(_RUNS_PREFIX) and len(path) > len(_RUNS_PREFIX):
                run_id = path[len(_RUNS_PREFIX) :]
                try:
                    trace = build_trace(store, run_id)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=404)
                    return
                data = asdict(trace)
                notes = annotations_for_run(store, run_id)
                if notes:
                    data["annotations"] = notes
                self._send_json(data)
            elif path == "/api/annotations":
                params = parse_qs(urlsplit(self.path).query)
                since = params.get("since", [None])[0]
                try:
                    summary = annotations_summary(store, since=since)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                self._send_json(summary)
            elif path == "/api/alerts":
                alerts = evaluate(store, load_rules(rules_path))
                self._send_json(to_webhook_payloads(alerts))
            else:
                self._send_json({"error": "no such route: %s" % path}, status=404)

        def _stream(self, store: EventStore) -> None:
            """Push events as Server-Sent Events until the client or server goes away."""
            params = parse_qs(urlsplit(self.path).query)
            header = self.headers.get("Last-Event-ID")
            try:
                if header is not None and header.strip():
                    last_id = _int_param(header.strip(), "Last-Event-ID", 0, 0)
                else:
                    last_id = _int_param(params.get("after", [None])[0], "after", 0, 0)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_cors()
            self.end_headers()
            self.close_connection = True
            stopping = getattr(self.server, "stopping", None)
            try:
                # Tell EventSource how fast to reconnect after a drop.
                self._write(b"retry: 1000\n\n")
                last_ping = time.monotonic()
                while stopping is None or not stopping.is_set():
                    page = events_page(store, last_id, STREAM_BATCH)
                    if page:
                        chunks = []
                        for row_id, event in page:
                            data = json.dumps(_event_json(row_id, event))
                            chunks.append("id: %d\ndata: %s\n\n" % (row_id, data))
                            last_id = row_id
                        self._write("".join(chunks).encode("utf-8"))
                        if len(page) == STREAM_BATCH:
                            continue  # still catching up; read the next page now
                    if time.monotonic() - last_ping >= STREAM_PING_SECONDS:
                        self._write(b": ping\n\n")
                        last_ping = time.monotonic()
                    if self._client_gone(STREAM_POLL_SECONDS):
                        return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return  # client went away mid-write

        def _write(self, data: bytes) -> None:
            self.wfile.write(data)
            self.wfile.flush()

        def _client_gone(self, timeout: float) -> bool:
            """Wait up to ``timeout`` seconds; True if the client closed its end."""
            try:
                readable, _, _ = select.select([self.connection], [], [], timeout)
                if not readable:
                    return False
                return self.connection.recv(4096) == b""
            except (OSError, ValueError, socket.error):
                return True

        def _serve_app(self, rest: str) -> None:
            """Serve the dashboard bundle with an index.html fallback for client routes."""
            index = app_root / "index.html"
            if not index.is_file():
                body = (_MISSING_BUNDLE % app_root).encode("utf-8")
                self._send(body, "text/plain; charset=utf-8", status=404)
                return
            if rest == "":
                # /app -> /app/ so relative asset URLs in index.html resolve.
                if not urlsplit(self.path).path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", _APP_ROUTE + "/")
                    self.send_header("Content-Length", "0")
                    self._send_cors()
                    self.end_headers()
                    return
                self._send_file(index, cache=False)
                return
            root = app_root.resolve()
            target = (root / rest.lstrip("/")).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self._send_json({"error": "no such route: /app%s" % rest}, status=404)
                return
            if target.is_file():
                self._send_file(target, cache=target.parent.name == "assets")
            elif Path(rest).suffix:
                self._send_json({"error": "no such file: /app%s" % rest}, status=404)
            else:
                self._send_file(index, cache=False)

        def _send_file(self, path: Path, cache: bool) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", _content_type(path))
            self.send_header("Content-Length", str(len(body)))
            # Vite puts content-hashed files in assets/; everything else revalidates.
            self.send_header(
                "Cache-Control", "public, max-age=31536000, immutable" if cache else "no-cache"
            )
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)

        def _send_cors(self) -> None:
            if _dev_mode():
                self.send_header("Access-Control-Allow-Origin", DEV_ORIGIN)

        def _route_post(self, path: str) -> None:
            if path != _EVENTS_ROUTE:
                self._send_json({"error": "no such route: %s" % path}, status=404)
                return
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type.lower() != "application/json":
                self._send_json({"error": "Content-Type must be application/json"}, status=415)
                return
            body = self._read_body()
            if body is None:
                return  # _read_body already sent the error response
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send_json({"error": "request body is not valid JSON"}, status=400)
                return
            self._ingest(data)

        def _read_body(self) -> Optional[bytes]:
            """Read the request body via Content-Length; None if rejected."""
            header = self.headers.get("Content-Length")
            try:
                length = int(header) if header is not None else None
            except ValueError:
                length = None
            if length is None or length < 0:
                self._send_json({"error": "Content-Length header is required"}, status=413)
                return None
            if length > MAX_BODY_BYTES:
                self._send_json(
                    {"error": "request body exceeds %d bytes" % MAX_BODY_BYTES}, status=413
                )
                return None
            return self.rfile.read(length)

        def _ingest(self, data: Any) -> None:
            """Validate then append decoded events; all-or-nothing per request."""
            if isinstance(data, dict):
                items = [data]
                is_array = False
            elif isinstance(data, list):
                items = data
                is_array = True
            else:
                self._send_json(
                    {"error": "body must be a JSON event object or array of events"}, status=400
                )
                return
            if len(items) > MAX_EVENTS_PER_REQUEST:
                self._send_json(
                    {
                        "error": "too many events: %d (limit %d per request)"
                        % (len(items), MAX_EVENTS_PER_REQUEST)
                    },
                    status=413,
                )
                return
            events = []
            for index, item in enumerate(items):
                try:
                    events.append(_parse_event(item))
                except ValueError as exc:
                    if is_array:
                        message = "invalid event at index %d: %s" % (index, exc)
                    else:
                        message = "invalid event: %s" % exc
                    self._send_json({"error": message}, status=400)
                    return
            store = EventStore(db_path)
            accepted = 0
            skipped = 0
            for event in events:
                if store.append_ignore_duplicates(event):
                    accepted += 1
                else:
                    skipped += 1
            self._send_json({"accepted": accepted, "skipped": skipped}, status=202)

        def _send_json(self, data: Any, status: int = 200) -> None:
            self._send(json.dumps(data).encode("utf-8"), "application/json", status=status)

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default per-request stderr logging."""

    return AgentMeshHandler


def create_server(
    db_path: PathLike,
    port: int = DEFAULT_PORT,
    rules_path: PathLike = None,
    static_dir: PathLike = None,
) -> AgentMeshServer:
    """Build a localhost-only threading server; ``port=0`` picks a free port.

    ``shutdown()`` or ``server_close()`` also ends any open ``/api/stream``
    connections within one poll interval.
    """
    handler = create_handler(db_path, rules_path=rules_path, static_dir=static_dir)
    return AgentMeshServer((HOST, port), handler)


def serve(db_path: PathLike, port: int, rules_path: PathLike = None) -> None:
    """Serve the board and API on ``127.0.0.1:port`` until interrupted.

    Localhost-only by design; see the module docstring. Blocks in
    ``serve_forever`` and always closes the listening socket on the way out.
    """
    server = create_server(db_path, port, rules_path=rules_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
