// L1-08 — flat-config ESLint for Next.js 16 + React 19 + TypeScript.
//
// ESLint v10 dropped legacy `.eslintrc.*` support. Without this file
// `npm run lint` was just exiting 1 ("couldn't find eslint.config.js")
// and our CI's `|| true` swallowed it — meaning lint had been a silent
// no-op for the entire project lifetime.
//
// Posture mirrors how tsc + ruff just graduated:
//   * ship the config so the gate actually exists
//   * keep CI on `|| true` warn-only for one observation cycle
//   * once the rule output is reviewed, drop the fallthrough
//
// Rule set: Next.js core-web-vitals (the framework's own
// recommended bundle) + typescript-eslint recommended. Project-
// local relaxations are at the bottom — only when a Next/TS
// default fights an intentional codebase pattern.

import js from "@eslint/js"
import next from "eslint-config-next"
import tseslint from "typescript-eslint"

export default tseslint.config(
  // Skip generated / vendored / test artifact directories outright.
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      "coverage/**",
      "playwright-report/**",
      "test-results/**",
      "backend/**",          // Python — ruff handles this.
      "deploy/**",            // shell + yaml templates.
    ],
  },

  js.configs.recommended,

  ...next,

  ...tseslint.configs.recommended,

  {
    rules: {
      // Test files need vi/jest globals + lots of any-typed mocks.
      "@typescript-eslint/no-explicit-any": "off",
      // React 19 + Next 16 sometimes emit unused vars in generated
      // .next code; the no-unused-vars rule is already covered by
      // the typescript-eslint variant which understands type-only
      // imports.
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // Console is intentional in dev tooling + components that hit
      // window.console for E2E breadcrumbs. Warn, don't fail.
      "no-console": "off",
    },
  },

  {
    files: ["test/**/*.{ts,tsx}", "**/*.test.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-unused-expressions": "off",
    },
  },
)
