import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { httpBackend, type Backend } from "./lib/backend";
import { MockFleet } from "./mocks/fleet";
import "./styles.css";

// `npm run dev:mock` (or `?mock` on any URL) replays the scripted fleet in
// the browser; otherwise the dashboard talks to the daemon it is served from.
const mock =
  import.meta.env.VITE_AGENTMESH_MOCK === "1" || new URLSearchParams(window.location.search).has("mock");
const backend: Backend = mock ? new MockFleet() : httpBackend();

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <App backend={backend} />
  </StrictMode>,
);
