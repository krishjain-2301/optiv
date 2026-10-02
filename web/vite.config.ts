import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the dashboard with hot reload and forwards /api to the Python server
// (python app.py --no-browser). `npm run build` writes web/dist, which the Python server hosts.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist", emptyOutDir: true },
});
