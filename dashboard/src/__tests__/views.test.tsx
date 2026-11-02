import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { fold } from "../lib/fold";
import type { StreamEvent } from "../lib/types";
import { MockFleet, summarizeAgents } from "../mocks/fleet";
import { scriptRun } from "../mocks/script";
import { HandoffsView } from "../views/HandoffsView";

const NOW = Date.parse("2026-11-02T10:00:30.000Z");

function stageshowEvents(): StreamEvent[] {
  const base = (id: number, ts: string, payload: Record<string, unknown>): StreamEvent => ({
    id,
    event_id: `e${id}`,
    run_id: "fleet-0007",
    agent: String(payload.from),
    type: "message",
    ts,
    payload: { kind: "handoff", ...payload },
  });
  return [
    base(1, "2026-11-02T09:59:00.000Z", { task_id: "t-1", task: "Review PR #41", from: "coder", to: "reviewer", status: "running" }),
    base(2, "2026-11-02T09:59:41.000Z", { task_id: "t-1", task: "Review PR #41", from: "coder", to: "reviewer", status: "done", duration_ms: 41000 }),
    base(3, "2026-11-02T09:59:50.000Z", { task_id: "t-2", task: "Fix flaky webhook test", from: "planner", to: "coder", status: "running" }),
    base(4, "2026-11-02T09:59:58.000Z", { task_id: "t-2", task: "Fix flaky webhook test", from: "planner", to: "coder", status: "failed", error: "TimeoutError: GitHub API timed out after 30s" }),
    base(5, "2026-11-02T10:00:07.000Z", { task_id: "t-3", task: "Add retry to GitHub sync", from: "planner", to: "coder", status: "running" }),
  ];
}

describe("HandoffsView", () => {
  it("renders the stageshow: newest first, Running with a live timer, Done and Failed", () => {
    const handoffs = fold(stageshowEvents());
    const { rerender } = render(<HandoffsView handoffs={handoffs} now={NOW} />);
    const rows = screen.getAllByRole("button");
    expect(rows.map((r) => within(r).getByText(/./, { selector: ".row__title" }).textContent)).toEqual([
      "Add retry to GitHub sync",
      "Fix flaky webhook test",
      "Review PR #41",
    ]);
    expect(within(rows[0]).getByText("Running")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("timer")).toHaveTextContent("23s");
    expect(within(rows[1]).getByText("Failed")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/GitHub API timed out/)).toBeInTheDocument();
    expect(within(rows[2]).getByText("Done")).toBeInTheDocument();
    expect(within(rows[2]).getByTestId("timer")).toHaveTextContent("41s");

    // The timer ticks while the handoff runs.
    rerender(<HandoffsView handoffs={handoffs} now={NOW + 5000} />);
    expect(within(screen.getAllByRole("button")[0]).getByTestId("timer")).toHaveTextContent("28s");
  });

  it("slides in only the rows that arrive after the first render", () => {
    const events = stageshowEvents();
    const { rerender, container } = render(<HandoffsView handoffs={fold(events.slice(0, 4))} now={NOW} />);
    expect(container.querySelectorAll(".row-wrap--enter")).toHaveLength(0);
    rerender(<HandoffsView handoffs={fold(events)} now={NOW} />);
    const entering = container.querySelectorAll(".row-wrap--enter");
    expect(entering).toHaveLength(1);
    expect(entering[0]).toHaveTextContent("Add retry to GitHub sync");
  });

  it("shows a hint when there are no handoffs", () => {
    render(<HandoffsView handoffs={[]} now={NOW} />);
    expect(screen.getByText("No handoffs yet")).toBeInTheDocument();
  });
});

describe("mock fleet", () => {
  it("scripts planner to coder to reviewer handoffs that fold cleanly", () => {
    const steps = scriptRun({ index: 0, seed: 7, speed: 0, failureRate: 0 });
    const events = steps.map((s, i) => ({
      id: i + 1,
      event_id: `e${i}`,
      run_id: "fleet-0001",
      ts: new Date(NOW + s.atMs).toISOString(),
      agent: s.agent,
      type: s.type,
      payload: s.payload,
    }));
    const pairs = fold(events).map((h) => `${h.from}->${h.to}:${h.status}`);
    expect(pairs).toHaveLength(4);
    expect(pairs.filter((p) => p === "planner->coder:done")).toHaveLength(2);
    expect(pairs.filter((p) => p === "coder->reviewer:done")).toHaveLength(2);
    expect(summarizeAgents(events).map((a) => [a.agent, a.status])).toEqual([
      ["coder", "ok"],
      ["planner", "ok"],
      ["reviewer", "ok"],
    ]);
  });

  it("produces a failed handoff when the coder fails", () => {
    const steps = scriptRun({ index: 0, seed: 7, speed: 0, failureRate: 1 });
    const events = steps.map((s, i) => ({
      id: i + 1,
      event_id: `e${i}`,
      run_id: "fleet-0001",
      ts: new Date(NOW + s.atMs).toISOString(),
      agent: s.agent,
      type: s.type,
      payload: s.payload,
    }));
    const handoffs = fold(events);
    expect(handoffs.every((h) => h.status === "failed")).toBe(true);
    expect(handoffs[0].error).toMatch(/TimeoutError/);
  });
});

describe("App on the mock fleet", () => {
  it("lists handoffs, opens a run's span tree, and shows the agents board", async () => {
    const fleet = new MockFleet({ seed: 7, history: 2, liveRuns: 0, now: () => NOW });
    render(<App backend={fleet} />);
    expect(await screen.findByText("Mock fleet")).toBeInTheDocument();
    const rows = await screen.findAllByRole("button", { name: /Open run fleet-/ });
    expect(rows.length).toBeGreaterThan(0);

    await act(async () => {
      fireEvent.click(rows[0]);
    });
    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("Span tree")).toBeInTheDocument();
    expect(await within(drawer).findAllByText("planner")).not.toHaveLength(0);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Agents" }));
    const table = await screen.findByRole("table", {}, { timeout: 3000 });
    expect(within(table).getByText("reviewer")).toBeInTheDocument();
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });
});
