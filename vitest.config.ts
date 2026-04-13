/// <reference types="vitest" />
import { defineConfig } from "vitest/config"
import path from "node:path"

// Phase 49A — vitest config for the Next.js frontend.
// - jsdom environment so React components can mount without a browser.
// - setupFiles wires @testing-library/jest-dom matchers + polyfills
//   (EventSource, ResizeObserver, etc.) required by our components.
// - Path alias @/ matches the Next.js tsconfig so imports resolve the
//   same way in tests as in the app.
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.ts", "test/**/*.test.tsx"],
    // Exclude the Python suite — vitest picks up backend/ otherwise because
    // we don't scope `include` above until rootDir matches.
    exclude: ["backend/**", "node_modules/**", ".next/**"],
    css: false,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./"),
    },
  },
})
