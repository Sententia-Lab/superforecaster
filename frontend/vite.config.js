import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies API routes to the FastAPI backend (port 8099, matching
// .claude/launch.json). In production there is no proxy — FastAPI serves dist/.
const API = "http://localhost:8099";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      ["/runs", "/questions", "/forecasts", "/calibration", "/config", "/healthz", "/admin"].map(
        (p) => [p, { target: API, changeOrigin: true }],
      ),
    ),
  },
});
