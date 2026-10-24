/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// No inline scripts/styles in the build: the CloudFront CSP is
// `script-src 'self'; style-src 'self'` (design §8). `modulePreload.polyfill`
// is disabled because Vite would otherwise inject an inline polyfill script.
export default defineConfig({
  plugins: [react()],
  build: {
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
