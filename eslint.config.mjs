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
      ".agent_workspaces/**", // cloned workspaces — lint the originals only.
    ],
  },

  js.configs.recommended,

  ...next,

  ...tseslint.configs.recommended,

  // FX.7.12 — eslint-plugin-jsx-a11y. eslint-config-next already pulls
  // the jsx-a11y plugin in for 6 rules at warn-level, but the project
  // never adopted the recommended ruleset (34 rules). Wire the plugin's
  // own flat-config recommended bundle here so all 34 rules are loaded,
  // then sort each into "error" (HTML/ARIA contract violations — always
  // invalid, worth blocking) or "warn" (behavioral refactors that need
  // human-in-the-loop — keyboard handler parity, focus management).
  // Per the project's gate-shipping pattern documented at the top of
  // this file: ship the gate now, iterate severity once the warn pile
  // has been triaged.
  // Note: eslint-config-next already registers `jsx-a11y` as a plugin
  // (with 6 warn-level rules); registering it again would error with
  // "Cannot redefine plugin". We only override / extend its rules here.
  {
    files: ["**/*.{ts,tsx,js,jsx,mjs}"],
    rules: {
      // === HTML / ARIA contract violations (error = blocking) ===
      // These flag patterns that are literally invalid markup or ARIA
      // misuse — no behavioral judgement required to fix.
      "jsx-a11y/alt-text": ["error", { elements: ["img"], img: ["Image"] }],
      "jsx-a11y/anchor-has-content": "error",
      "jsx-a11y/anchor-is-valid": "error",
      "jsx-a11y/aria-activedescendant-has-tabindex": "error",
      "jsx-a11y/aria-props": "error",
      "jsx-a11y/aria-proptypes": "error",
      "jsx-a11y/aria-role": "error",
      "jsx-a11y/aria-unsupported-elements": "error",
      "jsx-a11y/autocomplete-valid": "error",
      "jsx-a11y/heading-has-content": "error",
      "jsx-a11y/html-has-lang": "error",
      "jsx-a11y/iframe-has-title": "error",
      "jsx-a11y/img-redundant-alt": "error",
      "jsx-a11y/no-access-key": "error",
      "jsx-a11y/no-distracting-elements": "error",
      "jsx-a11y/no-redundant-roles": "error",
      "jsx-a11y/role-has-required-aria-props": "error",
      "jsx-a11y/role-supports-aria-props": "error",
      "jsx-a11y/scope": "error",
      "jsx-a11y/tabindex-no-positive": "error",
      // === Behavioral / refactor-required (warn for now) ===
      // These need design judgement (do you really want a div onClick?
      // should this be a button?) and tend to surface dozens of hits
      // across an existing UI tree. Keep at warn for one observation
      // cycle, then promote individually.
      "jsx-a11y/click-events-have-key-events": "warn",
      "jsx-a11y/interactive-supports-focus": "warn",
      "jsx-a11y/label-has-associated-control": "warn",
      "jsx-a11y/media-has-caption": "warn",
      "jsx-a11y/mouse-events-have-key-events": "warn",
      "jsx-a11y/no-autofocus": "warn",
      "jsx-a11y/no-interactive-element-to-noninteractive-role": "warn",
      "jsx-a11y/no-noninteractive-element-interactions": "warn",
      "jsx-a11y/no-noninteractive-element-to-interactive-role": "warn",
      "jsx-a11y/no-noninteractive-tabindex": "warn",
      "jsx-a11y/no-static-element-interactions": "warn",
    },
  },

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
        "error",
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

  // scripts/ is Node.js CLI land without package.json "type":"module".
  // .js files are CJS by default and use require() legitimately; forcing
  // ESM here would require .mjs rename + __filename shim + doc updates
  // for no runtime gain. The TS-aware rule still applies to scripts/*.ts
  // and scripts/*.mjs should we ever add them.
  {
    files: ["scripts/**/*.js"],
    rules: {
      "@typescript-eslint/no-require-imports": "off",
      "@typescript-eslint/no-var-requires": "off",
    },
  },
)
