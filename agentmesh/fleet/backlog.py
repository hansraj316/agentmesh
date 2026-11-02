"""The fleet's backlog: realistic goals for a small repo, and how the scripted
Planner splits each one into tasks."""

from typing import Dict, List

BACKLOG: Dict[str, List[str]] = {
    "Make GitHub ingestion reliable": [
        "Add retry to GitHub sync",
        "Handle 429 rate limits in ingest-gha",
        "Review PR #41",
    ],
    "Ship OTLP export to Honeycomb": [
        "Map usage fields onto OTLP span attributes",
        "Add gzip to the OTLP exporter",
        "Document the export-otlp command",
    ],
    "Cut alert noise": [
        "Debounce repeated failure-streak alerts",
        "Add a quiet-hours window to webhook alerts",
        "Review PR #57",
    ],
    "Tighten cost reporting": [
        "Group costs by model family",
        "Flag runs with missing token usage",
        "Add a cost column to the HTML board",
    ],
}

GOALS: List[str] = list(BACKLOG)


def tasks_for_goal(goal: str) -> List[str]:
    """Tasks the scripted Planner hands out for ``goal``.

    An unknown goal (for example one typed by a user) gets a generic split so
    the scripted model never fails on planning.
    """
    if goal in BACKLOG:
        return list(BACKLOG[goal])
    return [
        "Write a design note for: %s" % goal,
        "Implement: %s" % goal,
        "Add tests for: %s" % goal,
    ]
