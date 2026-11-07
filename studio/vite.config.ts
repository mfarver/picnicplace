/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// During development the Studio runs on Vite's dev server and proxies the API to a
// picnicd running on its default port.
const CORE_URL = "http://127.0.0.1:7420";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: CORE_URL, ws: true },
    },
  },
  test: {
    environment: "node",
  },
});
