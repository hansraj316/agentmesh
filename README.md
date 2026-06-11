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

For a live browser view, see [`agentmesh serve`](#serve-the-live-board) below.

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

## Trace a run

Nested `@mesh.agent` calls are linked automatically: every invocation gets a
`span_id` and records its caller as `parent_span_id`
([AMP v0.2](docs/amp-spec.md), async-safe via contextvars). Render any run as
a span tree:

```bash
agentmesh trace my-run-42
```

```
run my-run-42 — 3 spans, 1 failed, total 4.2s
└─ orchestrator ✓ 4.1s
   ├─ researcher ✓ 2.0s
   └─ writer ✗ 1.3s — ValueError: bad draft
```

`✓` ended ok, `✗` failed (with the first line of the error), `…` still
running. v0.1 events without span ids still work — they render as a flat
tree, paired by agent and event order. `--db PATH` points at a different
event database.

## Compare runs

Did yesterday's deploy make the pipeline slower? Diff two runs of the same
workflow to catch regressions:

```bash
agentmesh diff run-baseline run-after-deploy
```

```
# Run diff: run-baseline → run-after-deploy

run-baseline: 3 spans, 0 failed, total 4.2s | run-after-deploy: 3 spans, 1 failed, total 6.8s | wall-clock Δ +2.6s

| Agent path | run-baseline | run-after-deploy | Δ | %Δ | Status |  |
| --- | --- | --- | --- | --- | --- | --- |
| orchestrator | 4.1s | 6.7s | +2.6s | +63.4% | ok → ok | ⚠️ |
| orchestrator/researcher | 2.0s | 4.4s | +2.4s | +120.0% | ok → ok | ⚠️ |
| orchestrator/writer | 1.3s | 1.9s | +0.6s | +46.2% | ok → failed | ⚠️ |
```

Spans are aligned by structural path (the root-relative chain of agent
names); when the same agent runs twice under one parent, occurrences match
in order. Each aligned pair shows both durations, the absolute and percent
delta, and the status transition — rows that got slower beyond the
threshold or flipped `ok → failed` are marked `⚠️`, rows that sped up or
recovered are marked `✅`. Spans present in only one run get their own
"Only in …" sections, and when nothing crosses the threshold the diff says
so instead. `--threshold PCT` tunes what counts as significant (default
20%), `--db PATH` points at a different event database, and the exit code
is 0 whenever both runs exist — the diff is informational, not a gate.

## Alerting

Declare alert rules in `~/.agentmesh/alerts.json` (override with the
`AGENTMESH_ALERTS` environment variable or `--rules PATH`). Two rule types
exist today: `silent` fires when an agent's latest event is older than
`max_silence_minutes`, and `failure_streak` fires when its
consecutive-failure streak reaches `min_streak`. `"agent": "*"` checks every
agent.

```json
[
  {"name": "fleet-quiet", "type": "silent", "agent": "*", "max_silence_minutes": 120},
  {"name": "deploy-broken", "type": "failure_streak", "agent": "openclaw/deploy", "min_streak": 2}
]
```

Evaluate the rules against the event store:

```bash
agentmesh alerts
```

```
| Rule | Severity | Agent | Message |
|------|----------|-------|---------|
| fleet-quiet | 🟡 warning | agentmesh/daily-ci | agent agentmesh/daily-ci has been silent for 184m (max 120m) |
| deploy-broken | 🔴 critical | openclaw/deploy | agent openclaw/deploy has failed 2 runs in a row (min 2) |
```

`--webhook URL` POSTs one JSON payload per fired alert —
`{"source": "agentmesh", "severity", "title", "body", "details", "fired_at"}` —
ready for Slack bridges, PagerDuty, or your own receiver. `--exit-code` makes
the command exit 1 when anything fired, so one cron line (or CI step) can
re-ingest and alert in one go:

```bash
agentmesh ingest-gha hansraj316 --repo agentmesh --limit 50 && agentmesh alerts --exit-code
```

`--db PATH` points at a different event database, as with `trace`.

## Serve the live board

`agentmesh serve` turns the fleet workflow into a live dashboard: it serves
the HTML board plus a read-only JSON API from the event store, using only the
Python standard library (no daemon dependencies).

```bash
agentmesh ingest-gha hansraj316 --repo agentmesh --limit 50
agentmesh serve                 # agentmesh board at http://127.0.0.1:7777/
```

Open `http://127.0.0.1:7777/` in a browser — the board page auto-refreshes
every 5 seconds, so new events show up as they are ingested. The server binds
to **127.0.0.1 only** (localhost) by design: the event store can contain
private repo names and error details, so remote exposure is opt-in via your
own reverse proxy. `--port N` changes the port, `--db PATH` and
`--rules PATH` override the event database and alert rules file, and Ctrl-C
stops it cleanly.

| Route | Returns |
|-------|---------|
| `/` | HTML board (auto-refreshes every 5s) |
| `/api/agents` | JSON list of agent summaries (one per agent) |
| `/api/runs/<run_id>` | JSON span tree of one run (404 + JSON error if unknown) |
| `/api/alerts` | JSON webhook payloads for fired alert rules (`[]` with no rules) |

```bash
curl -s http://127.0.0.1:7777/api/agents | python3 -m json.tool
curl -s http://127.0.0.1:7777/api/runs/my-run-42 | python3 -m json.tool
```

## TypeScript SDK (experimental)

[`sdk-ts/`](sdk-ts/) is a dependency-free TypeScript mirror of the Python SDK:
wrap any sync or async function with `mesh.agent(name, fn)` and it emits
`agent_start`, `agent_end` (with `duration_seconds`), and `agent_error`
(re-thrown) AMP v0.2 events, with span nesting propagated through async code
via `AsyncLocalStorage`. Events are written as JSON Lines:

```ts
import { Mesh, JsonlFileSink } from "agentmesh-sdk";

const mesh = new Mesh(new JsonlFileSink("events.jsonl"));

const writer = mesh.agent("writer", async (task: string) => draft(task));

await mesh.run("my-run-42", () => writer("intro"));
```

Then bridge the JSONL file into the Python event store — the import is
idempotent (duplicate event ids are skipped), so it is safe to re-run:

```bash
agentmesh import-jsonl events.jsonl && agentmesh board
agentmesh trace my-run-42   # TS spans render in the same trace tree
```

`--db PATH` overrides the target database. To develop the SDK:
`cd sdk-ts && npm install && npx tsc --noEmit && npx vitest run`.

## Maintenance

Long-running event stores grow forever by default; `stats` and `prune` keep
them healthy:

```bash
agentmesh stats                            # counts per type, agents, runs, ts range, db size
agentmesh prune --older-than 30d           # delete events older than 30 days, then VACUUM
agentmesh prune --older-than 30d --dry-run # report what would be deleted, change nothing
```

Pruning never splits a run: if **any** event of a run is newer than the
cutoff, **all** events of that run are kept — only runs whose every event is
older than the cutoff are deleted, so traces always stay complete.
`--older-than` accepts `30d` or a bare day count like `30`, and `--db PATH`
overrides the event database on both commands.

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
- [x] Serve daemon (`agentmesh serve` — live board + read-only JSON API, stdlib-only)
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
- [x] Alert rules (`agentmesh alerts` — silence + failure-streak rules, webhook output)

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
