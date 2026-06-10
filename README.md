# AgentMesh

> Real-time visibility and coordination for multi-agent AI systems.

[![Indian Avengers](https://img.shields.io/badge/Managed%20By-Indian%20Avengers-orange?style=flat-square&logo=gitbook)](https://github.com/hansraj316/mission-control-openclaw)
[![Status](https://img.shields.io/badge/Status-Sentinel%20Audited-green?style=flat-square)](https://github.com/hansraj316/agentmesh)

**Status:** Early design phase — contributions and feedback welcome.

## The Mission

AgentMesh is a core technical pillar of the **Indian Avengers** organization. It provides the observability required to scale a multi-agent "GitHub Factory" toward the **$1,000,000 revenue goal**. Every agent, from **Anusandhan (The Sentinel)** to **Parmanu (Engineering Commander)**, uses AgentMesh to ensure coordination is flawless and shipping is daily.

## The Problem

Multi-agent coordination failures account for **36.9% of all AI agent system failures** — and they compound 17x in unstructured "bag of agents" systems. When you have 5 agents running in parallel, you have no idea which one is stuck, which finished, what they're doing, or why one failed silently.

## What It Is

AgentMesh is an open-source observability and coordination layer for multi-agent AI systems. Add one decorator to your agents, run `agentmesh up`, and get a real-time dashboard showing every agent, every message, every tool call — live.

```
┌─────────────────────────────────────────┐
│  AgentMesh Cloud  (hosted, coming soon)  │
├─────────────────────────────────────────┤
│  AgentMesh Dashboard  (React, localhost) │  ← live agent graph
├─────────────────────────────────────────┤
│  AgentMesh Daemon  (FastAPI + SQLite)    │  ← event store
├─────────────────────────────────────────┤
│  AgentMesh SDK  (Python + TypeScript)    │  ← zero-friction instrumentation
├─────────────────────────────────────────┤
│  AMP — AgentMesh Protocol  (event spec) │  ← open standard
└─────────────────────────────────────────┘
```

## Quick Start

The Python SDK, SQLite event store, and `agentmesh tail` CLI work today
([AMP v0.1 spec](docs/amp-spec.md)). The daemon and dashboard are still future work.

```bash
git clone https://github.com/hansraj316/agentmesh && cd agentmesh
pip install -e .  # Python 3.9+, no runtime dependencies
```

Decorate any sync or async callable — `agent_start`, `agent_end` (with duration),
and `agent_error` events are recorded automatically:

```python
from agentmesh import mesh

@mesh.agent(name="researcher")
async def researcher_agent(task: str) -> str:
    # your existing agent code — zero changes needed
    return await call_llm(task)

@mesh.agent(name="writer")
def writer_agent(task: str) -> str:
    return draft(task)

# Group related calls under one run_id:
with mesh.run("my-run-42"):
    writer_agent("intro")
```

Events are stored in SQLite at `~/.agentmesh/events.db` (override with the
`AGENTMESH_DB` environment variable). Inspect them from the terminal:

```bash
agentmesh tail                 # last 20 events, one per line
agentmesh tail --run my-run-42 # only one run
agentmesh tail --agent writer -n 5
agentmesh tail --follow        # stream new events live (Ctrl-C to stop)
```

The live dashboard (`agentmesh up` at `localhost:7777`) is on the roadmap below — not shipped yet.

## Observe your GitHub Actions fleet

Treat a repo fleet as your first mesh: each workflow run becomes an agent run
(`agent_start`, then `agent_end` or `agent_error`) with deterministic event ids,
so re-ingesting is idempotent. Set `GITHUB_TOKEN` for private repos or higher
rate limits.

```bash
agentmesh ingest-gha hansraj316 --repo agentmesh --repo mission-control-openclaw --limit 50
agentmesh tail --agent agentmesh/daily-ci -n 10
```

Then render the fleet as a status board — one row per agent with its current
status, failure streak, and average duration:

```bash
agentmesh board
```

```
| Agent | Status | Last event | Age | Runs | Errors | Streak | Avg duration |
|-------|--------|------------|-----|------|--------|--------|--------------|
| agentmesh/daily-ci | 🟢 ok | agent_end | 2h | 12 | 1 | 0 | 41.3s |
| mission-control-openclaw/deploy | 🔴 failed | agent_error | 3m | 8 | 3 | 2 | 58.0s |
| mission-control-openclaw/nightly | 🔵 running | agent_start | 45s | 9 | 0 | 0 | 4m 12s |
```

`--since 2026-06-10T00:00:00+00:00` limits the window, `--out board.md` writes
the markdown to a file, and `--html board.html` also writes a self-contained
HTML dashboard (inline CSS, no JavaScript) you can open in any browser.

## Framework Support (v0.1 target)

| Framework | Adapter |
|-----------|---------|
| Claude Agent SDK | `agentmesh.adapters.claude` |
| LangGraph | `agentmesh.adapters.langgraph` |
| OpenAI Agents SDK | `agentmesh.adapters.openai` |
| CrewAI | `agentmesh.adapters.crewai` |
| Raw Python | `@mesh.agent` decorator |
| TypeScript | `@agentmesh/sdk` |

## The AMP Event Spec

Every agent emits structured events:

```json
{
  "event": "agent.tool.called",
  "agent_id": "researcher-01",
  "parent_agent_id": "orchestrator-01",
  "task_id": "task-abc123",
  "timestamp": "2026-03-17T21:00:00Z",
  "payload": {
    "tool": "web_search",
    "input": "latest AI agent frameworks 2026"
  }
}
```

Event types: `agent.started`, `agent.completed`, `agent.failed`, `agent.message.sent`, `agent.message.received`, `agent.tool.called`, `agent.tool.returned`

## Roadmap

### v0.1 — See Everything
- [x] AMP protocol spec ([docs/amp-spec.md](docs/amp-spec.md))
- [x] Python SDK (`@mesh.agent` decorator + SQLite event store + `agentmesh tail` CLI)
- [ ] AgentMesh Daemon (FastAPI + SQLite)
- [ ] Real-time Dashboard (React + WebSocket)
- [ ] Claude Agent SDK adapter
- [ ] LangGraph adapter

### v0.2 — Understand Everything
- [ ] Timeline view (what ran, how long, what failed)
- [ ] Replay mode (replay any run from events)
- [ ] OpenAI Agents SDK adapter
- [ ] CrewAI adapter
- [ ] TypeScript SDK

### v0.3 — Fix Everything
- [ ] Diff view (compare two runs)
- [ ] Cost tracking per agent
- [ ] Circuit breakers (auto-stop runaway agents)
- [ ] Alert rules

### v1.0 — Ship to Production
- [ ] AgentMesh Cloud (hosted)
- [ ] Team collaboration
- [ ] Agent evaluation (LLM-as-judge)
- [ ] Regression test suite for agent behavior

## License

Apache 2.0

## Reference Implementation

[Mission Control](https://github.com/hansraj316/mission-control-openclaw) is a working AgentMesh-style dashboard built with Python + SQLite, battle-tested with a 25-agent autonomous AI organization. It implements the core visibility loop: real-time agent status, session timeline, health scores, security telemetry, and a cron/job runner. AgentMesh v0.1 will generalize this into an installable SDK + daemon.


## Daily TPM delivery update (2026-04-22)
- Functional: Ship visual topology map of agents, dependencies, and message throughput
- Non-functional: Implement chaos test suite for retry, timeout, and circuit-breaker behavior


## Daily delivery update (2026-04-28)
- Functional: Add multi-agent handoff visualization with state transitions
- Non-functional: Optimize queue throughput under high-concurrency workloads
