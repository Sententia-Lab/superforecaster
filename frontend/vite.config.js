import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies API routes to the FastAPI backend (port 8099, matching
// .claude/launch.json). In production there is no proxy — FastAPI serves dist/.
// VITE_API_PROXY_TARGET overrides this — the docker-compose "frontend" dev service
// sets it to http://api:8000 since containers can't reach each other via localhost.
const API = process.env.VITE_API_PROXY_TARGET || "http://localhost:8099";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    proxy: Object.fromEntries(
      ["/runs", "/questions", "/forecasts", "/calibration", "/config", "/healthz", "/admin"].map(
        (p) => [p, { target: API, changeOrigin: true }],
      ),
    ),
  },
});
