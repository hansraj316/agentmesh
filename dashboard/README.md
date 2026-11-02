# AgentMesh dashboard

The live browser dashboard for AgentMesh: **Handoffs**, **Agents** and a **Run**
drawer, styled after the stageshow scene in `docs/media/agentmesh-handoff.svg`.

The built bundle is committed to `agentmesh/static/app/`, and `agentmesh serve`
hosts it at `/app/`, so running AgentMesh never needs Node. You only need Node
(20.19+) to change the dashboard.

## Views

- **Handoffs**: newest first. A new handoff opens the list and drops in as
  Running with a spinner and a ticking timer, then settles to Done (green) or
  Failed (red, with the first line of the error). Click a row to open its run.
- **Agents**: the `/api/agents` board (status, runs, errors, streak, avg, p95,
  cost), refetched at most once a second while events arrive.
- **Run drawer**: `/api/runs/<run_id>`, with the run's handoffs, span tree,
  token and cost totals, and annotations. Esc closes it.

Animations respect `prefers-reduced-motion`.

## How it gets data

`src/lib/stream.ts` follows the live contract in `docs/BUILD_PLAN.md`:

1. page through `GET /api/events?after=<id>&limit=1000` for the first render;
2. open `GET /api/stream?after=<last id>` (Server-Sent Events, one JSON event
   per `data:` line, each with the store's `id`);
3. on any error, close and reconnect from the last id seen, backing off from
   1 s to 15 s, so nothing is missed or shown twice.

`src/lib/fold.ts` folds `message` events with `payload.kind == "handoff"` into
handoffs, with the same rules as the Python `agentmesh.handoffs.fold`.

## Develop

```bash
cd dashboard
npm ci
npm run dev:mock     # no daemon: replays a scripted planner -> coder -> reviewer fleet
npm run dev          # against a daemon: AGENTMESH_DEV=1 agentmesh serve (proxied /api)
npm test             # vitest: fold rules, stream reconnects, views
npm run build        # typecheck, then write agentmesh/static/app/
```

Commit the rebuilt `agentmesh/static/app/` with any dashboard change; CI checks
that it matches the source. The built bundle also has the mock fleet: open
`/app/?mock` to see the dashboard without any agents.

If `npm install <package>` fails with `Cannot read properties of null
(reading 'edgesOut')`, that is an npm 10 bug with vitest's peer set; run the
install with npm 11 (`npx npm@11 install ...`). `npm ci` works on npm 10.
