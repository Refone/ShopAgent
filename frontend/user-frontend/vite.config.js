import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const envDir = fileURLToPath(new URL("../..", import.meta.url));
const envPrefix = [
  "VITE_",
  "CUSTOMER_SERVICE_",
  "ECOMMERCE_SERVICE_",
  "AI_SERVICE_",
];

export default defineConfig({
  plugins: [vue()],
  envDir,
  envPrefix,
  server: {
    port: 5173,
    strictPort: true,
  },
});
