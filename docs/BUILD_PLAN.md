# AgentMesh end to end build plan

Status: proposed, waiting on Hansraj's review (2026-11-02)
Owner: Hansraj. Each piece below is built by its own Claude thread, then one integration step ties them together.

## Where AgentMesh stands today

AgentMesh already has a solid core on `main`: the AMP v0.3 event spec, a Python SDK (`@mesh.agent`, `mesh.run`, `mesh.record_usage`), a SQLite event store, 17 CLI commands (`tail`, `board`, `trace`, `diff`, `costs`, `flaky`, `latency`, `alerts`, `export-otlp`, `demo`, and more), a stdlib `agentmesh serve` daemon with a JSON API and `POST /api/events`, and an experimental TypeScript SDK. 401 tests pass. There are no runtime dependencies.

What is missing is the thing the portfolio stageshow scene shows: a live **Handoffs** view where a Planner hands "Add retry to GitHub sync" to a Coder, the row appears at the top of the fleet trace as Running with a ticking timer, then flips to Done. Today there is no handoff concept in AMP, no live push from the daemon, no browser dashboard beyond an auto-refreshing HTML table, and no real multi-agent workload to watch. The README roadmap lists the same gaps: "Real-time Dashboard (React + WebSocket)" and `agentmesh up`.

## What "end to end working" means

One command on a fresh clone shows a real multi-agent fleet working and AgentMesh watching it live:

```bash
git clone https://github.com/hansraj316/agentmesh && cd agentmesh
pip install -e .
agentmesh up            # starts the daemon, runs the indian-avengers fleet, opens the dashboard
```

The browser opens `http://127.0.0.1:7777/app/` and shows:

1. **Handoffs** (the stageshow view): Planner hands a task to Coder, Coder hands to Reviewer; each row appears live as Running with a timer and settles to Done or Failed.
2. **Agents**: the existing board data (status, streak, cost, p95) updating live.
3. **Run trace**: click a handoff to see that run's span tree, usage, and annotations.

It works offline with no API key (a deterministic scripted model). Setting `ANTHROPIC_API_KEY` and `--llm claude` makes the agents call Claude for real. The existing CLI keeps working on the same data (`agentmesh handoffs`, `trace`, `board`, `costs`).

Definition of done: `agentmesh up` works on macOS and Linux with Python 3.9+, no Node needed to run it, CI is green, and a new end to end test drives the whole path.

## Stack (matching what is in the repo)

- Python 3.9+, stdlib only at runtime (keeps `dependencies = []`).
- Live push over **Server-Sent Events** on the existing `ThreadingHTTPServer`. SSE is one-way, which is all the dashboard needs, and it needs no dependency. WebSocket stays on the roadmap; the portfolio card's "WebSocket" label can be updated or kept as the target.
- Dashboard: React 18 + TypeScript + Vite in `dashboard/`, styled after the stageshow scene (dark card, one green accent). The built bundle is committed to `agentmesh/static/app/` so users never need Node.
- Tests: pytest (Python), vitest (dashboard), ruff and black as today.

## The handoff contract (frozen before the threads start)

Every piece builds against this. It is an additive AMP v0.4 change: a handoff is a `message` event, so every v0.3 consumer still accepts it.

A handoff produces two events with the same `task_id`. The event's `agent` is the sender.

```json
{"type": "message", "agent": "planner", "run_id": "fleet-0007",
 "payload": {"kind": "handoff", "task_id": "t-3f2a", "task": "Add retry to GitHub sync",
             "from": "planner", "to": "coder", "status": "running"}}

{"type": "message", "agent": "planner", "run_id": "fleet-0007",
 "payload": {"kind": "handoff", "task_id": "t-3f2a", "task": "Add retry to GitHub sync",
             "from": "planner", "to": "coder", "status": "done", "duration_ms": 23140}}
```

- `status` is `running` on open, then `done` or `failed` on close. `failed` may carry `error` (first line only).
- `task_id` is unique within a run. `span_id` and `parent_span_id` are included when the handoff happens inside a decorated agent.
- A folded handoff (what every consumer shows) has: `task_id, run_id, task, from, to, status, started_at, ended_at, duration_seconds`. Still-open handoffs have `ended_at: null`.

Live stream contract (`GET /api/stream?after=<id>`): `text/event-stream`, one `data:` line per stored event, the JSON being the AMP event plus `"id"` (the store's row id). A comment line `: ping` every 15 seconds keeps the connection open. Clients reconnect with the last `id` they saw. `GET /api/events?after=<id>&limit=<n>` returns the same objects as a JSON array for the first page.

## The pieces

Five threads can start at the same time. They never edit the same file. Only P1 is a code dependency, and only for P4; P1 is small and should merge first. P2 and P3 work from raw events and need nothing from P1.

| # | Piece | Branch | Owns (files and folders) | Depends on |
|---|-------|--------|--------------------------|------------|
| P1 | Handoff protocol and SDK | `build/p1-handoffs` | `docs/amp-spec.md`, `agentmesh/sdk.py`, `agentmesh/handoffs.py` (new), `tests/test_handoffs.py` (new) | nothing |
| P2 | Live daemon API | `build/p2-live-api` | `agentmesh/server.py`, `tests/test_server.py`, `tests/test_stream.py` (new) | nothing |
| P3 | Live dashboard | `build/p3-dashboard` | `dashboard/` (new), `agentmesh/static/app/` (build output, new) | nothing |
| P4 | Example fleet | `build/p4-fleet` | `agentmesh/fleet/` (new package), `tests/test_fleet.py` (new) | P1 API only |
| P5 | Integration (last) | `build/p5-integrate` | `agentmesh/cli.py`, `README.md`, `pyproject.toml`, `.github/workflows/daily-ci.yml`, `tests/test_cli.py`, `tests/test_e2e.py` (new) | P1 to P4 merged |

### P1. Handoff protocol and SDK

Builds: the AMP v0.4 handoff section in `docs/amp-spec.md`; `mesh.handoff(task, to, task_id=None, from_=None)` in `sdk.py` as a sync and async context manager that emits `running` on enter, `done` on clean exit, `failed` on exception (re-raised), with `from_` defaulting to the current decorated agent; `agentmesh/handoffs.py` with `fold(events) -> list[Handoff]`, `list_handoffs(store, run_id=None, since=None, limit=50)`, a markdown table renderer, and `add_cli(subparsers)` that registers `agentmesh handoffs [--run ID] [--follow] [--db PATH]` (P5 wires it into `cli.py`).

Interface given to others: the payload above, `mesh.handoff(...)`, `handoffs.fold`, `handoffs.add_cli`.

Tested by: `tests/test_handoffs.py` covering open and close pairing, failed with error, nesting inside agents, async, unmatched opens shown as running, v0.3 events untouched, and the table output.

### P2. Live daemon API

Builds in `server.py`: `GET /api/stream` (SSE, polling `EventStore.events_after` every 250 ms, `after` query and `Last-Event-ID` header both honored, ping comment every 15 s, clean close on client disconnect); `GET /api/events?after=&limit=` (JSON page, default limit 200, max 1000); `GET /api/runs` (recent runs with start, end, status, span count); static hosting of `agentmesh/static/app/` at `/app/` with `index.html` fallback for client routes and a clear 404 message when the bundle is missing; `Access-Control-Allow-Origin: http://localhost:5173` only when `AGENTMESH_DEV=1` (Vite dev server). Keeps the existing localhost-only binding and every existing route unchanged.

Interface given to others: the stream and events contract above, `/api/runs`, `/app/`. Existing `/api/agents` and `/api/runs/<run_id>` stay as they are.

Tested by: `tests/test_stream.py` (start the server on port 0, append events, read the SSE stream, check order, resume from `after`, ping, disconnect) and new cases in `tests/test_server.py` for `/api/events` GET, `/api/runs`, and static hosting with a temporary bundle dir.

### P3. Live dashboard

Builds a Vite + React + TypeScript app in `dashboard/`, built with `npm run build` into `agentmesh/static/app/` (committed). Views: Handoffs (newest first, rows open in like the stageshow, Running with spinner and live timer, Done in green, Failed in red), Agents (from `/api/agents`, refreshed when new events arrive), and a Run drawer (from `/api/runs/<run_id>`). A small client folds stream events into handoffs using the same rules as P1's `fold`. Respects reduced motion.

Develops against a mock stream (`dashboard/src/mocks/`, a scripted fleet replaying planner to coder to reviewer) so it needs nothing from P2 to start; `npm run dev` proxies `/api` to `127.0.0.1:7777` once P2 lands.

Tested by: vitest for the fold reducer (same cases as P1), the stream client's reconnect logic, and a render test of the Handoffs view from mock events; `npm run build` must succeed and produce `agentmesh/static/app/index.html`.

### P4. Example fleet ("indian-avengers")

Builds `agentmesh/fleet/`: three decorated agents, Planner, Coder and Reviewer, working through a small backlog of realistic repo tasks ("Add retry to GitHub sync", "Review PR #41", and so on). Planner breaks a goal into tasks and hands each to Coder with `mesh.handoff`; Coder hands its result to Reviewer; Reviewer approves or sends it back once. Records usage on every step so cost and latency views have data. A pluggable model layer: `ScriptedLLM` (default, deterministic with a seed, realistic delays of a few seconds, an occasional failure so Failed rows and flaky stats show up) and `ClaudeLLM` (opt in with `ANTHROPIC_API_KEY`, model from `AGENTMESH_FLEET_MODEL`, called through the stdlib `urllib`, so still no runtime dependency).

Interface given to others: `fleet.run_once(store, llm=None, seed=None) -> run_id` and `fleet.run_forever(store, stop: threading.Event, interval_s=3.0, llm=None)`, plus `add_cli(subparsers)` registering `agentmesh fleet [--once] [--llm scripted|claude] [--db PATH]`.

Tested by: `tests/test_fleet.py` with the scripted model and delays set to zero: one run produces the expected handoff pairs and spans, a seeded failure produces a `failed` handoff, `run_forever` stops cleanly, and `ClaudeLLM` is tested against a fake HTTP response (no network in CI).

### P5. Integration (starts after P1 to P4 merge)

Wires `handoffs.add_cli` and `fleet.add_cli` into `cli.py` and adds `agentmesh up [--port 7777] [--db PATH] [--llm scripted|claude] [--no-browser]`: starts the daemon in a thread, runs `fleet.run_forever` in another, opens the browser at `/app/`, and shuts both down on Ctrl-C. Updates `pyproject.toml` (find packages so `agentmesh.fleet` ships, include `agentmesh/static/app/**` as package data). Adds a "Run it end to end" section to the README and ticks the roadmap items; this must be rebased on the separate README thread that adds the stageshow scene, so the two README changes merge cleanly. Adds a `dashboard` CI job (`npm ci`, `npm test`, `npm run build`, then fail if `agentmesh/static/app/` differs from what is committed).

Tested by: `tests/test_e2e.py`: start `up` with port 0, the scripted model at zero delay and no browser; read `/api/stream` until a handoff closes as done; check `/api/events`, `/api/runs/<id>` and `/app/` all answer; then `agentmesh handoffs --db` prints the same task. Plus a manual check on a clean clone: `pip install -e . && agentmesh up`, with a screen recording or screenshot of the Handoffs view for the PR.

## Order and rules for the threads

1. Merge this plan first so every thread reads the same contract.
2. Start P1 to P4 together, each on its own branch from `main`, each with its own draft PR.
3. P1 merges first (smallest). P4 rebases on it before review; P2 and P3 do not need to wait.
4. A thread that needs a change outside its own files writes it up on its PR and the change goes to the owning thread or to P5. No thread edits another's files.
5. Each PR keeps CI green (ruff, black, pytest, and for P3 the dashboard build) and stays draft until Hansraj reviews it. Nothing goes to `main` without his merge.
6. P5 starts once P1 to P4 are merged and opens the last draft PR with the demo recording.

## Open questions for Hansraj

- SSE now with WebSocket later, as above, or WebSocket from day one (adds a dependency or a hand-rolled handshake)?
- Commit the built dashboard bundle (recommended, so `pip install` is enough), or require Node to build it?
- Should the portfolio card's stack label change from "WebSocket" to "SSE" once this lands?
