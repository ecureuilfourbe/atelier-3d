import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Construit vers dist/ ; servi par le backend sur la même origine (aucun CDN à l'exécution).
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 2000 },
  server: { proxy: { "/api": "http://127.0.0.1:8765", "/health": "http://127.0.0.1:8765" } },
});
