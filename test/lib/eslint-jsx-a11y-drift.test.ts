// FX.7.12 — drift-guard for eslint-plugin-jsx-a11y rule severities.
//
// Background: eslint-config-next ships 6 jsx-a11y rules at warn-level by
// default. FX.7.12 layered the plugin's full recommended ruleset (34
// rules) on top — promoting HTML/ARIA contract violations to "error"
// (always-invalid markup) and keeping behavioural rules at "warn" while
// the existing UI tree gets refactored.
//
// This guard pins:
//   1. The ruleset is *loaded* at all (catches a future cleanup that
//      drops the FX.7.12 block from eslint.config.mjs).
//   2. Each "critical" rule resolves to "error" severity for a real
//      .tsx file path. Catches:
//        - someone downgrading a critical rule to "warn" / "off",
//        - an `eslint-plugin-jsx-a11y` major bump that flips a default,
//        - a future `ignores` block that accidentally excludes the UI
//          source tree from the FX.7.12 rule layer.
//   3. End-to-end smoke: lint a fixture string containing one of each
//      critical violation pattern, confirm the corresponding rule fires
//      with severity 2 (error). This catches the case where the rule is
//      *configured* but the plugin isn't actually wired.
//
// Why a vitest test (not just a CI lint job): the lint job runs on the
// whole tree and reports many warnings; if a critical rule silently
// downgraded from "error" to "warn" the lint exit code stays 0 and the
// regression ships. This guard fails fast on any severity drift.

import { describe, expect, it, beforeAll } from "vitest"
import { ESLint, type Linter } from "eslint"
import path from "node:path"

const REPO_ROOT = path.resolve(__dirname, "..", "..")

// Critical jsx-a11y rules that MUST resolve to "error" severity in
// `.tsx` files under the project root. Anything not in this list is
// allowed to be "warn" (or, for project-local relaxations like
// `anchor-ambiguous-text`, "off").
const CRITICAL_RULES = [
  "jsx-a11y/alt-text",
  "jsx-a11y/anchor-has-content",
  "jsx-a11y/anchor-is-valid",
  "jsx-a11y/aria-activedescendant-has-tabindex",
  "jsx-a11y/aria-props",
  "jsx-a11y/aria-proptypes",
  "jsx-a11y/aria-role",
  "jsx-a11y/aria-unsupported-elements",
  "jsx-a11y/autocomplete-valid",
  "jsx-a11y/heading-has-content",
  "jsx-a11y/html-has-lang",
  "jsx-a11y/iframe-has-title",
  "jsx-a11y/img-redundant-alt",
  "jsx-a11y/no-access-key",
  "jsx-a11y/no-distracting-elements",
  "jsx-a11y/no-redundant-roles",
  "jsx-a11y/role-has-required-aria-props",
  "jsx-a11y/role-supports-aria-props",
  "jsx-a11y/scope",
  "jsx-a11y/tabindex-no-positive",
] as const

// "Behavioural" rules — kept at warn during the FX.7.12 → follow-up
// refactor window. The guard asserts they're at least *registered* (not
// silently dropped to "off") so the dashboard isn't blind to new
// regressions while we triage the existing pile.
const BEHAVIOURAL_RULES_AT_WARN = [
  "jsx-a11y/click-events-have-key-events",
  "jsx-a11y/interactive-supports-focus",
  "jsx-a11y/label-has-associated-control",
  "jsx-a11y/media-has-caption",
  "jsx-a11y/mouse-events-have-key-events",
  "jsx-a11y/no-autofocus",
  "jsx-a11y/no-interactive-element-to-noninteractive-role",
  "jsx-a11y/no-noninteractive-element-interactions",
  "jsx-a11y/no-noninteractive-element-to-interactive-role",
  "jsx-a11y/no-noninteractive-tabindex",
  "jsx-a11y/no-static-element-interactions",
] as const

type Severity = 0 | 1 | 2

function severityOf(entry: Linter.RuleEntry | undefined): Severity {
  if (entry === undefined) return 0
  const raw = Array.isArray(entry) ? entry[0] : entry
  if (raw === "off" || raw === 0) return 0
  if (raw === "warn" || raw === 1) return 1
  if (raw === "error" || raw === 2) return 2
  return 0
}

describe("FX.7.12 jsx-a11y eslint rule drift guard", () => {
  let resolved: Linter.Config

  beforeAll(async () => {
    // Resolve the *effective* ESLint config for a representative .tsx
    // file under components/. ESLint walks the flat-config array, merges
    // overrides, and returns the final per-file config — so this catches
    // both "rule was deleted from eslint.config.mjs" and "an `ignores`
    // block now excludes components/".
    const eslint = new ESLint({ cwd: REPO_ROOT })
    const probeFile = path.join(REPO_ROOT, "components", "ui", "pagination.tsx")
    resolved = (await eslint.calculateConfigForFile(probeFile)) as Linter.Config
  })

  it("the eslint config has rules loaded for the probe file", () => {
    expect(resolved).toBeTruthy()
    expect(resolved.rules).toBeTruthy()
  })

  it.each(CRITICAL_RULES)(
    "%s is configured at severity 'error' (severity 2)",
    (ruleId) => {
      const entry = resolved.rules?.[ruleId]
      const sev = severityOf(entry)
      expect(
        sev,
        `expected ${ruleId} to be 'error'/2 in eslint.config.mjs (got ${JSON.stringify(entry)}) — FX.7.12 critical rules must not be downgraded`,
      ).toBe(2)
    },
  )

  it.each(BEHAVIOURAL_RULES_AT_WARN)(
    "%s is at least registered at severity 'warn' (severity 1) — not silently disabled",
    (ruleId) => {
      const entry = resolved.rules?.[ruleId]
      const sev = severityOf(entry)
      expect(
        sev,
        `expected ${ruleId} to be 'warn'/1 or 'error'/2 (got ${JSON.stringify(entry)}) — turning a behavioural a11y rule fully off needs an explicit follow-up row, not a silent downgrade`,
      ).toBeGreaterThanOrEqual(1)
    },
  )

  it("end-to-end: linting a fixture .tsx triggers critical rules as errors", async () => {
    const eslint = new ESLint({ cwd: REPO_ROOT })

    // 4 patterns, each one violates a different critical rule. The
    // fixture is wrapped in a single fragment so it parses as valid
    // JSX. We deliberately do NOT include patterns for behavioural
    // rules — those would just appear as warnings and dilute the
    // assertion.
    const fixture = `
      const X = () => (
        <>
          {/* alt-text: img missing alt */}
          <img src="/x.png" />
          {/* anchor-is-valid: href as javascript: */}
          <a href="javascript:void(0)">click</a>
          {/* role-supports-aria-props: aria-selected on listitem */}
          <li role="listitem" aria-selected="true">x</li>
          {/* aria-role: unknown role */}
          <div role="totally-not-a-real-aria-role" />
        </>
      )
      export default X
    `
    const results = await eslint.lintText(fixture, {
      filePath: path.join(REPO_ROOT, "components", "__fx_7_12_fixture__.tsx"),
    })
    const messages = results[0].messages
    const errorRules = new Set(
      messages.filter((m) => m.severity === 2).map((m) => m.ruleId),
    )

    // The 4 critical rules above must each show up as error. (Other
    // critical rules might also fire incidentally — we only assert the
    // specific ones our fixture targets.)
    for (const expected of [
      "jsx-a11y/alt-text",
      "jsx-a11y/anchor-is-valid",
      "jsx-a11y/role-supports-aria-props",
      "jsx-a11y/aria-role",
    ]) {
      expect(
        errorRules.has(expected),
        `fixture expected ${expected} to fire as error; got error rules: ${JSON.stringify([...errorRules])}`,
      ).toBe(true)
    }
  })
})
