import { useEffect, useMemo, useRef, useState } from "react";
import type { Backend } from "./backend";
import { foldInto, sortHandoffs, type HandoffMap } from "./fold";
import type { ConnectionState, Handoff, StreamEvent } from "./types";

export interface FleetState {
  handoffs: Handoff[];
  connection: ConnectionState;
  /** Bumps on every batch of new events; views refetch when it changes. */
  version: number;
  /** Run id of the newest event per run, newest run first. */
  runs: string[];
  /** Per run, the version at which it last saw an event. */
  runVersions: ReadonlyMap<string, number>;
}

/** Subscribe to the backend and fold its events into handoffs. */
export function useFleet(backend: Backend): FleetState {
  const [map, setMap] = useState<HandoffMap>(() => new Map());
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [version, setVersion] = useState(0);
  const runOrder = useRef<Map<string, number>>(new Map());
  const [runVersions, setRunVersions] = useState<ReadonlyMap<string, number>>(() => new Map());

  useEffect(() => {
    let v = 0;
    const onEvents = (events: StreamEvent[]) => {
      v += 1;
      const at = v;
      for (const e of events) {
        runOrder.current.delete(e.run_id);
        runOrder.current.set(e.run_id, e.id);
      }
      setMap((prev) => foldInto(prev, events));
      setRunVersions((prev) => {
        const next = new Map(prev);
        for (const e of events) next.set(e.run_id, at);
        return next;
      });
      setVersion(at);
    };
    return backend.subscribe(onEvents, setConnection);
  }, [backend]);

  const handoffs = useMemo(() => sortHandoffs(map.values()), [map]);
  const runs = useMemo(
    () => Array.from(runOrder.current.keys()).reverse(),
    [runVersions],
  );
  return { handoffs, connection, version, runs, runVersions };
}

/** A clock that ticks every second, paused when nothing needs it. */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
  return now;
}
