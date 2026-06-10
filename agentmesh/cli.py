"""AgentMesh command-line interface.

Usage::

    agentmesh tail [--run RUN] [--agent NAME] [-n N] [--follow]
    agentmesh ingest-gha OWNER --repo R1 [--repo R2 ...] [--limit N]
    agentmesh board [--since ISO] [--html PATH] [--out PATH]
    agentmesh trace RUN_ID [--db PATH]
    agentmesh alerts [--rules PATH] [--db PATH] [--webhook URL] [--exit-code]
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from agentmesh.alerts import (
    evaluate,
    load_rules,
    post_webhook,
    render_alerts_markdown,
    to_webhook_payloads,
)
from agentmesh.board import agent_summaries, render_html, render_markdown
from agentmesh.events import Event
from agentmesh.ingest_gha import ingest
from agentmesh.store import EventStore
from agentmesh.trace import build_trace, render_tree

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

    ingest_gha = subparsers.add_parser(
        "ingest-gha",
        help="Ingest recent GitHub Actions workflow runs as AMP events.",
    )
    ingest_gha.add_argument("owner", help="GitHub user or organization.")
    ingest_gha.add_argument(
        "--repo",
        action="append",
        dest="repos",
        required=True,
        metavar="REPO",
        help="Repository to ingest (repeatable).",
    )
    ingest_gha.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Workflow runs to fetch per repo (default: 30).",
    )

    board = subparsers.add_parser(
        "board",
        help="Render a fleet status dashboard from stored events.",
    )
    board.add_argument(
        "--since",
        default=None,
        help="Only events at or after this ISO-8601 UTC timestamp.",
    )
    board.add_argument(
        "--html",
        default=None,
        dest="html_path",
        metavar="PATH",
        help="Also write the dashboard as a self-contained HTML file.",
    )
    board.add_argument(
        "--out",
        default=None,
        dest="out_path",
        metavar="PATH",
        help="Write the markdown dashboard to a file instead of stdout.",
    )

    trace = subparsers.add_parser(
        "trace",
        help="Print the span tree of one run.",
    )
    trace.add_argument("run_id", help="The run_id to trace.")
    trace.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    alerts = subparsers.add_parser(
        "alerts",
        help="Evaluate alert rules against stored events.",
    )
    alerts.add_argument(
        "--rules",
        default=None,
        metavar="PATH",
        help="Rules file (default: $AGENTMESH_ALERTS or ~/.agentmesh/alerts.json).",
    )
    alerts.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    alerts.add_argument(
        "--webhook",
        default=None,
        metavar="URL",
        help="POST each fired alert to this URL as JSON.",
    )
    alerts.add_argument(
        "--exit-code",
        action="store_true",
        dest="exit_code",
        help="Exit 1 if any alert fired (for CI/cron use).",
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


def _ingest_gha(args: argparse.Namespace) -> int:
    store = EventStore()
    counts = ingest(args.owner, args.repos, store, limit_per_repo=args.limit)
    for repo in args.repos:
        print("%s/%s: %d new events" % (args.owner, repo, counts[repo]))
    return 0


def _board(args: argparse.Namespace) -> int:
    store = EventStore()
    summaries = agent_summaries(store, since=args.since)
    markdown = render_markdown(summaries)
    if args.out_path:
        Path(args.out_path).write_text(markdown + "\n", encoding="utf-8")
        print("wrote %s" % args.out_path)
    else:
        print(markdown)
    if args.html_path:
        Path(args.html_path).write_text(render_html(summaries), encoding="utf-8")
        print("wrote %s" % args.html_path)
    return 0


def _trace(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        trace = build_trace(store, args.run_id)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(render_tree(trace))
    return 0


def _alerts(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        rules = load_rules(args.rules)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    alerts = evaluate(store, rules)
    if not alerts:
        print("No alerts.")
        return 0
    print(render_alerts_markdown(alerts))
    if args.webhook:
        for payload in to_webhook_payloads(alerts):
            post_webhook(args.webhook, payload)
    return 1 if args.exit_code else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "tail":
        return _tail(args)
    if args.command == "ingest-gha":
        return _ingest_gha(args)
    if args.command == "board":
        return _board(args)
    if args.command == "trace":
        return _trace(args)
    if args.command == "alerts":
        return _alerts(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
