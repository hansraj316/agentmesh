# AgentMesh — Design Document

**Date:** 2026-03-17
**Author:** Hansraj Singh Thakur
**Status:** Draft

---

## Problem Statement

Multi-agent coordination failures account for **36.9% of all AI agent system failures**, compounding 17x in unstructured "bag of agents" systems. The core developer experience problem: when you have multiple agents running, you have no idea what each one is doing, where things are stuck, or why a task silently failed.

No open-source tool covers the full visibility loop for multi-agent systems. Langfuse, Braintrust, and Galileo are partial solutions targeting single-agent tracing. Nobody has built the coordination + visibility layer for multi-agent workflows.

---

## Goals

1. **Visibility first** — a developer should be able to see their entire agent graph live with one decorator added
2. **Zero friction** — `pip install agentmesh` + `@mesh.agent` + `agentmesh up` is the entire onboarding
3. **Framework-agnostic** — works with Claude SDK, LangGraph, CrewAI, OpenAI Agents SDK, or raw Python
4. **OSS-first** — Apache 2.0, community-driven, cloud product is additive not extractive
5. **Protocol-open** — AMP spec is open so any framework can implement it natively

---

## Non-Goals (v0.1)

- Agent evaluation / LLM-as-judge scoring (v0.3+)
- Cost enforcement / circuit breakers (v0.3+)
- Cross-machine coordination (v1.0)
- Enterprise SSO / RBAC (cloud product)

---

## Architecture

### Five Layers

```
┌─────────────────────────────────────────┐
│  AgentMesh Cloud  (hosted product)       │  SaaS — team collab, alerting
├─────────────────────────────────────────┤
│  AgentMesh Dashboard  (React + WS)       │  localhost:7777, live agent graph
├─────────────────────────────────────────┤
│  AgentMesh Daemon  (FastAPI + SQLite)    │  local event store + WS broadcaster
├─────────────────────────────────────────┤
│  AgentMesh SDK  (Python + TypeScript)    │  decorator/wrapper, framework adapters
├─────────────────────────────────────────┤
│  AMP — AgentMesh Protocol               │  open event schema spec
└─────────────────────────────────────────┘
```

Each layer is independently useful. A developer can use just the SDK + Daemon without the dashboard. The protocol is what enables ecosystem adoption.

---

## AMP — AgentMesh Protocol

### Event Schema

All events share a common envelope:

```json
{
  "amp_version": "0.1",
  "event": "<event_type>",
  "event_id": "<uuid>",
  "agent_id": "<string>",
  "parent_agent_id": "<string | null>",
  "task_id": "<string>",
  "session_id": "<string>",
  "timestamp": "<ISO 8601>",
  "payload": {}
}
```

### Event Types

| Event | Payload |
|-------|---------|
| `agent.started` | `{ name, framework, config }` |
| `agent.completed` | `{ result_summary, token_count, duration_ms }` |
| `agent.failed` | `{ error, error_type, stack_trace }` |
| `agent.message.sent` | `{ to_agent_id, content_summary, message_type }` |
| `agent.message.received` | `{ from_agent_id, content_summary, message_type }` |
| `agent.tool.called` | `{ tool_name, input_summary }` |
| `agent.tool.returned` | `{ tool_name, output_summary, duration_ms, error? }` |
| `agent.thinking` | `{ thought_summary }` |

### Transport

Events are emitted over:
1. **Local Unix socket** (default, zero latency, same machine)
2. **HTTP POST** (fallback, cross-process, cross-machine)
3. **Optional: Redis/NATS** (production multi-machine deployments)

---

## SDK Design

### Python

```python
from agentmesh import mesh

# Decorator — wraps any async function
@mesh.agent(name="researcher", tags=["research"])
async def researcher_agent(task: str) -> str:
    result = await call_llm(task)
    return result

# Context manager — for class-based agents
async def my_agent():
    async with mesh.span("custom-step") as span:
        result = await do_work()
        span.set_output(result)
```

### TypeScript

```typescript
import { mesh } from '@agentmesh/sdk'

const researcher = mesh.agent('researcher', async (task: string) => {
  return await callLLM(task)
})
```

### Framework Adapters (v0.1)

**Claude Agent SDK:**
```python
from agentmesh.adapters.claude import MeshClaudeAgent
agent = MeshClaudeAgent(model="claude-opus-4-6", tools=[...])
```

**LangGraph:**
```python
from agentmesh.adapters.langgraph import MeshCheckpointer
graph = StateGraph(...).compile(checkpointer=MeshCheckpointer())
```

**OpenAI Agents SDK:**
```python
from agentmesh.adapters.openai import mesh_runner
result = await mesh_runner.run(agent, task)
```

---

## Daemon

A local FastAPI server that:
- Listens on Unix socket `/tmp/agentmesh.sock` for events
- Persists events to SQLite (`~/.agentmesh/events.db`)
- Broadcasts events over WebSocket to connected dashboard clients
- Exposes REST API for querying historical runs

### Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/runs` | List all task runs |
| `GET` | `/runs/{task_id}` | Full event log for a run |
| `GET` | `/runs/{task_id}/graph` | Agent graph topology for a run |
| `WS` | `/live` | Real-time event stream |

### Storage Schema (SQLite)

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE,
  task_id TEXT,
  session_id TEXT,
  agent_id TEXT,
  parent_agent_id TEXT,
  event_type TEXT,
  timestamp TEXT,
  payload JSON,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_task_id ON events(task_id);
CREATE INDEX idx_agent_id ON events(agent_id);
```

---

## Dashboard

React + TypeScript single-page app served by the Daemon at `localhost:7777`.

### Views

**1. Live Graph View** (default)
- D3-force directed graph of running agents
- Nodes: agents (colored by status: blue=running, green=done, yellow=waiting, red=failed)
- Edges: messages between agents (animated when active)
- Sidebar: click any node to see its event stream

**2. Timeline View**
- Gantt-style timeline of all agents in a run
- Shows: when each started/ended, tool calls as markers, messages as connectors
- Identify bottlenecks at a glance

**3. Replay View**
- Step through any historical run event by event
- See the agent graph evolve from start to finish
- Essential for debugging failures

**4. Runs List**
- All historical runs with: task name, duration, agent count, status, token count
- Click any run to open in Graph/Timeline/Replay view

### Tech Stack
- React 19 + TypeScript
- Vite (build)
- D3.js (graph visualization)
- WebSocket (real-time events)
- TanStack Query (data fetching)
- Tailwind CSS + shadcn/ui

---

## OSS Strategy

### Launch Plan

1. **Build the demo first** — get to a GIF-worthy dashboard in week 1-2
2. **Write the post** — "I built a tool to see what my agents are actually doing" (HN, DEV.to)
3. **Ship the adapters** — Claude SDK, LangGraph on launch; CrewAI + OpenAI in week 2
4. **Contribute upstream** — PRs to major frameworks for native AMP support
5. **Cloud waitlist** — day-one landing page for hosted AgentMesh

### License
Apache 2.0 — permissive enough for enterprise adoption, protects the name.

### Monetization (post-traction)
- **AgentMesh Cloud**: hosted daemon + dashboard, team sharing, alerting, longer retention
- **AgentMesh Enterprise**: SSO, RBAC, on-prem, SLA
- Follows the Grafana / PostHog / Langfuse open-core model

---

## Implementation Plan (v0.1 — 4 weeks)

### Week 1: Foundation
- [ ] AMP protocol spec (this doc → `docs/amp-spec.md`)
- [ ] Python SDK core (`@mesh.agent` decorator, Unix socket emitter)
- [ ] Daemon skeleton (FastAPI, SQLite schema, WebSocket)

### Week 2: Dashboard + Claude Adapter
- [ ] Dashboard live graph view (D3 + WebSocket)
- [ ] Claude Agent SDK adapter
- [ ] LangGraph adapter
- [ ] End-to-end demo with 3+ agents

### Week 3: Polish + More Adapters
- [ ] Timeline view
- [ ] OpenAI Agents SDK adapter
- [ ] TypeScript SDK
- [ ] `agentmesh up` CLI command

### Week 4: Launch Prep
- [ ] Replay view
- [ ] README GIF + demo video
- [ ] Landing page (`agentmesh.dev`)
- [ ] HN launch post

---

## Open Questions

1. Should the Daemon be Python (FastAPI) or Go for lower resource usage?
2. For the dashboard graph — D3 force-directed or a purpose-built graph lib (React Flow)?
3. First framework integration priority: Claude SDK or LangGraph? (Claude = personal use, LangGraph = most enterprise users)
4. Naming: `agentmesh` or something else? Check npm + PyPI availability.

---

## References

- [State of AI Agents — LangChain 2025](https://www.langchain.com/state-of-agent-engineering)
- [The Multi-Agent Trap — Towards Data Science](https://towardsdatascience.com/the-multi-agent-trap/)
- [Model Context Protocol](https://modelcontextprotocol.io) — prior art for open agent protocols
- [OpenTelemetry](https://opentelemetry.io) — inspiration for SDK + collector architecture
