import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

const envDir = fileURLToPath(new URL("../..", import.meta.url));
const envPrefix = [
  "VITE_",
  "CUSTOMER_SERVICE_",
  "ECOMMERCE_SERVICE_",
  "AI_SERVICE_",
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, envDir, ["ADMIN_FRONTEND_"]);
  const host = env.ADMIN_FRONTEND_HOST || "127.0.0.1";
  const port = Number(env.ADMIN_FRONTEND_PORT || 18101);
  // 部署在子路径时（如 nginx 反代 /shopagent/admin/）由 .env 指定；
  // 本地开发留空即根路径。build 与 preview 共用这份配置，deploy.py 无需额外参数。
  const base = env.ADMIN_FRONTEND_BASE || "/";

  return {
    base,
    plugins: [vue()],
    envDir,
    envPrefix,
    server: {
      host,
      port,
      strictPort: true,
    },
    preview: {
      host,
      port,
      strictPort: true,
    },
  };
});
