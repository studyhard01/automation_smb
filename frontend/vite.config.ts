import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "node:path";

const backendTarget = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8010";

export default defineConfig(({ command }) => ({
  // 개발 서버는 /login·/register·/user를 직접 제공하고, 배포 자산은 기존 /playground/ 경로를 유지한다.
  base: command === "build" ? "/playground/" : "/",
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
    strictPort: false,
    proxy: {
      "/api": backendTarget,
      "/health": backendTarget,
    },
  },
}));
