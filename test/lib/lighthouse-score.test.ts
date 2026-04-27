/**
 * BS.11.7 — Pure-helper contract tests for `lib/a11y/lighthouse-score.ts`.
 *
 * The Playwright spec at `e2e/bs11-7-platforms-a11y.spec.ts` injects
 * `axe-core` into the rendered Platforms fixture page and runs
 * `axe.run()` to produce an `axe.AxeResults` object. The threshold
 * math that turns that result into a Lighthouse-equivalent weighted
 * score lives in `lib/a11y/lighthouse-score.ts` — unit-testing it here
 * means a math regression red-fails CI without spinning up a Chrome
 * instance, and the Playwright spec only has to cover the page
 * integration.
 */

import { describe, expect, it } from "vitest"

import {
  BS11_7_MIN_EVALUATED_RULES,
  BS11_7_PLATFORMS_MIN_A11Y_SCORE,
  LIGHTHOUSE_IMPACT_WEIGHTS,
  computeA11yScore,
  formatA11ySummary,
  weightForImpact,
  type AxeImpact,
  type AxeResultLike,
  type AxeRunSummary,
} from "@/lib/a11y/lighthouse-score"

function rule(
  id: string,
  impact: AxeImpact | null | undefined,
  nodeCount: number,
): AxeResultLike {
  return {
    id,
    impact,
    nodes: Array.from({ length: nodeCount }, (_, idx) => ({ idx })),
  }
}

function summary(parts: Partial<AxeRunSummary> = {}): AxeRunSummary {
  return {
    violations: [],
    passes: [],
    incomplete: [],
    inapplicable: [],
    ...parts,
  }
}

describe("BS.11.7 — a11y thresholds (literal SoT)", () => {
  it("exposes the BS.11.7 row's literal budget constants", () => {
    expect(BS11_7_PLATFORMS_MIN_A11Y_SCORE).toBe(90)
    expect(BS11_7_MIN_EVALUATED_RULES).toBe(10)
    expect(LIGHTHOUSE_IMPACT_WEIGHTS).toEqual({
      critical: 10,
      serious: 7,
      moderate: 3,
      minor: 1,
    })
  })

  it("freezes the LIGHTHOUSE_IMPACT_WEIGHTS map so callers cannot drift it", () => {
    expect(Object.isFrozen(LIGHTHOUSE_IMPACT_WEIGHTS)).toBe(true)
  })
})

describe("BS.11.7 — weightForImpact", () => {
  it("maps the four axe impact tiers to Lighthouse-style weights", () => {
    expect(weightForImpact("critical")).toBe(10)
    expect(weightForImpact("serious")).toBe(7)
    expect(weightForImpact("moderate")).toBe(3)
    expect(weightForImpact("minor")).toBe(1)
  })

  it("falls back to minor weight (1) for null / undefined / unknown impact", () => {
    expect(weightForImpact(null)).toBe(1)
    expect(weightForImpact(undefined)).toBe(1)
    // @ts-expect-error — exercises the runtime fallback for off-spec values.
    expect(weightForImpact("catastrophic")).toBe(1)
  })
})

describe("BS.11.7 — computeA11yScore", () => {
  it("scores 100 when every relevant rule passes", () => {
    const passes = Array.from({ length: 20 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const verdict = computeA11yScore({ summary: summary({ passes }) })
    expect(verdict.score).toBe(100)
    expect(verdict.passed).toBe(true)
    expect(verdict.reasons).toEqual([])
    expect(verdict.evaluatedRuleCount).toBe(20)
    expect(verdict.passWeight).toBe(20 * 7)
    expect(verdict.failWeight).toBe(0)
    expect(verdict.violations).toEqual([])
  })

  it("ignores `inapplicable` and `incomplete` when computing the denominator", () => {
    const passes = Array.from({ length: 15 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const inapplicable = Array.from({ length: 8 }, (_, i) => rule(`inap-${i}`, "critical", 0))
    const incomplete = Array.from({ length: 3 }, (_, i) => rule(`inc-${i}`, "critical", 0))
    const verdict = computeA11yScore({
      summary: summary({ passes, inapplicable, incomplete }),
    })
    // inapplicable + incomplete must NOT contribute to the denominator.
    // 15 passes × weight 7 = 105 / 105 = 100 score.
    expect(verdict.score).toBe(100)
    expect(verdict.evaluatedRuleCount).toBe(15)
  })

  it("returns score 100 when no rules were evaluated, but fails on the floor", () => {
    const verdict = computeA11yScore({ summary: summary() })
    expect(verdict.score).toBe(100)
    expect(verdict.passed).toBe(false)
    expect(verdict.reasons.some((r) => r.includes("evaluatedRuleCount=0"))).toBe(true)
    // Threshold itself is satisfied (100 ≥ 90), so no second reason is added
    // for that — only the floor breach.
    expect(verdict.reasons.length).toBe(1)
  })

  it("computes the Lighthouse-style weighted average for a mixed run", () => {
    // 18 serious passes (weight 7 × 18 = 126) + 1 minor violation (weight 1).
    // total = 127, pass = 126 → score = round(126/127 * 100) = round(99.21) = 99.
    const passes = Array.from({ length: 18 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const violations = [rule("color-contrast", "minor", 2)]
    const verdict = computeA11yScore({
      summary: summary({ passes, violations }),
    })
    expect(verdict.score).toBe(99)
    expect(verdict.passWeight).toBe(126)
    expect(verdict.failWeight).toBe(1)
    expect(verdict.evaluatedRuleCount).toBe(19)
    // 99 ≥ 90 threshold AND 19 ≥ 10 floor — but the rule's a violation,
    // and the verdict surfaces every violation as a reason.
    expect(verdict.passed).toBe(false)
    expect(verdict.reasons.some((r) => r.includes("violation rule=color-contrast"))).toBe(true)
  })

  it("flags below-threshold scores explicitly in `reasons`", () => {
    // 10 serious passes (weight 70) + 1 critical violation (weight 10).
    // total = 80, pass = 70 → score = round(70/80 * 100) = round(87.5) = 88.
    const passes = Array.from({ length: 10 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const violations = [rule("aria-hidden-body", "critical", 1)]
    const verdict = computeA11yScore({
      summary: summary({ passes, violations }),
    })
    expect(verdict.score).toBe(88)
    expect(verdict.passed).toBe(false)
    expect(verdict.reasons.some((r) => r.includes("score=88") && r.includes("threshold=90"))).toBe(true)
    expect(verdict.reasons.some((r) => r.includes("aria-hidden-body"))).toBe(true)
    expect(verdict.reasons.some((r) => r.includes("impact=critical"))).toBe(true)
  })

  it("orders violations by descending weight then ascending id", () => {
    const passes = Array.from({ length: 25 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const violations = [
      rule("zzzz-minor", "minor", 1),
      rule("aaaa-critical", "critical", 1),
      rule("mmmm-serious", "serious", 1),
      rule("bbbb-critical", "critical", 1),
    ]
    const verdict = computeA11yScore({ summary: summary({ passes, violations }) })
    expect(verdict.violations.map((v) => v.id)).toEqual([
      "aaaa-critical",
      "bbbb-critical",
      "mmmm-serious",
      "zzzz-minor",
    ])
  })

  it("clamps score into [0, 100]", () => {
    const violations = Array.from({ length: 30 }, (_, i) => rule(`fail-${i}`, "critical", 1))
    const verdict = computeA11yScore({ summary: summary({ violations }) })
    expect(verdict.score).toBe(0)
    expect(verdict.passed).toBe(false)
  })

  it("records nodeCount per violation for human-readable reasons", () => {
    const passes = Array.from({ length: 20 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const violations = [rule("button-name", "critical", 4)]
    const verdict = computeA11yScore({ summary: summary({ passes, violations }) })
    const detail = verdict.violations.find((v) => v.id === "button-name")
    expect(detail).toBeDefined()
    expect(detail?.nodeCount).toBe(4)
    expect(detail?.weight).toBe(10)
    expect(detail?.impact).toBe("critical")
  })

  it("treats null impact as minor weight (defensive against malformed axe results)", () => {
    const passes = Array.from({ length: 10 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const violations = [rule("malformed", null, 1)]
    const verdict = computeA11yScore({ summary: summary({ passes, violations }) })
    expect(verdict.violations[0].weight).toBe(1)
    expect(verdict.violations[0].impact).toBe(null)
  })

  it("respects custom threshold and minEvaluatedRules overrides", () => {
    const passes = Array.from({ length: 5 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const verdict = computeA11yScore({
      summary: summary({ passes }),
      threshold: 50,
      minEvaluatedRules: 3,
    })
    expect(verdict.passed).toBe(true)
    expect(verdict.threshold).toBe(50)
    expect(verdict.evaluatedRuleCount).toBe(5)
  })
})

describe("BS.11.7 — formatA11ySummary", () => {
  it("emits a fixed-shape one-liner readable in CI logs", () => {
    const passes = Array.from({ length: 20 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const verdict = computeA11yScore({ summary: summary({ passes }) })
    const line = formatA11ySummary("platforms-grid", verdict)
    expect(line).toMatch(/^\[BS\.11\.7\] platforms-grid\s+pass/)
    expect(line).toContain("score=100")
    expect(line).toContain("rules=20")
    expect(line).toContain("violations=0")
    expect(line).toContain("passW=140")
    expect(line).toContain("failW=0")
  })

  it("flags FAIL prominently when verdict failed", () => {
    const passes = Array.from({ length: 10 }, (_, i) => rule(`pass-${i}`, "serious", 1))
    const violations = [rule("aria-hidden-body", "critical", 1)]
    const verdict = computeA11yScore({ summary: summary({ passes, violations }) })
    const line = formatA11ySummary("platforms-detail", verdict)
    expect(line).toContain("FAIL")
    expect(line).toContain("violations=1")
  })
})
