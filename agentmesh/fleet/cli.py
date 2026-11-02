"""``agentmesh fleet``: run the example Planner, Coder and Reviewer fleet.

``add_cli(subparsers)`` registers the command and sets ``args.handler``;
``agentmesh/cli.py`` wires it in (build plan P5).
"""

import argparse
import sys
import threading
from typing import Any, Optional

from agentmesh.fleet.llm import LLMError, make_llm
from agentmesh.fleet.runner import RunSummary, run_fleet, run_forever
from agentmesh.store import EventStore


def add_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``agentmesh fleet [--once] [--llm scripted|claude] [--db PATH]``."""
    parser = subparsers.add_parser(
        "fleet",
        help="Run the example Planner, Coder and Reviewer fleet against the event store.",
        description=(
            "Run a small multi-agent fleet (Planner, Coder, Reviewer) that hands tasks to "
            "each other, so every AgentMesh view has live data. The default scripted model "
            "works offline; --llm claude calls Claude using ANTHROPIC_API_KEY and the "
            "model in AGENTMESH_FLEET_MODEL."
        ),
    )
    parser.add_argument(
        "--once", action="store_true", help="Run one goal through the fleet, then exit."
    )
    parser.add_argument(
        "--llm",
        choices=("scripted", "claude"),
        default="scripted",
        help="Model behind the agents (default: scripted, no network).",
    )
    parser.add_argument("--db", default=None, metavar="PATH", help="Event store path.")
    parser.add_argument(
        "--seed", type=int, default=None, help="Seed for the scripted model (repeatable runs)."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="Pause between runs when running continuously (default: 3).",
    )
    parser.set_defaults(handler=fleet_command)
    return parser


def format_summary(summary: RunSummary) -> str:
    parts = ["%d done" % len(summary.done)]
    if summary.failed:
        parts.append("%d failed" % len(summary.failed))
    if summary.sent_back:
        parts.append("%d sent back by review" % len(summary.sent_back))
    line = "%s  %s: %s" % (summary.run_id, summary.goal, ", ".join(parts))
    if summary.stopped_early:
        line += " (stopped early)"
    return line


def fleet_command(args: argparse.Namespace, out: Optional[Any] = None) -> int:
    out = out if out is not None else sys.stdout
    try:
        llm = make_llm(args.llm, seed=args.seed)
    except LLMError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    store = EventStore(args.db)
    if args.once:
        print(format_summary(run_fleet(store, llm=llm, seed=args.seed)), file=out)
        return 0
    stop = threading.Event()
    print("fleet running against %s (Ctrl-C to stop)" % store.path, file=out)
    worker = threading.Thread(
        target=run_forever,
        args=(store, stop),
        kwargs={
            "interval_s": args.interval,
            "llm": llm,
            "on_run": lambda s: print(format_summary(s), file=out, flush=True),
        },
        daemon=True,
    )
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        worker.join(10.0)
    return 0
