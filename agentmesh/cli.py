"""AgentMesh command-line interface.

Usage::

    agentmesh tail [--run RUN] [--agent NAME] [-n N] [--follow]
"""

import argparse
import json
import sys
import time
from typing import List, Optional

from agentmesh.events import Event
from agentmesh.store import EventStore

_FOLLOW_POLL_SECONDS = 0.5


def format_event(event: Event) -> str:
    return "%s run=%s agent=%s type=%s payload=%s" % (
        event.ts,
        event.run_id,
        event.agent,
        event.type,
        json.dumps(event.payload, sort_keys=True),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentmesh",
        description="AgentMesh — observability for multi-agent AI systems.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tail = subparsers.add_parser("tail", help="Print recent events, one per line.")
    tail.add_argument("--run", default=None, help="Only events with this run_id.")
    tail.add_argument("--agent", default=None, help="Only events from this agent.")
    tail.add_argument(
        "-n",
        type=int,
        default=20,
        dest="limit",
        help="Number of recent events to print (default: 20).",
    )
    tail.add_argument(
        "--follow",
        action="store_true",
        help="Keep polling and print new events as they arrive (Ctrl-C to stop).",
    )
    return parser


def _tail(args: argparse.Namespace) -> int:
    store = EventStore()
    for event in store.query(run_id=args.run, agent=args.agent, limit=args.limit):
        print(format_event(event))
    if not args.follow:
        return 0
    last_id = store.max_id()
    try:
        while True:
            time.sleep(_FOLLOW_POLL_SECONDS)
            for row_id, event in store.events_after(last_id, run_id=args.run, agent=args.agent):
                print(format_event(event), flush=True)
                last_id = max(last_id, row_id)
    except KeyboardInterrupt:
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "tail":
        return _tail(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
