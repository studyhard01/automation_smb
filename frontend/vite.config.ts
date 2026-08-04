import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "node:path";

const backendTarget = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8011";

export default defineConfig({
  base: "/playground/",
  plugins: [vue()],
  resolve: {
    alias: {
      "@": path.resolve("src"),
    },
  },
  build: {
    outDir: "../backend/src/smb_finder/web",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": backendTarget,
      "/health": backendTarget,
    },
  },
});
