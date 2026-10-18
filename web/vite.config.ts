import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the backend (kst run, default :8787). `npm run build` → web/dist, served by FastAPI.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 800 },
});
