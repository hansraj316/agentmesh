import pytest

from agentmesh.store import EventStore


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point AGENTMESH_DB at a fresh temp database for the test."""
    path = tmp_path / "events.db"
    monkeypatch.setenv("AGENTMESH_DB", str(path))
    return path


@pytest.fixture
def store(db_path):
    return EventStore(db_path)
