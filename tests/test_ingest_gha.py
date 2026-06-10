import json

import pytest

import agentmesh.ingest_gha
from agentmesh.cli import main
from agentmesh.ingest_gha import fetch_runs, ingest, runs_to_events


def _run(
    run_id,
    name="ci",
    status="completed",
    conclusion="success",
    started="2026-06-09T10:00:00Z",
    updated="2026-06-09T10:05:30Z",
):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "run_started_at": started,
        "created_at": started,
        "updated_at": updated,
        "html_url": "https://github.com/acme/widgets/actions/runs/%d" % run_id,
        "event": "push",
        "head_branch": "main",
    }


class FakeFetcher:
    """Records every request and serves canned runs keyed by repo name."""

    def __init__(self, runs_by_repo):
        self.runs_by_repo = runs_by_repo
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        repo = url.split("/repos/")[1].split("/")[1]
        return json.dumps({"workflow_runs": self.runs_by_repo.get(repo, [])}).encode()


def test_fetch_runs_url_headers_and_parsing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fetcher = FakeFetcher({"widgets": [_run(1)]})
    runs = fetch_runs("acme", "widgets", limit=7, fetcher=fetcher)
    assert [run["id"] for run in runs] == [1]
    url, headers = fetcher.calls[0]
    assert url == "https://api.github.com/repos/acme/widgets/actions/runs?per_page=7"
    assert headers["User-Agent"] == "agentmesh"
    assert headers["Accept"] == "application/vnd.github+json"
    assert "Authorization" not in headers


def test_fetch_runs_sends_token_when_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    fetcher = FakeFetcher({"widgets": []})
    fetch_runs("acme", "widgets", fetcher=fetcher)
    _, headers = fetcher.calls[0]
    assert headers["Authorization"] == "Bearer ghp_secret"


def test_completed_success_maps_to_start_and_end():
    events = runs_to_events("acme", "widgets", [_run(42)])
    assert [event.type for event in events] == ["agent_start", "agent_end"]
    start, end = events
    assert start.event_id == "gha-42-start"
    assert end.event_id == "gha-42-end"
    assert start.run_id == end.run_id == "gha-42"
    assert start.agent == end.agent == "widgets/ci"
    assert start.ts == "2026-06-09T10:00:00Z"
    assert end.ts == "2026-06-09T10:05:30Z"
    assert start.payload == {
        "html_url": "https://github.com/acme/widgets/actions/runs/42",
        "event": "push",
        "head_branch": "main",
    }
    assert end.payload == {
        "conclusion": "success",
        "html_url": "https://github.com/acme/widgets/actions/runs/42",
        "event": "push",
        "head_branch": "main",
        "duration_seconds": 330.0,
    }


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
def test_failed_run_maps_to_agent_error(conclusion):
    events = runs_to_events("acme", "widgets", [_run(7, conclusion=conclusion)])
    assert [event.type for event in events] == ["agent_start", "agent_error"]
    error = events[1]
    assert error.event_id == "gha-7-end"
    assert error.payload == {
        "conclusion": conclusion,
        "html_url": "https://github.com/acme/widgets/actions/runs/7",
    }


def test_in_progress_run_emits_only_start():
    events = runs_to_events("acme", "widgets", [_run(9, status="in_progress", conclusion=None)])
    assert [event.type for event in events] == ["agent_start"]
    assert events[0].event_id == "gha-9-start"


def test_ingest_is_idempotent(store):
    fetcher = FakeFetcher({"widgets": [_run(1), _run(2, conclusion="failure")]})
    first = ingest("acme", ["widgets"], store, fetcher=fetcher)
    assert first == {"widgets": 4}
    second = ingest("acme", ["widgets"], store, fetcher=fetcher)
    assert second == {"widgets": 0}
    assert len(store.query(limit=100)) == 4


def test_ingest_counts_per_repo(store):
    fetcher = FakeFetcher(
        {
            "widgets": [_run(1)],
            "gadgets": [_run(2), _run(3, status="in_progress", conclusion=None)],
        }
    )
    counts = ingest("acme", ["widgets", "gadgets"], store, fetcher=fetcher)
    assert counts == {"widgets": 2, "gadgets": 3}
    agents = {event.agent for event in store.query(limit=100)}
    assert agents == {"widgets/ci", "gadgets/ci"}


def test_ingest_respects_limit_per_repo(store):
    fetcher = FakeFetcher({"widgets": []})
    ingest("acme", ["widgets"], store, limit_per_repo=5, fetcher=fetcher)
    url, _ = fetcher.calls[0]
    assert url.endswith("per_page=5")


def test_cli_ingest_gha_stores_events_and_prints_counts(store, capsys, monkeypatch):
    fetcher = FakeFetcher({"widgets": [_run(1)], "gadgets": [_run(2, conclusion="failure")]})
    monkeypatch.setattr(agentmesh.ingest_gha, "_default_fetcher", fetcher)
    assert main(["ingest-gha", "acme", "--repo", "widgets", "--repo", "gadgets"]) == 0
    out = capsys.readouterr().out
    assert "acme/widgets: 2 new events" in out
    assert "acme/gadgets: 2 new events" in out
    assert len(store.query(limit=100)) == 4
    # Re-ingest via the CLI adds nothing.
    assert main(["ingest-gha", "acme", "--repo", "widgets", "--repo", "gadgets"]) == 0
    assert len(store.query(limit=100)) == 4


def test_cli_ingest_gha_passes_limit(store, capsys, monkeypatch):
    fetcher = FakeFetcher({"widgets": []})
    monkeypatch.setattr(agentmesh.ingest_gha, "_default_fetcher", fetcher)
    assert main(["ingest-gha", "acme", "--repo", "widgets", "--limit", "3"]) == 0
    url, _ = fetcher.calls[0]
    assert url.endswith("per_page=3")


def test_cli_ingest_gha_without_repo_errors(db_path):
    with pytest.raises(SystemExit) as excinfo:
        main(["ingest-gha", "acme"])
    assert excinfo.value.code != 0
