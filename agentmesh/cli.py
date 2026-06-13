"""AgentMesh command-line interface.

Usage::

    agentmesh tail [--run RUN] [--agent NAME] [-n N] [--follow] [--db PATH]
    agentmesh ingest-gha OWNER --repo R1 [--repo R2 ...] [--limit N]
    agentmesh board [--since ISO] [--p95] [--html PATH] [--out PATH] [--db PATH]
    agentmesh costs [--by run|agent|model] [--since ISO] [--db PATH]
    agentmesh flaky [--window N] [--since ISO] [--db PATH]
    agentmesh latency [--by agent|model] [--since ISO] [--db PATH]
    agentmesh trace RUN_ID [--db PATH]
    agentmesh annotate RUN_ID TEXT [--author NAME] [--db PATH]
    agentmesh export-otlp RUN_ID [--db PATH] [--out PATH]
    agentmesh diff RUN_A RUN_B [--db PATH] [--threshold PCT]
    agentmesh alerts [--rules PATH] [--db PATH] [--webhook URL] [--exit-code]
    agentmesh import-jsonl PATH [--db PATH]
    agentmesh serve [--port N] [--db PATH] [--rules PATH]
    agentmesh stats [--db PATH]
    agentmesh prune --older-than 30d [--db PATH] [--dry-run]
    agentmesh demo [--db PATH] [--fresh]
    agentmesh config [--config PATH]

A global ``--config PATH`` flag (before the subcommand) points at a JSON
config file that supplies defaults for repeated flags; see agentmesh.config.
"""

import argparse
import json
import os
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
from agentmesh.annotations import add_annotation, annotations_for_run
from agentmesh.board import agent_summaries, render_html, render_markdown
from agentmesh.config import (
    DEFAULT_PORT,
    DEFAULT_THRESHOLD,
    Config,
    effective_value,
    load_config,
    render_config,
    resolve_settings,
)
from agentmesh.costs import GROUP_BY_CHOICES, render_costs, usage_summary
from agentmesh.demo import DEMO_DB_PATH, DEMO_RULES_FILENAME, demo_tour, seed_demo, write_demo_rules
from agentmesh.diff import diff_traces, render_diff
from agentmesh.events import Event
from agentmesh.flaky import agent_outcomes, flakiness_metrics, render_flaky
from agentmesh.ingest_gha import ingest
from agentmesh.jsonl import import_jsonl
from agentmesh.latency import (
    GROUP_BY_CHOICES as LATENCY_GROUP_BY_CHOICES,
    latency_stats,
    render_latency,
    span_durations,
)
from agentmesh.maintenance import prune_events, render_stats, store_stats
from agentmesh.otlp import export_otlp
from agentmesh.server import serve
from agentmesh.store import EventStore
from agentmesh.trace import build_trace, render_tree

_FOLLOW_POLL_SECONDS = 0.5


def _parse_older_than(value: str) -> int:
    """Parse an age cutoff in days: "30d" or a bare integer like "30"."""
    text = value.strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = int(text)
    except ValueError:
        raise ValueError('expected a day count like "30d" or "30", got %r' % value)
    if days < 0:
        raise ValueError("day count must not be negative, got %r" % value)
    return days


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
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Config file with flag defaults "
        "(default: $AGENTMESH_CONFIG or ~/.agentmesh/config.json).",
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
    tail.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
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
        "--p95",
        action="store_true",
        dest="p95",
        help="Add a per-agent p95 duration column.",
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
    board.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    costs_cmd = subparsers.add_parser(
        "costs",
        help="Aggregate token & cost usage from stored events.",
    )
    costs_cmd.add_argument(
        "--by",
        default="run",
        choices=GROUP_BY_CHOICES,
        dest="group_by",
        help="Group usage by run, agent, or model (default: run).",
    )
    costs_cmd.add_argument(
        "--since",
        default=None,
        help="Only events at or after this ISO-8601 UTC timestamp.",
    )
    costs_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    flaky_cmd = subparsers.add_parser(
        "flaky",
        help="Rank agents by flakiness: intermittency, failure rate, MTBF.",
    )
    flaky_cmd.add_argument(
        "--window",
        type=int,
        default=None,
        metavar="N",
        help="Only the N most recent completed runs per agent.",
    )
    flaky_cmd.add_argument(
        "--since",
        default=None,
        help="Only events at or after this ISO-8601 UTC timestamp.",
    )
    flaky_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    latency_cmd = subparsers.add_parser(
        "latency",
        help="Latency percentiles (p50/p90/p99) of completed spans per agent or model.",
    )
    latency_cmd.add_argument(
        "--by",
        default="agent",
        choices=LATENCY_GROUP_BY_CHOICES,
        dest="group_by",
        help="Group span durations by agent or model (default: agent).",
    )
    latency_cmd.add_argument(
        "--since",
        default=None,
        help="Only spans started at or after this ISO-8601 UTC timestamp.",
    )
    latency_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
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

    annotate = subparsers.add_parser(
        "annotate",
        help='Attach an operator note to a run ("deploy happened here").',
    )
    annotate.add_argument("run_id", help="The run_id to annotate.")
    annotate.add_argument("text", help="The annotation text.")
    annotate.add_argument(
        "--author",
        default=None,
        metavar="NAME",
        help="Who wrote the note (stored in the annotation payload).",
    )
    annotate.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    export_otlp_cmd = subparsers.add_parser(
        "export-otlp",
        help="Export one run's span tree as OTLP/JSON (for Jaeger, Tempo, ...).",
    )
    export_otlp_cmd.add_argument("run_id", help="The run_id to export.")
    export_otlp_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    export_otlp_cmd.add_argument(
        "--out",
        default=None,
        dest="out_path",
        metavar="PATH",
        help="Write the OTLP JSON document to a file instead of stdout.",
    )

    diff_cmd = subparsers.add_parser(
        "diff",
        help="Compare two runs of the same workflow: durations, statuses, structure.",
    )
    diff_cmd.add_argument("run_a", help="The baseline run_id (A).")
    diff_cmd.add_argument("run_b", help="The run_id to compare against the baseline (B).")
    diff_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    diff_cmd.add_argument(
        "--threshold",
        type=float,
        default=None,
        metavar="PCT",
        help="Duration change (in percent) that counts as significant (default: 20).",
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

    import_jsonl_cmd = subparsers.add_parser(
        "import-jsonl",
        help="Import AMP events from a JSONL file (e.g. emitted by the TypeScript SDK).",
    )
    import_jsonl_cmd.add_argument("path", help="JSONL file with one AMP event per line.")
    import_jsonl_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    serve_cmd = subparsers.add_parser(
        "serve",
        help="Serve the live board and a read-only JSON API on localhost.",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on at 127.0.0.1 (default: 7777).",
    )
    serve_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    serve_cmd.add_argument(
        "--rules",
        default=None,
        metavar="PATH",
        help="Alert rules file for /api/alerts "
        "(default: $AGENTMESH_ALERTS or ~/.agentmesh/alerts.json).",
    )

    stats_cmd = subparsers.add_parser(
        "stats",
        help="Print event store statistics (counts, agents, runs, ts range, db size).",
    )
    stats_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )

    prune_cmd = subparsers.add_parser(
        "prune",
        help="Delete events older than N days (whole runs only), then VACUUM.",
    )
    prune_cmd.add_argument(
        "--older-than",
        required=True,
        dest="older_than",
        type=_parse_older_than,
        metavar="DAYS",
        help='Age cutoff in days, e.g. "30d" or "30".',
    )
    prune_cmd.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Event database path (default: $AGENTMESH_DB or ~/.agentmesh/events.db).",
    )
    prune_cmd.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Only report what would be deleted; change nothing.",
    )

    demo = subparsers.add_parser(
        "demo",
        help="Seed a self-contained demo database and print a guided tour.",
    )
    demo.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Demo database path (default: ~/.agentmesh/demo.db).",
    )
    demo.add_argument(
        "--fresh",
        action="store_true",
        help="Delete an existing demo database and reseed from scratch.",
    )

    config_cmd = subparsers.add_parser(
        "config",
        help="Show the effective configuration and where each value comes from.",
    )
    config_cmd.add_argument(
        "--config",
        # SUPPRESS so an unused subcommand default never clobbers a value
        # given via the global --config flag before the subcommand.
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Config file with flag defaults "
        "(default: $AGENTMESH_CONFIG or ~/.agentmesh/config.json).",
    )
    return parser


def _apply_config(args: argparse.Namespace, config: Config) -> None:
    """Fill flag values the user did not pass from env/config-file/defaults.

    Precedence stays: explicit flag > env var > config file > built-in
    default. With no config file this reproduces today's behavior exactly.
    The demo command keeps its own dedicated database and is left alone.
    """
    if hasattr(args, "db") and args.command != "demo":
        args.db = effective_value("db", args.db, config, env_value=os.environ.get("AGENTMESH_DB"))
    if hasattr(args, "rules"):
        args.rules = effective_value(
            "rules", args.rules, config, env_value=os.environ.get("AGENTMESH_ALERTS")
        )
    if hasattr(args, "port"):
        args.port = effective_value("port", args.port, config, default=DEFAULT_PORT)
    if hasattr(args, "threshold"):
        args.threshold = effective_value(
            "threshold", args.threshold, config, default=DEFAULT_THRESHOLD
        )
    if hasattr(args, "window"):
        args.window = effective_value("window", args.window, config)
    if hasattr(args, "since"):
        args.since = effective_value("since", args.since, config)


def _tail(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
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
    store = EventStore(args.db) if args.db else EventStore()
    summaries = agent_summaries(store, since=args.since)
    markdown = render_markdown(summaries, include_p95=args.p95)
    if args.out_path:
        Path(args.out_path).write_text(markdown + "\n", encoding="utf-8")
        print("wrote %s" % args.out_path)
    else:
        print(markdown)
    if args.html_path:
        html_page = render_html(summaries, include_p95=args.p95)
        Path(args.html_path).write_text(html_page, encoding="utf-8")
        print("wrote %s" % args.html_path)
    return 0


def _costs(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    print(render_costs(usage_summary(store, group_by=args.group_by, since=args.since)))
    return 0


def _flaky(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        outcomes = agent_outcomes(store, window=args.window, since=args.since)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(render_flaky(flakiness_metrics(outcomes)))
    return 0


def _latency(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    samples = span_durations(store, since=args.since)
    durations = samples.by_agent if args.group_by == "agent" else samples.by_model
    print(render_latency(latency_stats(durations), by=args.group_by, since=args.since))
    return 0


def _trace(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        trace = build_trace(store, args.run_id)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(render_tree(trace, annotations=annotations_for_run(store, args.run_id)))
    return 0


def _annotate(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        event = add_annotation(store, args.run_id, args.text, author=args.author)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    suffix = " (%s)" % args.author if args.author else ""
    print('annotated run %s: "%s"%s' % (args.run_id, event.payload["text"], suffix))
    return 0


def _export_otlp(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        document = export_otlp(store, args.run_id)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    text = json.dumps(document, indent=2)
    if args.out_path:
        Path(args.out_path).write_text(text + "\n", encoding="utf-8")
        print("Wrote %s" % args.out_path)
    else:
        print(text)
    return 0


def _diff(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    traces = []
    for run_id in (args.run_a, args.run_b):
        try:
            traces.append(build_trace(store, run_id))
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
    print(render_diff(diff_traces(traces[0], traces[1]), threshold_pct=args.threshold))
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


def _import_jsonl(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    try:
        imported, skipped = import_jsonl(args.path, store)
    except FileNotFoundError:
        print("error: no such file: %s" % args.path, file=sys.stderr)
        return 1
    except ValueError as exc:
        print("error: %s: %s" % (args.path, exc), file=sys.stderr)
        return 1
    print("imported %d event(s), skipped %d duplicate(s)" % (imported, skipped))
    return 0


def _serve(args: argparse.Namespace) -> int:
    print("agentmesh board at http://127.0.0.1:%d/" % args.port, flush=True)
    try:
        serve(args.db, args.port, rules_path=args.rules)
    except KeyboardInterrupt:
        pass
    return 0


def _stats(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    print(render_stats(store_stats(store)))
    return 0


def _prune(args: argparse.Namespace) -> int:
    store = EventStore(args.db) if args.db else EventStore()
    result = prune_events(store, args.older_than, dry_run=args.dry_run)
    if args.dry_run:
        print(
            "dry run: would delete %d event(s), %d would remain"
            % (result["deleted"], result["remaining"])
        )
    else:
        print("deleted %d event(s), %d remaining" % (result["deleted"], result["remaining"]))
    return 0


def _demo(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else DEMO_DB_PATH
    if db_path.exists():
        if args.fresh:
            db_path.unlink()
        elif EventStore(db_path).count_events() > 0:
            print(
                "error: %s already contains events; pass --fresh to delete and reseed" % db_path,
                file=sys.stderr,
            )
            return 1
    summary = seed_demo(EventStore(db_path))
    rules_path = write_demo_rules(db_path.parent / DEMO_RULES_FILENAME)
    print(
        "Seeded %d events across %d runs and %d agents into %s"
        % (summary["events"], summary["runs"], summary["agents"], db_path)
    )
    print("Demo alert rules written to %s" % rules_path)
    print()
    print(demo_tour(db_path))
    return 0


def _config(config: Config) -> int:
    print(render_config(config, resolve_settings(config)))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if args.command == "config":
        return _config(config)
    _apply_config(args, config)
    if args.command == "tail":
        return _tail(args)
    if args.command == "ingest-gha":
        return _ingest_gha(args)
    if args.command == "board":
        return _board(args)
    if args.command == "costs":
        return _costs(args)
    if args.command == "flaky":
        return _flaky(args)
    if args.command == "latency":
        return _latency(args)
    if args.command == "trace":
        return _trace(args)
    if args.command == "annotate":
        return _annotate(args)
    if args.command == "export-otlp":
        return _export_otlp(args)
    if args.command == "diff":
        return _diff(args)
    if args.command == "alerts":
        return _alerts(args)
    if args.command == "import-jsonl":
        return _import_jsonl(args)
    if args.command == "serve":
        return _serve(args)
    if args.command == "stats":
        return _stats(args)
    if args.command == "prune":
        return _prune(args)
    if args.command == "demo":
        return _demo(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
