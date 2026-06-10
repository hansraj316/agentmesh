import json
import uuid
from datetime import datetime, timezone

import pytest

from agentmesh.alerts import (
    Rule,
    evaluate,
    load_rules,
    post_webhook,
    render_alerts_markdown,
    to_webhook_payloads,
)
from agentmesh.cli import main
from agentmesh.events import Event
from agentmesh.store import EventStore

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minute, second=0):
    """A UTC timestamp within the hour before NOW."""
    return "2026-06-10T11:%02d:%02d+00:00" % (minute, second)


def _append(store, run_id, agent, type, ts, payload=None):
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type=type,
            ts=ts,
            payload=payload if payload is not None else {},
        )
    )


def _silent(name="quiet", agent="*", max_silence_minutes=30):
    return Rule(name=name, type="silent", agent=agent, threshold=max_silence_minutes)


def _streak(name="flaky", agent="*", min_streak=3):
    return Rule(name=name, type="failure_streak", agent=agent, threshold=min_streak)


def _write_rules(tmp_path, rules):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return path


@pytest.fixture
def fleet(store):
    """Two agents: researcher last seen 5m before NOW, critic on a 3-failure streak."""
    _append(store, "r1", "researcher", "agent_start", _ts(54))
    _append(store, "r1", "researcher", "agent_end", _ts(55), {"duration_seconds": 60.0})
    _append(store, "c1", "critic", "agent_start", _ts(10))
    _append(store, "c1", "critic", "agent_error", _ts(10, 1), {"error": "boom"})
    _append(store, "c2", "critic", "agent_start", _ts(11))
    _append(store, "c2", "critic", "agent_error", _ts(11, 1), {"error": "boom"})
    _append(store, "c3", "critic", "agent_start", _ts(12))
    _append(store, "c3", "critic", "agent_error", _ts(12, 1), {"error": "boom"})
    return store


# --- load_rules ---


def test_load_rules_missing_file_means_no_rules(tmp_path):
    assert load_rules(tmp_path / "nope.json") == []


def test_load_rules_parses_both_rule_types(tmp_path):
    path = _write_rules(
        tmp_path,
        [
            {"name": "quiet", "type": "silent", "agent": "*", "max_silence_minutes": 30},
            {"name": "flaky", "type": "failure_streak", "agent": "critic", "min_streak": 3},
        ],
    )
    rules = load_rules(path)
    assert rules == [
        Rule(name="quiet", type="silent", agent="*", threshold=30),
        Rule(name="flaky", type="failure_streak", agent="critic", threshold=3),
    ]


def test_load_rules_agent_defaults_to_wildcard(tmp_path):
    path = _write_rules(tmp_path, [{"name": "quiet", "type": "silent", "max_silence_minutes": 5}])
    assert load_rules(path)[0].agent == "*"


def test_load_rules_env_var_overrides_default(tmp_path, monkeypatch):
    path = _write_rules(tmp_path, [{"name": "quiet", "type": "silent", "max_silence_minutes": 5}])
    monkeypatch.setenv("AGENTMESH_ALERTS", str(path))
    assert [rule.name for rule in load_rules()] == ["quiet"]


def test_load_rules_rejects_unknown_type(tmp_path):
    path = _write_rules(tmp_path, [{"name": "weird", "type": "cpu_hot", "max_heat": 9}])
    with pytest.raises(ValueError, match="weird.*type must be one of"):
        load_rules(path)


def test_load_rules_rejects_missing_params(tmp_path):
    path = _write_rules(tmp_path, [{"name": "quiet", "type": "silent"}])
    with pytest.raises(ValueError, match="quiet.*max_silence_minutes must be a positive number"):
        load_rules(path)
    path = _write_rules(tmp_path, [{"name": "flaky", "type": "failure_streak", "min_streak": 0}])
    with pytest.raises(ValueError, match="flaky.*min_streak must be a positive number"):
        load_rules(path)


def test_load_rules_rejects_missing_name_and_non_list(tmp_path):
    path = _write_rules(tmp_path, [{"type": "silent", "max_silence_minutes": 5}])
    with pytest.raises(ValueError, match="name must be a non-empty string"):
        load_rules(path)
    path = _write_rules(tmp_path, {"name": "quiet"})
    with pytest.raises(ValueError, match="must contain a JSON list"):
        load_rules(path)


def test_load_rules_rejects_invalid_json(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_rules(path)


# --- silent rules ---


def test_silent_fires_when_latest_event_is_older_than_max(fleet):
    alerts = evaluate(fleet, [_silent(agent="researcher", max_silence_minutes=3)], now=NOW)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule_name == "quiet"
    assert alert.rule_type == "silent"
    assert alert.agent == "researcher"
    assert alert.severity == "warning"
    assert "silent for 5m (max 3m)" in alert.message
    assert alert.details == {
        "silence_minutes": 5.0,
        "max_silence_minutes": 3,
        "last_ts": _ts(55),
    }
    assert alert.fired_at == NOW.isoformat()


def test_silent_does_not_fire_within_the_window(fleet):
    assert evaluate(fleet, [_silent(agent="researcher", max_silence_minutes=10)], now=NOW) == []


def test_silent_does_not_fire_at_exactly_max_silence(fleet):
    assert evaluate(fleet, [_silent(agent="researcher", max_silence_minutes=5)], now=NOW) == []


def test_silent_wildcard_fires_once_per_silent_agent(fleet):
    alerts = evaluate(fleet, [_silent(agent="*", max_silence_minutes=30)], now=NOW)
    assert [alert.agent for alert in alerts] == ["critic"]  # researcher is only 5m old
    alerts = evaluate(fleet, [_silent(agent="*", max_silence_minutes=3)], now=NOW)
    assert [alert.agent for alert in alerts] == ["critic", "researcher"]


def test_silent_specific_agent_ignores_others(fleet):
    alerts = evaluate(fleet, [_silent(agent="critic", max_silence_minutes=3)], now=NOW)
    assert [alert.agent for alert in alerts] == ["critic"]


# --- failure_streak rules ---


def test_failure_streak_fires_at_exactly_min_streak(fleet):
    alerts = evaluate(fleet, [_streak(agent="critic", min_streak=3)], now=NOW)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule_type == "failure_streak"
    assert alert.severity == "critical"
    assert "failed 3 runs in a row (min 3)" in alert.message
    assert alert.details == {"streak": 3, "min_streak": 3, "last_ts": _ts(12, 1)}


def test_failure_streak_does_not_fire_below_min_streak(fleet):
    assert evaluate(fleet, [_streak(agent="critic", min_streak=4)], now=NOW) == []


def test_failure_streak_resets_after_a_success(fleet):
    _append(fleet, "c4", "critic", "agent_start", _ts(20))
    _append(fleet, "c4", "critic", "agent_end", _ts(20, 2), {"duration_seconds": 2.0})
    assert evaluate(fleet, [_streak(agent="critic", min_streak=1)], now=NOW) == []


def test_failure_streak_wildcard_skips_healthy_agents(fleet):
    alerts = evaluate(fleet, [_streak(agent="*", min_streak=1)], now=NOW)
    assert [alert.agent for alert in alerts] == ["critic"]


# --- rendering and payloads ---


def test_render_alerts_markdown_one_row_per_alert(fleet):
    rules = [_streak(agent="critic", min_streak=3), _silent(agent="*", max_silence_minutes=3)]
    markdown = render_alerts_markdown(evaluate(fleet, rules, now=NOW))
    lines = markdown.splitlines()
    assert lines[0] == "| Rule | Severity | Agent | Message |"
    assert (
        "| flaky | 🔴 critical | critic | agent critic has failed 3 runs in a row (min 3) |"
        in lines
    )
    assert (
        "| quiet | 🟡 warning | researcher | "
        "agent researcher has been silent for 5m (max 3m) |" in lines
    )


def test_webhook_payload_shape(fleet):
    alerts = evaluate(fleet, [_streak(agent="critic", min_streak=3)], now=NOW)
    payloads = to_webhook_payloads(alerts)
    assert len(payloads) == 1
    payload = payloads[0]
    assert set(payload) == {"source", "severity", "title", "body", "details", "fired_at"}
    assert payload["source"] == "agentmesh"
    assert payload["severity"] == "critical"
    assert payload["title"] == "flaky: critic"
    assert payload["body"] == alerts[0].message
    assert payload["details"]["streak"] == 3
    assert payload["fired_at"] == NOW.isoformat()
    json.dumps(payloads)  # JSON-serializable, end to end


def test_post_webhook_sends_json_body_and_headers():
    calls = []

    def poster(url, data, headers):
        calls.append((url, data, headers))

    payload = {"source": "agentmesh", "severity": "warning"}
    post_webhook("https://hooks.example.com/amesh", payload, poster=poster)
    assert len(calls) == 1
    url, data, headers = calls[0]
    assert url == "https://hooks.example.com/amesh"
    assert json.loads(data.decode("utf-8")) == payload
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "agentmesh"


# --- CLI ---


def test_cli_alerts_no_rules_file_prints_no_alerts(store, tmp_path, capsys):
    assert main(["alerts", "--rules", str(tmp_path / "missing.json")]) == 0
    assert capsys.readouterr().out.strip() == "No alerts."


def test_cli_alerts_prints_markdown_when_rules_fire(fleet, tmp_path, capsys):
    rules = _write_rules(
        tmp_path, [{"name": "flaky", "type": "failure_streak", "agent": "*", "min_streak": 3}]
    )
    assert main(["alerts", "--rules", str(rules)]) == 0
    out = capsys.readouterr().out
    assert "| Rule | Severity | Agent | Message |" in out
    assert "critic" in out
    assert "🔴 critical" in out


def test_cli_alerts_exit_code_flag_fails_when_alerts_fire(fleet, tmp_path, capsys):
    rules = _write_rules(
        tmp_path, [{"name": "flaky", "type": "failure_streak", "agent": "*", "min_streak": 3}]
    )
    assert main(["alerts", "--rules", str(rules), "--exit-code"]) == 1
    assert "critic" in capsys.readouterr().out


def test_cli_alerts_exit_code_flag_passes_when_quiet(fleet, tmp_path, capsys):
    rules = _write_rules(
        tmp_path, [{"name": "flaky", "type": "failure_streak", "agent": "*", "min_streak": 99}]
    )
    assert main(["alerts", "--rules", str(rules), "--exit-code"]) == 0
    assert capsys.readouterr().out.strip() == "No alerts."


def test_cli_alerts_malformed_rules_exit_nonzero(store, tmp_path, capsys):
    rules = _write_rules(tmp_path, [{"name": "weird", "type": "cpu_hot"}])
    assert main(["alerts", "--rules", str(rules)]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_alerts_posts_each_payload_to_webhook(fleet, tmp_path, capsys, monkeypatch):
    posted = []
    monkeypatch.setattr(
        "agentmesh.cli.post_webhook", lambda url, payload: posted.append((url, payload))
    )
    rules = _write_rules(
        tmp_path,
        [
            {"name": "flaky", "type": "failure_streak", "agent": "*", "min_streak": 3},
            {"name": "wobbly", "type": "failure_streak", "agent": "critic", "min_streak": 1},
        ],
    )
    assert main(["alerts", "--rules", str(rules), "--webhook", "https://h.example.com/x"]) == 0
    assert len(posted) == 2  # one payload per fired alert
    assert all(url == "https://h.example.com/x" for url, _ in posted)
    assert all(payload["source"] == "agentmesh" for _, payload in posted)
    assert {payload["title"] for _, payload in posted} == {"flaky: critic", "wobbly: critic"}


def test_cli_alerts_db_flag_points_at_another_store(tmp_path, db_path, capsys):
    other = tmp_path / "other.db"
    other_store = EventStore(other)
    _append(other_store, "x1", "loner", "agent_start", _ts(30))
    _append(other_store, "x1", "loner", "agent_error", _ts(30, 1), {"error": "boom"})
    rules = _write_rules(
        tmp_path, [{"name": "flaky", "type": "failure_streak", "agent": "loner", "min_streak": 1}]
    )
    assert main(["alerts", "--rules", str(rules), "--db", str(other)]) == 0
    assert "loner" in capsys.readouterr().out
    # The default (AGENTMESH_DB) store is empty, so the same rules stay quiet.
    assert main(["alerts", "--rules", str(rules)]) == 0
    assert capsys.readouterr().out.strip() == "No alerts."
