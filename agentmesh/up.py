"""``agentmesh up``: the whole loop in one command.

Starts the daemon (board, JSON API, live stream and the ``/app/`` dashboard)
and the example Planner, Coder and Reviewer fleet against the same event
store, then opens the dashboard in a browser. Ctrl-C stops both.

``start(...)`` returns an :class:`UpSession` so tests and other callers can
run the same loop on a free port and stop it cleanly.
"""

import argparse
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TextIO

from agentmesh.fleet import LLMError, make_llm, run_forever
from agentmesh.fleet.cli import format_summary
from agentmesh.server import STATIC_APP_DIR, AgentMeshServer, create_server
from agentmesh.store import EventStore

_JOIN_SECONDS = 10.0


@dataclass
class UpSession:
    """A running daemon plus fleet. Call :meth:`stop` to shut both down."""

    store: EventStore
    server: AgentMeshServer
    stop_event: threading.Event
    server_thread: threading.Thread
    fleet_thread: threading.Thread
    _stopped: bool = field(default=False, repr=False)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def app_url(self) -> str:
        return self.base_url + "/app/"

    def stop(self) -> None:
        """Stop the fleet first (so no half-written run), then the daemon."""
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        self.fleet_thread.join(_JOIN_SECONDS)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(_JOIN_SECONDS)


def start(
    store: EventStore,
    port: int,
    llm: Any,
    interval_s: float = 3.0,
    rules_path: Any = None,
    on_run: Optional[Callable[[Any], None]] = None,
) -> UpSession:
    """Start the daemon and the fleet in background threads.

    ``port=0`` picks a free port. Raises ``OSError`` if the port is taken;
    nothing is left running in that case.
    """
    server = create_server(store.path, port, rules_path=rules_path)
    stop_event = threading.Event()
    server_thread = threading.Thread(
        target=server.serve_forever, name="agentmesh-daemon", daemon=True
    )
    fleet_thread = threading.Thread(
        target=run_forever,
        args=(store, stop_event),
        kwargs={"interval_s": interval_s, "llm": llm, "on_run": on_run},
        name="agentmesh-fleet",
        daemon=True,
    )
    server_thread.start()
    fleet_thread.start()
    return UpSession(store, server, stop_event, server_thread, fleet_thread)


def _wait(session: UpSession) -> None:
    """Block until the fleet stops on its own or Ctrl-C raises."""
    while session.fleet_thread.is_alive():
        session.fleet_thread.join(0.5)


def add_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``agentmesh up``; the dispatcher calls ``args.func(args)``."""
    parser = subparsers.add_parser(
        "up",
        help="Run the daemon, the example fleet and the live dashboard together.",
        description=(
            "Start the AgentMesh daemon and the example Planner, Coder and Reviewer fleet "
            "against one event store, and open the live dashboard at /app/. The default "
            "scripted model works offline; --llm claude calls Claude using "
            "ANTHROPIC_API_KEY. Ctrl-C stops everything."
        ),
    )
    parser.add_argument("--port", type=int, default=None, help="Port at 127.0.0.1 (default: 7777).")
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    parser.add_argument(
        "--llm",
        choices=("scripted", "claude"),
        default="scripted",
        help="Model behind the fleet's agents (default: scripted, no network).",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Seed for the scripted model (repeatable runs)."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Pause between fleet runs (default: 3).",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open the dashboard in a browser."
    )
    parser.set_defaults(func=run_cli)
    return parser


def run_cli(args: argparse.Namespace, out: Optional[TextIO] = None) -> int:
    out = out if out is not None else sys.stdout
    try:
        llm = make_llm(args.llm, seed=args.seed)
    except LLMError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    store = EventStore(args.db) if args.db else EventStore()
    try:
        session = start(
            store,
            args.port,
            llm,
            interval_s=args.interval,
            on_run=lambda s: print("  run " + format_summary(s), file=out, flush=True),
        )
    except OSError as exc:
        print(
            "error: could not listen on 127.0.0.1:%d (%s). Try --port." % (args.port, exc),
            file=sys.stderr,
        )
        return 1
    print("AgentMesh is up (Ctrl-C to stop)", file=out)
    print("  dashboard  %s" % session.app_url, file=out)
    print("  board      %s/" % session.base_url, file=out)
    print("  events     %s" % store.path, file=out)
    print("  fleet      planner -> coder -> reviewer (%s model)" % args.llm, file=out, flush=True)
    if not (STATIC_APP_DIR / "index.html").is_file():
        print("warning: dashboard bundle missing; /app/ will 404", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(session.app_url)
    try:
        _wait(session)
    except KeyboardInterrupt:
        print("\nstopping...", file=out, flush=True)
    finally:
        session.stop()
    return 0
