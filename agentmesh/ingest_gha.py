"""GitHub Actions ingestor: observe a repo fleet as AMP events.

Each workflow run becomes an agent named ``{repo}/{workflow_name}`` with a
deterministic ``run_id`` (``gha-{run.id}``) and deterministic event ids, so
re-ingesting the same runs is idempotent.
"""

import json
import os
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from agentmesh.events import Event, _parse_ts
from agentmesh.store import EventStore

GITHUB_API = "https://api.github.com"

_USER_AGENT = "agentmesh"

_ERROR_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

Fetcher = Callable[[str, Dict[str, str]], bytes]


def _default_fetcher(url: str, headers: Dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request) as response:
        return response.read()


def fetch_runs(
    owner: str,
    repo: str,
    limit: int = 30,
    fetcher: Optional[Fetcher] = None,
) -> List[Dict[str, Any]]:
    """Fetch the most recent workflow runs for one repo via the GitHub REST API.

    Uses only the standard library; ``GITHUB_TOKEN`` (if set) is sent as a
    bearer token. ``fetcher(url, headers) -> bytes`` is injectable for tests.
    """
    if fetcher is None:
        fetcher = _default_fetcher
    url = "%s/repos/%s/%s/actions/runs?per_page=%d" % (GITHUB_API, owner, repo, limit)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer %s" % token
    data = json.loads(fetcher(url, headers))
    return data.get("workflow_runs", [])


def runs_to_events(owner: str, repo: str, runs: List[Dict[str, Any]]) -> List[Event]:
    """Map GitHub workflow runs to AMP events.

    Every run yields an ``agent_start`` (ts = ``run_started_at``); completed
    runs additionally yield an ``agent_end``, or an ``agent_error`` when the
    conclusion is one of ``failure``/``timed_out``/``startup_failure``
    (ts = ``updated_at``). In-progress runs yield only the start event.
    """
    events = []
    for run in runs:
        run_id = "gha-%s" % run["id"]
        agent = "%s/%s" % (repo, run["name"])
        started_ts = run.get("run_started_at") or run["created_at"]
        events.append(
            Event(
                event_id="%s-start" % run_id,
                run_id=run_id,
                agent=agent,
                type="agent_start",
                ts=started_ts,
                payload={
                    "html_url": run.get("html_url"),
                    "event": run.get("event"),
                    "head_branch": run.get("head_branch"),
                },
            )
        )
        if run.get("status") != "completed":
            continue
        conclusion = run.get("conclusion")
        ended_ts = run["updated_at"]
        if conclusion in _ERROR_CONCLUSIONS:
            type_ = "agent_error"
            payload: Dict[str, Any] = {
                "conclusion": conclusion,
                "html_url": run.get("html_url"),
            }
        else:
            type_ = "agent_end"
            duration = _parse_ts(ended_ts) - _parse_ts(started_ts)
            payload = {
                "conclusion": conclusion,
                "html_url": run.get("html_url"),
                "event": run.get("event"),
                "head_branch": run.get("head_branch"),
                "duration_seconds": duration.total_seconds(),
            }
        events.append(
            Event(
                event_id="%s-end" % run_id,
                run_id=run_id,
                agent=agent,
                type=type_,
                ts=ended_ts,
                payload=payload,
            )
        )
    return events


def ingest(
    owner: str,
    repos: List[str],
    store: EventStore,
    limit_per_repo: int = 30,
    fetcher: Optional[Fetcher] = None,
) -> Dict[str, int]:
    """Fetch and store recent runs for each repo; return {repo: n_events_added}.

    Deterministic event ids plus duplicate-skipping appends make re-ingest
    idempotent: already-seen events count as 0.
    """
    counts = {}
    for repo in repos:
        added = 0
        runs = fetch_runs(owner, repo, limit=limit_per_repo, fetcher=fetcher)
        for event in runs_to_events(owner, repo, runs):
            if store.append_ignore_duplicates(event):
                added += 1
        counts[repo] = added
    return counts
