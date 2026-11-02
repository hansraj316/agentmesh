/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is committed to agentmesh/static/app/ and served by
// `agentmesh serve` at /app/, so users never need Node to run AgentMesh.
const outDir = "../agentmesh/static/app";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    // Once the daemon is running (AGENTMESH_DEV=1 agentmesh serve), the dev
    // server forwards API calls to it. `npm run dev:mock` needs no daemon.
    proxy: {
      "/api": { target: "http://127.0.0.1:7777", changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
