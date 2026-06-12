"""Serve daemon: live board + JSON API over stdlib ``http.server``.

The server binds to 127.0.0.1 only — by design it is a local dashboard. The
event store may contain private repo names and error details, so exposing it
remotely is opt-in via your own reverse proxy.

GET routes (read-only):

* ``/`` — the HTML board from ``board.render_html`` with a 5-second
  auto-refresh meta tag injected.
* ``/api/agents`` — JSON list of agent summaries.
* ``/api/runs/<run_id>`` — JSON span tree from ``trace.build_trace``.
* ``/api/alerts`` — JSON webhook payloads for fired alert rules.

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
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Type, Union
from urllib.parse import unquote, urlsplit

from agentmesh.alerts import evaluate, load_rules, to_webhook_payloads
from agentmesh.board import agent_summaries, render_html
from agentmesh.events import Event
from agentmesh.store import EventStore
from agentmesh.trace import build_trace

HOST = "127.0.0.1"
DEFAULT_PORT = 7777

MAX_EVENTS_PER_REQUEST = 1000
MAX_BODY_BYTES = 5 * 1024 * 1024

_AUTO_REFRESH_META = '<meta http-equiv="refresh" content="5">'
_RUNS_PREFIX = "/api/runs/"
_EVENTS_ROUTE = "/api/events"

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


def create_handler(db_path: PathLike, rules_path: PathLike = None) -> Type[BaseHTTPRequestHandler]:
    """Build a request handler class serving the board and the JSON API.

    Every request opens a fresh ``EventStore`` (which itself opens a
    short-lived SQLite connection per operation), so the handler is safe
    under ``ThreadingHTTPServer``.
    """

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
            store = EventStore(db_path)
            if path == "/":
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
                self._send_json(asdict(trace))
            elif path == "/api/alerts":
                alerts = evaluate(store, load_rules(rules_path))
                self._send_json(to_webhook_payloads(alerts))
            else:
                self._send_json({"error": "no such route: %s" % path}, status=404)

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
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default per-request stderr logging."""

    return AgentMeshHandler


def create_server(
    db_path: PathLike,
    port: int = DEFAULT_PORT,
    rules_path: PathLike = None,
) -> ThreadingHTTPServer:
    """Build a localhost-only threading server; ``port=0`` picks a free port."""
    return ThreadingHTTPServer((HOST, port), create_handler(db_path, rules_path=rules_path))


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
