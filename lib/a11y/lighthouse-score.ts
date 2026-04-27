/**
 * BS.11.7 — Pure Lighthouse-equivalent accessibility score helper for
 * the Platforms page a11y-budget spec
 * (`e2e/bs11-7-platforms-a11y.spec.ts`).
 *
 * Why we model the Lighthouse a11y score ourselves rather than running
 * Lighthouse CLI in-process:
 *
 *   • Lighthouse's CI binary (`@lhci/cli` + `lighthouse`) ships ~120 MB
 *     of additional dependencies, owns its own browser launch, and
 *     bundles a fixed Chrome version. The repo already drives a real
 *     Chromium via Playwright; bringing Lighthouse alongside duplicates
 *     the browser stack and bloats the dev image substantially.
 *   • Lighthouse's accessibility category is implemented on top of
 *     `axe-core` — every Lighthouse a11y audit is a thin adapter over
 *     a (small set of) axe rule(s). The repo already has `axe-core`
 *     present in `node_modules` (transitive dep of
 *     `eslint-plugin-jsx-a11y`); injecting it into the page under
 *     test and computing the same kind of weighted score Lighthouse
 *     emits is materially equivalent for the BS.11.7 row's "score
 *     ≥ 90" gate without the duplicate-browser overhead.
 *   • The runbook at `docs/ops/bs11_7_a11y_runbook.md` documents the
 *     manual `npx lhci collect` cross-check that closes the row's
 *     `[D]` flip on a real production deployment — the automated spec
 *     ships the dev-time guard.
 *
 * Lighthouse's accessibility category score (per
 * `lighthouse/core/config/default-config.js`, version 11/12) is a
 * binary-per-audit weighted average:
 *
 *     relevant_rules = passes ∪ violations
 *                       (NOT inapplicable, NOT incomplete)
 *     total_weight   = Σ weight(rule) for rule in relevant_rules
 *     pass_weight    = Σ weight(rule) for rule in passes
 *     score          = round(pass_weight / total_weight * 100)
 *
 * If a rule fails for ANY node it counts as failed for the entire
 * audit (no partial credit). If `relevant_rules` is empty the score is
 * 100 (a page that triggers no a11y rule has nothing to fail).
 *
 * Lighthouse weights rules per `audit.meta.scoreDisplayMode` and
 * impact tier. The full table varies across versions but the pattern
 * is:
 *
 *   • Critical-impact violations (e.g. `aria-hidden-body`,
 *     `aria-required-children`, `button-name`) — weight 10
 *   • Serious-impact violations (most rules) — weight 7
 *   • Moderate-impact violations — weight 3
 *   • Minor-impact violations — weight 1
 *
 * `axe-core` already labels each rule with one of those four impact
 * tiers, so `weightForImpact()` below maps them 1:1 to Lighthouse's
 * tier weights. For the BS.11.7 ≥ 90 threshold the precise weights
 * matter less than the proportionality — a single critical violation
 * costs ~10x more than a minor one, exactly as Lighthouse does it.
 *
 * Module-global state audit (per docs/sop/implement_phase_step.md
 * Step 1): this module is dependency-free, exports only pure
 * functions and frozen const objects, and reads no globals. Cross-
 * worker derivation is trivially identical (Answer #1 in the SOP).
 */

/** BS.11.7 row's literal acceptance gate — Lighthouse a11y ≥ 90. */
export const BS11_7_PLATFORMS_MIN_A11Y_SCORE = 90

/** Floor on how many rules must be evaluated for the score to be
 *  meaningful. A page that only triggers e.g. one rule and passes
 *  it would score 100 trivially — the spec asserts a healthy run
 *  evaluated at least this many rules so a regression that
 *  accidentally bypasses axe (no DOM, blank page) red-fails. */
export const BS11_7_MIN_EVALUATED_RULES = 10

/** Lighthouse-style weight per axe impact tier. The exact integers
 *  match Lighthouse's weighting model; the proportionality (10 : 7 :
 *  3 : 1) is what the score ≥ 90 gate actually depends on. */
export const LIGHTHOUSE_IMPACT_WEIGHTS = Object.freeze({
  critical: 10,
  serious: 7,
  moderate: 3,
  minor: 1,
} as const)

export type AxeImpact = "critical" | "serious" | "moderate" | "minor"

/** Subset of an axe-core rule result we depend on. Keeping this
 *  narrower than `axe.Result` keeps the helper testable without
 *  shipping an axe-core fixture for every unit test. */
export interface AxeResultLike {
  id: string
  impact: AxeImpact | null | undefined
  nodes: ReadonlyArray<unknown>
}

/** Subset of the `axe.AxeResults` shape we depend on. */
export interface AxeRunSummary {
  violations: ReadonlyArray<AxeResultLike>
  passes: ReadonlyArray<AxeResultLike>
  incomplete: ReadonlyArray<AxeResultLike>
  inapplicable: ReadonlyArray<AxeResultLike>
}

export interface A11yViolationDetail {
  id: string
  impact: AxeImpact | null
  weight: number
  nodeCount: number
}

export interface A11yScoreVerdict {
  /** Lighthouse-style score in [0, 100]. */
  score: number
  /** Whether the score cleared `threshold` AND the relevant-rule
   *  floor (`BS11_7_MIN_EVALUATED_RULES`). */
  passed: boolean
  /** Human-readable failure reasons; empty when `passed === true`. */
  reasons: ReadonlyArray<string>
  /** The threshold the verdict was evaluated against (default 90). */
  threshold: number
  /** Number of axe rules whose result counted toward the score
   *  (passes + violations, NOT inapplicable/incomplete). */
  evaluatedRuleCount: number
  /** Sum of weights of rules in `passes`. */
  passWeight: number
  /** Sum of weights of rules in `violations`. */
  failWeight: number
  /** One entry per violation rule, ordered by descending weight then id. */
  violations: ReadonlyArray<A11yViolationDetail>
}

/** Map an axe impact tier → Lighthouse-equivalent weight. Falls back
 *  to `minor` (weight 1) for null / undefined / unknown impact so a
 *  malformed axe response doesn't blow up the scoring. */
export function weightForImpact(impact: AxeImpact | null | undefined): number {
  if (impact === "critical") return LIGHTHOUSE_IMPACT_WEIGHTS.critical
  if (impact === "serious") return LIGHTHOUSE_IMPACT_WEIGHTS.serious
  if (impact === "moderate") return LIGHTHOUSE_IMPACT_WEIGHTS.moderate
  if (impact === "minor") return LIGHTHOUSE_IMPACT_WEIGHTS.minor
  return LIGHTHOUSE_IMPACT_WEIGHTS.minor
}

/** Round to nearest integer, half-up. JS's `Math.round` is half-away-
 *  from-zero so this matches Lighthouse's score rounding for non-
 *  negative inputs (which the score always is). */
function roundScore(value: number): number {
  return Math.round(value)
}

interface ComputeA11yScoreInput {
  summary: AxeRunSummary
  /** Defaults to BS11_7_PLATFORMS_MIN_A11Y_SCORE. */
  threshold?: number
  /** Defaults to BS11_7_MIN_EVALUATED_RULES. */
  minEvaluatedRules?: number
}

/**
 * Apply the Lighthouse weighted-average model to an axe summary.
 *
 *   • Score = round(pass_weight / total_weight * 100), clamped to
 *     [0, 100]. If `total_weight === 0` (no rules evaluated) the
 *     score is 100 — but the verdict will fail because the
 *     evaluated-rule floor (`minEvaluatedRules`) catches it.
 *   • `passed` requires both `score >= threshold` AND
 *     `evaluatedRuleCount >= minEvaluatedRules`. The second clause
 *     guards against "blank page passes trivially" regressions.
 *   • `reasons[]` enumerates every threshold breach so the error
 *     message in the spec can include the score AND the bad rules
 *     without further string juggling.
 */
export function computeA11yScore(input: ComputeA11yScoreInput): A11yScoreVerdict {
  const summary = input.summary
  const threshold = input.threshold ?? BS11_7_PLATFORMS_MIN_A11Y_SCORE
  const minEvaluatedRules = input.minEvaluatedRules ?? BS11_7_MIN_EVALUATED_RULES

  const passWeight = sumWeights(summary.passes)
  const failWeight = sumWeights(summary.violations)
  const totalWeight = passWeight + failWeight
  const evaluatedRuleCount = summary.passes.length + summary.violations.length

  let rawScore: number
  if (totalWeight === 0) {
    rawScore = 100
  } else {
    rawScore = (passWeight / totalWeight) * 100
  }
  const score = Math.max(0, Math.min(100, roundScore(rawScore)))

  const violations: A11yViolationDetail[] = summary.violations
    .map((rule): A11yViolationDetail => ({
      id: rule.id,
      impact: rule.impact ?? null,
      weight: weightForImpact(rule.impact),
      nodeCount: rule.nodes.length,
    }))
    .sort((a, b) => {
      if (b.weight !== a.weight) return b.weight - a.weight
      return a.id.localeCompare(b.id)
    })

  const reasons: string[] = []
  if (evaluatedRuleCount < minEvaluatedRules) {
    reasons.push(
      `evaluatedRuleCount=${evaluatedRuleCount} < ${minEvaluatedRules} — ` +
      `axe likely failed to inject or the page rendered no auditable DOM`,
    )
  }
  if (score < threshold) {
    reasons.push(
      `score=${score} < threshold=${threshold} ` +
      `(passWeight=${passWeight}, failWeight=${failWeight})`,
    )
  }
  for (const v of violations) {
    reasons.push(
      `violation rule=${v.id} impact=${v.impact ?? "unknown"} ` +
      `weight=${v.weight} nodes=${v.nodeCount}`,
    )
  }

  return {
    score,
    passed: reasons.length === 0,
    reasons,
    threshold,
    evaluatedRuleCount,
    passWeight,
    failWeight,
    violations,
  }
}

function sumWeights(rules: ReadonlyArray<AxeResultLike>): number {
  let total = 0
  for (const rule of rules) {
    total += weightForImpact(rule.impact)
  }
  return total
}

/**
 * One-line summary readable in CI logs and pasted into HANDOFF
 * entries. Format:
 *
 *   [BS.11.7] platforms-grid       pass  score=95 (passW=178, failW=10) rules=42 violations=1
 */
export function formatA11ySummary(scenario: string, verdict: A11yScoreVerdict): string {
  const status = verdict.passed ? "pass" : "FAIL"
  return (
    `[BS.11.7] ${scenario.padEnd(22, " ")}` +
    ` ${status.padEnd(4, " ")}` +
    ` score=${verdict.score}` +
    ` (passW=${verdict.passWeight}, failW=${verdict.failWeight})` +
    ` rules=${verdict.evaluatedRuleCount}` +
    ` violations=${verdict.violations.length}`
  )
}
