/**
 * AS.7.8 — First-login onboarding helpers.
 *
 * Pure browser-safe helpers (no React, no DOM, no Next imports) the
 * AS.7.8 `/onboarding` page composes. The page is a single scaffold
 * around 4 step kinds (3 input steps + 1 celebration step):
 *
 *   1. **tenant**   — Confirm / rename the user's workspace tenant.
 *                     Calls `adminPatchTenant()` when the role allows
 *                     it; otherwise the row renders read-only.
 *   2. **profile**  — Capture the display name. Persisted to
 *                     `localStorage` until the backend `PATCH /auth/me`
 *                     endpoint lands (Phase-1 fail-closed pattern —
 *                     same approach as the AS.7.7 GDPR forms).
 *   3. **project**  — Create the user's first project via
 *                     `createTenantProject()` (already wired in
 *                     `lib/api.ts` since Y8 row 2).
 *   4. **celebrate** — 30-particle burst from the centre + rising
 *                     "Welcome aboard, X!" wordmark. Pure visual
 *                     reward; triggers the `/` redirect on completion.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals or pure
 *     functions. Zero module-level mutable container.
 *   - Cross-worker / cross-tab derivation is trivially identical
 *     (Answer #1 of the SOP §1 audit) — the celebration particle
 *     layout uses a deterministic golden-ratio multiplicative hash
 *     so SSR / vitest / browser emit byte-identical particle arrays.
 *
 * Read-after-write timing audit: N/A — pure helpers, no async DB
 * calls, no parallelisation change vs. existing auth-context.
 */

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Step vocabulary
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** Canonical 4-step vocabulary the dedicated page renders. Frozen
 *  `as const` so adding a new step requires updating the test in
 *  lockstep. */
export const ONBOARDING_STEP_KIND = {
  tenant: "tenant",
  profile: "profile",
  project: "project",
  celebrate: "celebrate",
} as const

export type OnboardingStepKind =
  (typeof ONBOARDING_STEP_KIND)[keyof typeof ONBOARDING_STEP_KIND]

/** Drift guard: every step the page renders, in canonical order.
 *  Pinned by the test so adding a new step without updating the test
 *  is a CI red. */
export const ONBOARDING_STEPS_ORDERED = [
  "tenant",
  "profile",
  "project",
  "celebrate",
] as const

/** Per-step UI copy. Pinned by the test; do not edit without
 *  updating the test. */
export interface OnboardingStepCopy {
  readonly title: string
  readonly summary: string
  readonly ctaLabel: string
}

export const ONBOARDING_STEP_COPY: Readonly<
  Record<OnboardingStepKind, OnboardingStepCopy>
> = Object.freeze({
  tenant: Object.freeze({
    title: "Confirm your workspace",
    summary:
      "OmniSight created a workspace for you when you signed up. Give it a name your teammates will recognise.",
    ctaLabel: "Continue",
  }),
  profile: Object.freeze({
    title: "Tell us your name",
    summary:
      "We'll use this on your profile and in audit logs. You can change it any time from settings.",
    ctaLabel: "Continue",
  }),
  project: Object.freeze({
    title: "Create your first project",
    summary:
      "Projects scope every artefact OmniSight produces — agents, dashboards, datasets. Pick a product line to seed sane defaults.",
    ctaLabel: "Create project",
  }),
  celebrate: Object.freeze({
    title: "You're all set",
    summary:
      "Your workspace is ready. We're dropping you in the dashboard.",
    ctaLabel: "Open dashboard",
  }),
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Step resolver
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface FirstLoginRequiredStepInput {
  /** Resolved tenant display name (from `listUserTenants()`). Empty
   *  / null means the user has never confirmed it on the onboarding
   *  page. */
  readonly tenantName: string | null
  /** Resolved display name (read from localStorage or, once the
   *  backend endpoint lands, the `whoami` payload). Empty / null
   *  means the profile step hasn't completed. */
  readonly displayName: string | null
  /** True when the user has at least one project under their
   *  tenant. */
  readonly hasProject: boolean
  /** True when every prior step has been confirmed AND the
   *  celebration burst has fired. The page sets this once the user
   *  reaches the terminal redirect; before then `firstLoginRequiredStep`
   *  may dwell on `celebrate`. */
  readonly celebrated: boolean
}

/** Compute which step the wizard should render given the current
 *  state. Pure cascade — the first unsatisfied gate wins. */
export function firstLoginRequiredStep(
  input: FirstLoginRequiredStepInput,
): OnboardingStepKind {
  if (!input.tenantName || !input.tenantName.trim()) {
    return ONBOARDING_STEP_KIND.tenant
  }
  if (!input.displayName || !input.displayName.trim()) {
    return ONBOARDING_STEP_KIND.profile
  }
  if (!input.hasProject) {
    return ONBOARDING_STEP_KIND.project
  }
  return ONBOARDING_STEP_KIND.celebrate
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Tenant step
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const TENANT_NAME_MIN_LENGTH = 2
export const TENANT_NAME_MAX_LENGTH = 64

export interface TenantNameGateInput {
  readonly busy: boolean
  readonly name: string
  /** False when the role can't call `adminPatchTenant`. The page
   *  surfaces a "locked" reason so the CTA stays gated even if the
   *  user typed a valid name. */
  readonly canEdit: boolean
}

/** Drift guard: every reason string the tenant-name gate may emit.
 *  Pinned by the test. */
export const TENANT_NAME_BLOCKED_REASONS = [
  "busy",
  "locked",
  "name_too_short",
  "name_too_long",
] as const

export type TenantNameBlockedReason =
  (typeof TENANT_NAME_BLOCKED_REASONS)[number]

export function tenantNameBlockedReason(
  input: TenantNameGateInput,
): TenantNameBlockedReason | null {
  if (input.busy) return "busy"
  if (!input.canEdit) return "locked"
  const trimmed = input.name.trim()
  if (trimmed.length < TENANT_NAME_MIN_LENGTH) return "name_too_short"
  if (trimmed.length > TENANT_NAME_MAX_LENGTH) return "name_too_long"
  return null
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Profile step
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const DISPLAY_NAME_MIN_LENGTH = 2
export const DISPLAY_NAME_MAX_LENGTH = 64

export interface ProfileGateInput {
  readonly busy: boolean
  readonly displayName: string
}

export const PROFILE_BLOCKED_REASONS = [
  "busy",
  "display_name_too_short",
  "display_name_too_long",
] as const

export type ProfileBlockedReason = (typeof PROFILE_BLOCKED_REASONS)[number]

export function profileBlockedReason(
  input: ProfileGateInput,
): ProfileBlockedReason | null {
  if (input.busy) return "busy"
  const trimmed = input.displayName.trim()
  if (trimmed.length < DISPLAY_NAME_MIN_LENGTH) return "display_name_too_short"
  if (trimmed.length > DISPLAY_NAME_MAX_LENGTH) return "display_name_too_long"
  return null
}

/** localStorage key for the per-account display name set during
 *  onboarding. Namespaced so other features won't trip on it. */
export const ONBOARDING_DISPLAY_NAME_KEY = "omnisight:onboarding:displayName"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Project step
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const PROJECT_NAME_MIN_LENGTH = 2
export const PROJECT_NAME_MAX_LENGTH = 64

/** Product-line ids byte-equal to `lib/api.ts::ProductLine`. The
 *  drift-guard test pins both this tuple AND the per-row copy. */
export const PRODUCT_LINE_IDS_ORDERED = [
  "embedded",
  "web",
  "mobile",
  "software",
  "custom",
] as const

export type ProductLineId = (typeof PRODUCT_LINE_IDS_ORDERED)[number]

export interface ProductLineOption {
  readonly id: ProductLineId
  readonly label: string
  readonly summary: string
}

export const PRODUCT_LINE_OPTIONS: readonly ProductLineOption[] =
  Object.freeze([
    Object.freeze({
      id: "embedded",
      label: "Embedded / camera",
      summary: "ISP tuning, firmware, on-device AI pipelines.",
    }),
    Object.freeze({
      id: "web",
      label: "Web",
      summary: "Dashboards, portals, server-side rendering.",
    }),
    Object.freeze({
      id: "mobile",
      label: "Mobile",
      summary: "iOS / Android apps and SDK packaging.",
    }),
    Object.freeze({
      id: "software",
      label: "Server / software",
      summary: "Backend services, jobs, ML pipelines.",
    }),
    Object.freeze({
      id: "custom",
      label: "Other",
      summary: "Anything else — pick this and rename later.",
    }),
  ])

export interface ProjectGateInput {
  readonly busy: boolean
  readonly name: string
  readonly productLine: string | null
}

export const PROJECT_BLOCKED_REASONS = [
  "busy",
  "name_too_short",
  "name_too_long",
  "product_line_required",
] as const

export type ProjectBlockedReason = (typeof PROJECT_BLOCKED_REASONS)[number]

export function projectBlockedReason(
  input: ProjectGateInput,
): ProjectBlockedReason | null {
  if (input.busy) return "busy"
  const trimmed = input.name.trim()
  if (trimmed.length < PROJECT_NAME_MIN_LENGTH) return "name_too_short"
  if (trimmed.length > PROJECT_NAME_MAX_LENGTH) return "name_too_long"
  const line = (input.productLine || "").toLowerCase().trim()
  if (
    !line ||
    !PRODUCT_LINE_IDS_ORDERED.includes(line as ProductLineId)
  ) {
    return "product_line_required"
  }
  return null
}

/** Lower-case + replace whitespace runs with `-` + strip everything
 *  outside `[a-z0-9-]` + clamp to 64. Mirrors the conventional
 *  slug pattern the backend `tenant_projects.slug` column accepts.
 *  Pure — same input always produces the same output. */
export function slugifyProjectName(name: string): string {
  const lowered = (name || "").toLowerCase().trim()
  const dashed = lowered.replace(/[\s_]+/g, "-")
  const stripped = dashed.replace(/[^a-z0-9-]/g, "")
  const collapsed = stripped.replace(/-+/g, "-").replace(/^-|-$/g, "")
  return collapsed.slice(0, 64)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Tenant-update + project-create error classifier
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const ONBOARDING_ERROR_KIND = {
  invalidInput: "invalid_input",
  conflict: "conflict",
  rateLimited: "rate_limited",
  serviceUnavailable: "service_unavailable",
} as const

export type OnboardingErrorKind =
  (typeof ONBOARDING_ERROR_KIND)[keyof typeof ONBOARDING_ERROR_KIND]

export const TENANT_UPDATE_ERROR_COPY: Readonly<
  Record<OnboardingErrorKind, string>
> = Object.freeze({
  invalid_input:
    "That workspace name didn't pass the server's validation. Try a shorter, alphanumeric variation.",
  conflict:
    "A workspace with that name already exists. Pick something distinct.",
  rate_limited:
    "Too many workspace updates. Wait a moment before trying again.",
  service_unavailable:
    "We couldn't update the workspace right now. Try again in a few moments.",
})

export const PROJECT_CREATE_ERROR_COPY: Readonly<
  Record<OnboardingErrorKind, string>
> = Object.freeze({
  invalid_input:
    "That project name or slug didn't pass server validation. Try a shorter, alphanumeric variation.",
  conflict:
    "A project with that slug already exists in this workspace. Pick a different name.",
  rate_limited:
    "Too many project creations. Wait a moment before trying again.",
  service_unavailable:
    "We couldn't create the project right now. Try again in a few moments.",
})

export interface OnboardingErrorInput {
  readonly status: number | null
  readonly retryAfter?: string | null
}

export interface OnboardingErrorOutcome {
  readonly kind: OnboardingErrorKind
  readonly message: string
  readonly retryAfterSeconds: number | null
}

function _resolveRetryAfter(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null
  const num = Number(raw)
  if (Number.isFinite(num) && num >= 0) return num
  return null
}

function _classifyOnboardingError(
  input: OnboardingErrorInput,
  copyTable: Readonly<Record<OnboardingErrorKind, string>>,
): OnboardingErrorOutcome {
  const retry = _resolveRetryAfter(input.retryAfter)
  const status = input.status
  if (status === 400 || status === 422) {
    return Object.freeze({
      kind: ONBOARDING_ERROR_KIND.invalidInput,
      message: copyTable.invalid_input,
      retryAfterSeconds: null,
    })
  }
  if (status === 409) {
    return Object.freeze({
      kind: ONBOARDING_ERROR_KIND.conflict,
      message: copyTable.conflict,
      retryAfterSeconds: null,
    })
  }
  if (status === 429) {
    return Object.freeze({
      kind: ONBOARDING_ERROR_KIND.rateLimited,
      message: copyTable.rate_limited,
      retryAfterSeconds: retry,
    })
  }
  if (status === null || (status >= 500 && status < 600)) {
    return Object.freeze({
      kind: ONBOARDING_ERROR_KIND.serviceUnavailable,
      message: copyTable.service_unavailable,
      retryAfterSeconds: retry,
    })
  }
  return Object.freeze({
    kind: ONBOARDING_ERROR_KIND.serviceUnavailable,
    message: copyTable.service_unavailable,
    retryAfterSeconds: retry,
  })
}

export function classifyTenantUpdateError(
  input: OnboardingErrorInput,
): OnboardingErrorOutcome {
  return _classifyOnboardingError(input, TENANT_UPDATE_ERROR_COPY)
}

export function classifyProjectCreateError(
  input: OnboardingErrorInput,
): OnboardingErrorOutcome {
  return _classifyOnboardingError(input, PROJECT_CREATE_ERROR_COPY)
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Celebration burst
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** The row spec pins this constant: 30 particles erupt from the
 *  centre. Drift-guarded by the test. */
export const CELEBRATION_PARTICLE_COUNT = 30

/** Six brand-aligned hues — purple core + a couple of cool / warm
 *  satellites so the burst doesn't read monochrome. The hash below
 *  picks one per particle deterministically. Drift-guarded. */
export const CELEBRATION_PARTICLE_HUE_PALETTE = [
  "#a78bfa",
  "#c084fc",
  "#f0abfc",
  "#7dd3fc",
  "#86efac",
  "#facc15",
] as const

/** One particle's resolved layout. Frozen on emit so React can use it
 *  as a stable prop / key without defensive cloning. */
export interface CelebrationParticle {
  readonly index: number
  /** Outward angle in radians, measured clockwise from 12 o'clock. */
  readonly angleRad: number
  /** Travel distance in pixels from the centre. */
  readonly distancePx: number
  /** Hex hue picked from `CELEBRATION_PARTICLE_HUE_PALETTE`. */
  readonly hue: string
  /** Per-particle delay so the burst staggers across the duration. */
  readonly delayMs: number
  /** Per-particle animation duration. */
  readonly durationMs: number
  /** Resolved x offset from centre (sin × distance, rounded to
   *  3 decimals for cross-runner stability). */
  readonly xPx: number
  /** Resolved y offset from centre (-cos × distance). */
  readonly yPx: number
}

/** Deterministic golden-ratio multiplicative hash. Same `(seed, max)`
 *  always returns the same integer, so the particle layout is
 *  byte-equal across SSR / vitest / browser (Answer #1 of the SOP §1
 *  audit, deterministic-by-construction). */
function _goldenHash(seed: number, modulo: number): number {
  // Knuth's multiplicative hash constant; modulo applied last so
  // negative moduli are normalised to non-negative.
  const phi = 0.6180339887498949
  const product = (seed + 1) * phi
  const fraction = product - Math.floor(product)
  return Math.floor(fraction * modulo)
}

export type CelebrationMotionLevel = "off" | "subtle" | "normal" | "dramatic"

/** Total burst duration per motion level. The leaf reads this so it
 *  can fire `onComplete` after the animation settles. Off / subtle
 *  collapse to 0 so the page redirects without delay. */
export const CELEBRATION_DURATION_BY_LEVEL: Readonly<
  Record<CelebrationMotionLevel, number>
> = Object.freeze({
  off: 0,
  subtle: 0,
  normal: 1500,
  dramatic: 2400,
})

/** Build the deterministic 30-particle layout. The page renders the
 *  array as `<span>` leaves with the resolved CSS variables. Pure —
 *  same `(level, count)` always emits byte-identical output. */
export function buildCelebrationParticles(
  level: CelebrationMotionLevel,
  count: number = CELEBRATION_PARTICLE_COUNT,
): readonly CelebrationParticle[] {
  if (level === "off" || level === "subtle") return Object.freeze([])
  const isDramatic = level === "dramatic"
  // Inner ring distance vs outer ring; the dramatic level pushes
  // particles further so the eruption fills more of the card.
  const baseDistance = isDramatic ? 180 : 120
  const distanceJitter = isDramatic ? 60 : 40
  const baseDuration = isDramatic ? 1400 : 900
  const durationJitter = isDramatic ? 800 : 500
  const totalSpread = isDramatic ? 600 : 400
  const safeCount = Math.max(0, Math.floor(count))
  const particles: CelebrationParticle[] = []
  for (let i = 0; i < safeCount; i += 1) {
    const angleStep = (2 * Math.PI) / Math.max(1, safeCount)
    const baseAngle = angleStep * i
    // Jitter the angle by up to ±half-step so the ring doesn't read
    // as a perfect 30-pointed star.
    const angleJitter =
      ((_goldenHash(i * 7 + 1, 1000) - 500) / 500) * (angleStep / 2)
    const angleRad = baseAngle + angleJitter
    const distance =
      baseDistance + _goldenHash(i * 11 + 3, distanceJitter)
    const hue =
      CELEBRATION_PARTICLE_HUE_PALETTE[
        _goldenHash(i * 13 + 5, CELEBRATION_PARTICLE_HUE_PALETTE.length)
      ]
    const delayMs = _goldenHash(i * 17 + 7, totalSpread)
    const durationMs = baseDuration + _goldenHash(i * 19 + 11, durationJitter)
    const xPx = Math.round(Math.sin(angleRad) * distance * 1000) / 1000
    const yPx = Math.round(-Math.cos(angleRad) * distance * 1000) / 1000
    particles.push(
      Object.freeze({
        index: i,
        angleRad,
        distancePx: distance,
        hue,
        delayMs,
        durationMs,
        xPx,
        yPx,
      }),
    )
  }
  return Object.freeze(particles)
}

/** The "Welcome aboard" copy renders the user's display name when
 *  available, falling back to the bare phrase. Pure. */
export function formatWelcomeAboard(displayName: string | null): string {
  const trimmed = (displayName || "").trim()
  if (!trimmed) return "Welcome aboard"
  return `Welcome aboard, ${trimmed}`
}
