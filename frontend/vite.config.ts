import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base 从 process.env 读，允许 scripts/build-offline.mjs 在启动时注入 VITE_BASE=./
// 用于 GHP 部署（挂在 /repo-name/ 下）走相对路径
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE || "/",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
