"""Serve daemon: live board + read-only JSON API over stdlib ``http.server``.

The server binds to 127.0.0.1 only — by design it is a local dashboard. The
event store may contain private repo names and error details, so exposing it
remotely is opt-in via your own reverse proxy.

Routes (all GET, read-only):

* ``/`` — the HTML board from ``board.render_html`` with a 5-second
  auto-refresh meta tag injected.
* ``/api/agents`` — JSON list of agent summaries.
* ``/api/runs/<run_id>`` — JSON span tree from ``trace.build_trace``.
* ``/api/alerts`` — JSON webhook payloads for fired alert rules.
"""

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Type, Union
from urllib.parse import unquote, urlsplit

from agentmesh.alerts import evaluate, load_rules, to_webhook_payloads
from agentmesh.board import agent_summaries, render_html
from agentmesh.store import EventStore
from agentmesh.trace import build_trace

HOST = "127.0.0.1"
DEFAULT_PORT = 7777

_AUTO_REFRESH_META = '<meta http-equiv="refresh" content="5">'
_RUNS_PREFIX = "/api/runs/"

PathLike = Optional[Union[str, Path]]


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
