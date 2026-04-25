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

## Quick Start (coming soon)

```bash
pip install agentmesh
agentmesh up  # starts daemon + dashboard at localhost:7777
```

```python
from agentmesh import mesh

@mesh.agent(name="researcher")
async def researcher_agent(task: str) -> str:
    # your existing agent code — zero changes needed
    result = await call_llm(task)
    return result

@mesh.agent(name="orchestrator")
async def orchestrator(task: str):
    results = await asyncio.gather(
        researcher_agent(task),
        writer_agent(task),
    )
    return combine(results)
```

Open `localhost:7777` → see your agents as a live graph. Nodes = agents. Edges = messages. Colors = status.

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
- [ ] AMP protocol spec
- [ ] Python SDK (`@mesh.agent` decorator)
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
