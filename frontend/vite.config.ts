import react from "@vitejs/plugin-react";
// vitest 4 no longer merges its `test` block into vite's config type, so the
// config helper has to come from vitest rather than vite.
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Same-origin in dev: proxy API calls to the backend so cookies/CSRF just work.
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
    css: false,
  },
});
