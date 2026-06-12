import itertools
import json
import uuid

import pytest

from agentmesh.cli import main
from agentmesh.config import Config, effective_value, load_config, render_config, resolve_settings
from agentmesh.events import Event
from agentmesh.store import EventStore


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Fresh HOME and no AGENTMESH_* env vars, so the host machine leaks nothing in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("AGENTMESH_CONFIG", "AGENTMESH_DB", "AGENTMESH_ALERTS"):
        monkeypatch.delenv(name, raising=False)
    return home


def _write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _seed_event(db_path, agent="alpha", run_id="run-1", ts="2026-06-01T12:00:00+00:00"):
    store = EventStore(db_path)
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type="agent_start",
            ts=ts,
            payload={"function": agent},
        )
    )
    return store


def _span(store, run_id, agent, span_id, duration, start="2026-06-01T12:00:00+00:00"):
    end_ts = start.replace("12:00:00", "12:00:05")
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type="agent_start",
            ts=start,
            payload={"function": agent, "span_id": span_id},
        )
    )
    store.append(
        Event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            agent=agent,
            type="agent_end",
            ts=end_ts,
            payload={"span_id": span_id, "duration_seconds": duration},
        )
    )


# ---------------------------------------------------------------- load_config


def test_load_missing_file_returns_empty_config(tmp_path):
    assert load_config(tmp_path / "nope.json") == Config()


def test_load_default_path_is_under_home(isolated_env):
    _write_config(isolated_env / ".agentmesh" / "config.json", {"port": 9001})
    assert load_config().port == 9001


def test_load_env_override(tmp_path, monkeypatch):
    path = _write_config(tmp_path / "custom.json", {"window": 5})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(path))
    assert load_config().window == 5


def test_load_explicit_path_beats_env(tmp_path, monkeypatch):
    env_path = _write_config(tmp_path / "env.json", {"port": 1111})
    explicit = _write_config(tmp_path / "explicit.json", {"port": 2222})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(env_path))
    assert load_config(explicit).port == 2222


def test_load_unknown_key_error_names_it(tmp_path):
    path = _write_config(tmp_path / "config.json", {"prot": 80, "db": "x"})
    with pytest.raises(ValueError) as exc:
        load_config(path)
    assert "prot" in str(exc.value)
    assert "unknown config key" in str(exc.value)


def test_load_invalid_json_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_config(path)


def test_load_non_object_raises(tmp_path):
    path = _write_config(tmp_path / "config.json", ["db"])
    with pytest.raises(ValueError, match="JSON object"):
        load_config(path)


@pytest.mark.parametrize(
    "key,bad_value",
    [
        ("db", 123),
        ("rules", ["a.json"]),
        ("port", "7777"),
        ("port", True),
        ("port", 7777.0),
        ("threshold", "twenty"),
        ("threshold", False),
        ("window", 2.5),
        ("window", "5"),
        ("since", 20260101),
    ],
)
def test_load_type_validation_per_key(tmp_path, key, bad_value):
    path = _write_config(tmp_path / "config.json", {key: bad_value})
    with pytest.raises(ValueError) as exc:
        load_config(path)
    assert "'%s'" % key in str(exc.value)


def test_load_threshold_int_becomes_float(tmp_path):
    path = _write_config(tmp_path / "config.json", {"threshold": 30})
    config = load_config(path)
    assert config.threshold == 30.0
    assert isinstance(config.threshold, float)


def test_load_all_keys(tmp_path):
    path = _write_config(
        tmp_path / "config.json",
        {
            "db": "/tmp/e.db",
            "rules": "/tmp/r.json",
            "port": 8080,
            "threshold": 12.5,
            "window": 10,
            "since": "2026-01-01T00:00:00Z",
        },
    )
    config = load_config(path)
    assert config == Config(
        db="/tmp/e.db",
        rules="/tmp/r.json",
        port=8080,
        threshold=12.5,
        window=10,
        since="2026-01-01T00:00:00Z",
    )


# ------------------------------------------------------------ effective_value


@pytest.mark.parametrize(
    "flag,env,cfg",
    list(itertools.product([None, "/flag.db"], [None, "/env.db"], [None, "/cfg.db"])),
)
def test_effective_value_precedence_matrix(flag, env, cfg):
    expected = flag or env or cfg or "/default.db"
    value = effective_value("db", flag, Config(db=cfg), env_value=env, default="/default.db")
    assert value == expected


def test_effective_value_unknown_name_raises():
    with pytest.raises(ValueError, match="limit"):
        effective_value("limit", None, Config())


def test_resolve_settings_reports_sources(monkeypatch):
    monkeypatch.setenv("AGENTMESH_ALERTS", "/env/rules.json")
    resolved = resolve_settings(Config(port=9000))
    assert resolved["port"] == (9000, "config")
    assert resolved["rules"] == ("/env/rules.json", "env")
    assert resolved["threshold"] == (20.0, "default")
    assert resolved["window"] == (None, "default")
    db_value, db_source = resolved["db"]
    assert db_source == "default"
    assert db_value.endswith("events.db")


def test_render_config_lists_values_and_sources():
    config = Config(since="2026-01-01T00:00:00Z")
    text = render_config(config, resolve_settings(config))
    assert "| key | effective value | source |" in text
    assert "| since | 2026-01-01T00:00:00Z | config |" in text
    assert "| port | 7777 | default |" in text
    assert "flags always override" in text


# -------------------------------------------------------- CLI integration: db


def test_cli_uses_config_db_when_no_flag(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    _seed_event(db, agent="from-config")
    config = _write_config(tmp_path / "config.json", {"db": str(db)})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["tail"]) == 0
    assert "agent=from-config" in capsys.readouterr().out


def test_cli_db_flag_beats_config(tmp_path, monkeypatch, capsys):
    config_db = tmp_path / "config.db"
    flag_db = tmp_path / "flag.db"
    _seed_event(config_db, agent="from-config")
    _seed_event(flag_db, agent="from-flag")
    config = _write_config(tmp_path / "config.json", {"db": str(config_db)})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["tail", "--db", str(flag_db)]) == 0
    out = capsys.readouterr().out
    assert "agent=from-flag" in out
    assert "agent=from-config" not in out


def test_cli_db_env_beats_config(tmp_path, monkeypatch, capsys):
    config_db = tmp_path / "config.db"
    env_db = tmp_path / "env.db"
    _seed_event(config_db, agent="from-config")
    _seed_event(env_db, agent="from-env")
    config = _write_config(tmp_path / "config.json", {"db": str(config_db)})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    monkeypatch.setenv("AGENTMESH_DB", str(env_db))
    assert main(["tail"]) == 0
    out = capsys.readouterr().out
    assert "agent=from-env" in out
    assert "agent=from-config" not in out


def test_cli_global_config_flag_beats_env_config(tmp_path, monkeypatch, capsys):
    env_db = tmp_path / "envcfg.db"
    flag_db = tmp_path / "flagcfg.db"
    _seed_event(env_db, agent="from-env-config")
    _seed_event(flag_db, agent="from-flag-config")
    env_config = _write_config(tmp_path / "env-config.json", {"db": str(env_db)})
    flag_config = _write_config(tmp_path / "flag-config.json", {"db": str(flag_db)})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(env_config))
    assert main(["--config", str(flag_config), "tail"]) == 0
    assert "agent=from-flag-config" in capsys.readouterr().out


def test_cli_invalid_config_reports_error(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path / "config.json", {"bogus": 1})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["tail"]) == 1
    assert "bogus" in capsys.readouterr().err


# ------------------------------------------------------ CLI integration: port


def _capture_serve(monkeypatch):
    calls = {}

    def fake_serve(db_path, port, rules_path=None):
        calls["db"] = db_path
        calls["port"] = port
        calls["rules"] = rules_path

    monkeypatch.setattr("agentmesh.cli.serve", fake_serve)
    return calls


def test_serve_port_from_config(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path / "config.json", {"port": 9009, "rules": "/cfg/rules.json"})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    calls = _capture_serve(monkeypatch)
    assert main(["serve"]) == 0
    assert calls["port"] == 9009
    assert calls["rules"] == "/cfg/rules.json"
    assert "http://127.0.0.1:9009/" in capsys.readouterr().out


def test_serve_port_flag_beats_config(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path / "config.json", {"port": 9009})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    calls = _capture_serve(monkeypatch)
    assert main(["serve", "--port", "8088", "--rules", "/flag/rules.json"]) == 0
    assert calls["port"] == 8088
    assert calls["rules"] == "/flag/rules.json"


def test_serve_port_defaults_to_7777_without_config(monkeypatch, capsys):
    calls = _capture_serve(monkeypatch)
    assert main(["serve"]) == 0
    assert calls["port"] == 7777


# ------------------------------------------------- CLI integration: threshold


def _seed_diff_runs(tmp_path):
    db = tmp_path / "diff.db"
    store = EventStore(db)
    _span(store, "run-a", "worker", "s1", duration=1.0)
    _span(store, "run-b", "worker", "s1", duration=1.5, start="2026-06-01T13:00:00+00:00")
    return db


def test_diff_threshold_from_config(tmp_path, monkeypatch, capsys):
    db = _seed_diff_runs(tmp_path)
    config = _write_config(tmp_path / "config.json", {"db": str(db), "threshold": 60})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["diff", "run-a", "run-b"]) == 0
    # +50% duration change is below the configured ±60% threshold.
    assert "No significant changes (threshold ±60.0%)" in capsys.readouterr().out


def test_diff_threshold_flag_beats_config(tmp_path, monkeypatch, capsys):
    db = _seed_diff_runs(tmp_path)
    config = _write_config(tmp_path / "config.json", {"db": str(db), "threshold": 60})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["diff", "run-a", "run-b", "--threshold", "20"]) == 0
    assert "No significant changes" not in capsys.readouterr().out


# ---------------------------------------------------- CLI integration: window


def test_flaky_window_from_config(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    _seed_event(db)
    # window=0 is rejected by the flaky command, proving the config value
    # travelled all the way into agent_outcomes().
    config = _write_config(tmp_path / "config.json", {"db": str(db), "window": 0})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["flaky"]) == 1
    assert "window must be a positive integer, got 0" in capsys.readouterr().err


def test_flaky_window_flag_beats_config(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    _seed_event(db)
    config = _write_config(tmp_path / "config.json", {"db": str(db), "window": 0})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["flaky", "--window", "3"]) == 0


# ----------------------------------------------------- CLI integration: since


def test_latency_since_from_config(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    store = EventStore(db)
    _span(store, "run-1", "worker", "s1", duration=1.0)
    config = _write_config(
        tmp_path / "config.json", {"db": str(db), "since": "2026-01-01T00:00:00+00:00"}
    )
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["latency"]) == 0
    assert "since 2026-01-01T00:00:00+00:00" in capsys.readouterr().out


def test_latency_since_flag_beats_config(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    store = EventStore(db)
    _span(store, "run-1", "worker", "s1", duration=1.0)
    config = _write_config(
        tmp_path / "config.json", {"db": str(db), "since": "2026-01-01T00:00:00+00:00"}
    )
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    assert main(["latency", "--since", "2026-05-01T00:00:00+00:00"]) == 0
    assert "since 2026-05-01T00:00:00+00:00" in capsys.readouterr().out


# -------------------------------------------------- agentmesh config command


def test_config_command_shows_values_and_sources(tmp_path, monkeypatch, capsys):
    config = _write_config(tmp_path / "config.json", {"db": "/cfg/events.db", "port": 9000})
    monkeypatch.setenv("AGENTMESH_CONFIG", str(config))
    monkeypatch.setenv("AGENTMESH_ALERTS", "/env/rules.json")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "| db | /cfg/events.db | config |" in out
    assert "| port | 9000 | config |" in out
    assert "| rules | /env/rules.json | env |" in out
    assert "| threshold | 20.0 | default |" in out
    assert "flags always override" in out


def test_config_command_accepts_config_flag_after_subcommand(tmp_path, capsys):
    config = _write_config(tmp_path / "config.json", {"window": 25})
    assert main(["config", "--config", str(config)]) == 0
    assert "| window | 25 | config |" in capsys.readouterr().out


def test_config_command_with_global_flag(tmp_path, capsys):
    config = _write_config(tmp_path / "config.json", {"threshold": 33.5})
    assert main(["--config", str(config), "config"]) == 0
    assert "| threshold | 33.5 | config |" in capsys.readouterr().out


def test_config_command_empty_environment_all_defaults(capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "| port | 7777 | default |" in out
    assert "| window | — | default |" in out
    assert out.count("| default |") == 6
