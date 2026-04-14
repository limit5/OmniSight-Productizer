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
    include: [
      "test/**/*.test.ts",
      "test/**/*.test.tsx",
    ],
    // Exclude the Python suite — vitest picks up backend/ otherwise because
    // we don't scope `include` above until rootDir matches.
    exclude: ["backend/**", "node_modules/**", ".next/**"],
    css: false,
    coverage: {
      // N7 (audit fix): v8 provider avoids the jest-style babel
      // instrumentation cost. Report as text + lcov so CI can upload.
      provider: "v8",
      reporter: ["text", "lcov", "html"],
      reportsDirectory: "./coverage",
      // Coverage is scoped to the Phase 48 Autonomous-Decision surface —
      // the three new components plus the shared SSE manager segment in
      // lib/api.ts. Extending scope will be done as more of lib/api.ts
      // gets component coverage; scoping here keeps thresholds meaningful
      // rather than perpetually red because of un-touched legacy helpers.
      include: [
        "components/omnisight/mode-selector.tsx",
        "components/omnisight/decision-dashboard.tsx",
        "components/omnisight/budget-strategy-panel.tsx",
      ],
      // Tuned to actual numbers from the current suite — statements/
      // functions/lines comfortably above 85 %; branches clear 70 %.
      // CI can bump these once error-path branches are filled in.
      thresholds: {
        lines: 85,
        statements: 85,
        functions: 85,
        branches: 70,
      },
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./"),
    },
  },
})
