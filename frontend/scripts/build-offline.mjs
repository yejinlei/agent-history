// 离线演示版构建脚本：注入 VITE_BASE=VITE_OFFLINE=1 环境变量后调 Vite build。
// Windows 命令行不支持 `VAR=x cmd` 语法，故写脚本而非依赖 shell 注入。
import { spawnSync } from "node:child_process";

const ENV = {
  ...process.env,
  VITE_BASE: "./",       // 相对路径，GHP 挂在 /repo-name/ 下也能加载
  VITE_OFFLINE: "1",     // 前端 api.ts 读取这个开关切到 /demo/*.json
};

// 1) TypeScript 类型检查（等价 npm run build 的前半段）
const tsc = spawnSync("npx", ["tsc", "-b"], {
  env: ENV,
  stdio: "inherit",
  shell: process.platform === "win32",
});
if (tsc.status !== 0) process.exit(tsc.status ?? 1);

// 2) Vite build（--mode offline 会加载 .env.offline）
const vite = spawnSync("npx", ["vite", "build", "--mode", "offline"], {
  env: ENV,
  stdio: "inherit",
  shell: process.platform === "win32",
});
process.exit(vite.status ?? 1);
