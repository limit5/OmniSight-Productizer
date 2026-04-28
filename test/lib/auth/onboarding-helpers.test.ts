/**
 * AS.7.8 — `lib/auth/onboarding-helpers.ts` contract tests.
 *
 * Pins the step vocabulary, copy table, step resolver cascade, three
 * blocked-reason gates (tenant rename / profile / project create),
 * the slugifier, the tenant-update + project-create error
 * classifiers, and the deterministic celebration-particle layout.
 */

import { describe, expect, it } from "vitest"

import {
  buildCelebrationParticles,
  CELEBRATION_DURATION_BY_LEVEL,
  CELEBRATION_PARTICLE_COUNT,
  CELEBRATION_PARTICLE_HUE_PALETTE,
  classifyProjectCreateError,
  classifyTenantUpdateError,
  DISPLAY_NAME_MAX_LENGTH,
  DISPLAY_NAME_MIN_LENGTH,
  firstLoginRequiredStep,
  formatWelcomeAboard,
  ONBOARDING_DISPLAY_NAME_KEY,
  ONBOARDING_ERROR_KIND,
  ONBOARDING_STEP_COPY,
  ONBOARDING_STEP_KIND,
  ONBOARDING_STEPS_ORDERED,
  PROFILE_BLOCKED_REASONS,
  profileBlockedReason,
  PROJECT_BLOCKED_REASONS,
  PROJECT_CREATE_ERROR_COPY,
  PROJECT_NAME_MAX_LENGTH,
  PROJECT_NAME_MIN_LENGTH,
  PRODUCT_LINE_IDS_ORDERED,
  PRODUCT_LINE_OPTIONS,
  projectBlockedReason,
  slugifyProjectName,
  TENANT_NAME_BLOCKED_REASONS,
  TENANT_NAME_MAX_LENGTH,
  TENANT_NAME_MIN_LENGTH,
  TENANT_UPDATE_ERROR_COPY,
  tenantNameBlockedReason,
} from "@/lib/auth/onboarding-helpers"

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Step vocabulary drift guard
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — step vocabulary", () => {
  it("ONBOARDING_STEP_KIND covers exactly 4 steps", () => {
    expect(Object.values(ONBOARDING_STEP_KIND)).toEqual([
      "tenant",
      "profile",
      "project",
      "celebrate",
    ])
  })

  it("ONBOARDING_STEPS_ORDERED matches the step kind tuple", () => {
    expect([...ONBOARDING_STEPS_ORDERED]).toEqual([
      "tenant",
      "profile",
      "project",
      "celebrate",
    ])
  })

  it("ONBOARDING_STEP_COPY pins title + summary + ctaLabel for every step", () => {
    for (const kind of ONBOARDING_STEPS_ORDERED) {
      const copy = ONBOARDING_STEP_COPY[kind]
      expect(copy.title.length).toBeGreaterThan(0)
      expect(copy.summary.length).toBeGreaterThan(0)
      expect(copy.ctaLabel.length).toBeGreaterThan(0)
    }
    expect(ONBOARDING_STEP_COPY.tenant.title).toBe("Confirm your workspace")
    expect(ONBOARDING_STEP_COPY.celebrate.title).toBe("You're all set")
  })

  it("ONBOARDING_STEP_COPY is frozen", () => {
    expect(Object.isFrozen(ONBOARDING_STEP_COPY)).toBe(true)
    expect(Object.isFrozen(ONBOARDING_STEP_COPY.tenant)).toBe(true)
  })

  it("ONBOARDING_DISPLAY_NAME_KEY is a namespaced literal", () => {
    expect(ONBOARDING_DISPLAY_NAME_KEY).toBe(
      "omnisight:onboarding:displayName",
    )
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Step resolver cascade
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — firstLoginRequiredStep", () => {
  const baseInput = {
    tenantName: "Acme",
    displayName: "Yi",
    hasProject: true,
    celebrated: false,
  }

  it("returns tenant when tenantName is null", () => {
    expect(
      firstLoginRequiredStep({ ...baseInput, tenantName: null }),
    ).toBe("tenant")
  })

  it("returns tenant when tenantName is whitespace only", () => {
    expect(
      firstLoginRequiredStep({ ...baseInput, tenantName: "   " }),
    ).toBe("tenant")
  })

  it("returns profile when displayName is empty / null", () => {
    expect(
      firstLoginRequiredStep({ ...baseInput, displayName: null }),
    ).toBe("profile")
    expect(
      firstLoginRequiredStep({ ...baseInput, displayName: "" }),
    ).toBe("profile")
  })

  it("returns project when hasProject is false", () => {
    expect(
      firstLoginRequiredStep({ ...baseInput, hasProject: false }),
    ).toBe("project")
  })

  it("returns celebrate when every gate is satisfied", () => {
    expect(firstLoginRequiredStep(baseInput)).toBe("celebrate")
  })

  it("cascade: tenant beats profile beats project", () => {
    expect(
      firstLoginRequiredStep({
        tenantName: null,
        displayName: null,
        hasProject: false,
        celebrated: false,
      }),
    ).toBe("tenant")
    expect(
      firstLoginRequiredStep({
        tenantName: "Acme",
        displayName: null,
        hasProject: false,
        celebrated: false,
      }),
    ).toBe("profile")
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Tenant-name gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — tenantNameBlockedReason", () => {
  it("TENANT_NAME_BLOCKED_REASONS pins the 4 reason strings", () => {
    expect([...TENANT_NAME_BLOCKED_REASONS]).toEqual([
      "busy",
      "locked",
      "name_too_short",
      "name_too_long",
    ])
  })

  it("returns busy when busy=true regardless of other inputs", () => {
    expect(
      tenantNameBlockedReason({
        busy: true,
        name: "Acme",
        canEdit: true,
      }),
    ).toBe("busy")
  })

  it("returns locked when canEdit=false", () => {
    expect(
      tenantNameBlockedReason({
        busy: false,
        name: "Acme",
        canEdit: false,
      }),
    ).toBe("locked")
  })

  it("returns name_too_short for short trimmed name", () => {
    expect(
      tenantNameBlockedReason({
        busy: false,
        name: " A ",
        canEdit: true,
      }),
    ).toBe("name_too_short")
  })

  it("returns name_too_long when over the cap", () => {
    expect(
      tenantNameBlockedReason({
        busy: false,
        name: "x".repeat(TENANT_NAME_MAX_LENGTH + 1),
        canEdit: true,
      }),
    ).toBe("name_too_long")
  })

  it("returns null when every gate clears", () => {
    expect(
      tenantNameBlockedReason({
        busy: false,
        name: "Acme",
        canEdit: true,
      }),
    ).toBeNull()
  })

  it("min/max constants are sane", () => {
    expect(TENANT_NAME_MIN_LENGTH).toBe(2)
    expect(TENANT_NAME_MAX_LENGTH).toBe(64)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Profile gate
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — profileBlockedReason", () => {
  it("PROFILE_BLOCKED_REASONS pins the 3 reason strings", () => {
    expect([...PROFILE_BLOCKED_REASONS]).toEqual([
      "busy",
      "display_name_too_short",
      "display_name_too_long",
    ])
  })

  it("returns busy first", () => {
    expect(
      profileBlockedReason({
        busy: true,
        displayName: "Yi Hsuan",
      }),
    ).toBe("busy")
  })

  it("returns display_name_too_short for empty / short", () => {
    expect(profileBlockedReason({ busy: false, displayName: "" })).toBe(
      "display_name_too_short",
    )
    expect(profileBlockedReason({ busy: false, displayName: "Y" })).toBe(
      "display_name_too_short",
    )
  })

  it("returns display_name_too_long over cap", () => {
    expect(
      profileBlockedReason({
        busy: false,
        displayName: "x".repeat(DISPLAY_NAME_MAX_LENGTH + 1),
      }),
    ).toBe("display_name_too_long")
  })

  it("returns null when valid", () => {
    expect(
      profileBlockedReason({ busy: false, displayName: "Yi" }),
    ).toBeNull()
  })

  it("min/max constants are sane", () => {
    expect(DISPLAY_NAME_MIN_LENGTH).toBe(2)
    expect(DISPLAY_NAME_MAX_LENGTH).toBe(64)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Project gate + product line catalog
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — product line catalog", () => {
  it("PRODUCT_LINE_IDS_ORDERED pins the backend ProductLine values", () => {
    expect([...PRODUCT_LINE_IDS_ORDERED]).toEqual([
      "embedded",
      "web",
      "mobile",
      "software",
      "custom",
    ])
  })

  it("PRODUCT_LINE_OPTIONS row count matches IDs tuple", () => {
    expect(PRODUCT_LINE_OPTIONS.length).toBe(PRODUCT_LINE_IDS_ORDERED.length)
  })

  it("each option has a non-empty label + summary + frozen", () => {
    for (const opt of PRODUCT_LINE_OPTIONS) {
      expect(PRODUCT_LINE_IDS_ORDERED).toContain(opt.id)
      expect(opt.label.length).toBeGreaterThan(0)
      expect(opt.summary.length).toBeGreaterThan(0)
      expect(Object.isFrozen(opt)).toBe(true)
    }
  })
})

describe("AS.7.8 onboarding-helpers — projectBlockedReason", () => {
  it("PROJECT_BLOCKED_REASONS pins the 4 reason strings", () => {
    expect([...PROJECT_BLOCKED_REASONS]).toEqual([
      "busy",
      "name_too_short",
      "name_too_long",
      "product_line_required",
    ])
  })

  it("returns busy first", () => {
    expect(
      projectBlockedReason({
        busy: true,
        name: "X",
        productLine: "web",
      }),
    ).toBe("busy")
  })

  it("returns name_too_short for empty / short trimmed name", () => {
    expect(
      projectBlockedReason({
        busy: false,
        name: " ",
        productLine: "web",
      }),
    ).toBe("name_too_short")
  })

  it("returns name_too_long over cap", () => {
    expect(
      projectBlockedReason({
        busy: false,
        name: "x".repeat(PROJECT_NAME_MAX_LENGTH + 1),
        productLine: "web",
      }),
    ).toBe("name_too_long")
  })

  it("returns product_line_required when null / unknown", () => {
    expect(
      projectBlockedReason({
        busy: false,
        name: "Lobby",
        productLine: null,
      }),
    ).toBe("product_line_required")
    expect(
      projectBlockedReason({
        busy: false,
        name: "Lobby",
        productLine: "drone",
      }),
    ).toBe("product_line_required")
  })

  it("returns null when every gate clears", () => {
    expect(
      projectBlockedReason({
        busy: false,
        name: "Lobby cameras",
        productLine: "embedded",
      }),
    ).toBeNull()
  })

  it("min/max constants are sane", () => {
    expect(PROJECT_NAME_MIN_LENGTH).toBe(2)
    expect(PROJECT_NAME_MAX_LENGTH).toBe(64)
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  slugifyProjectName
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — slugifyProjectName", () => {
  it("lowercases + replaces whitespace with dashes", () => {
    expect(slugifyProjectName("Lobby Cameras")).toBe("lobby-cameras")
  })

  it("collapses multiple separators", () => {
    expect(slugifyProjectName("  Lobby   _Cameras  ")).toBe("lobby-cameras")
  })

  it("strips non-alphanumeric characters", () => {
    expect(slugifyProjectName("L0b/by!Cameras✨")).toBe("l0bbycameras")
  })

  it("clamps to 64 characters", () => {
    expect(slugifyProjectName("x".repeat(80)).length).toBeLessThanOrEqual(64)
  })

  it("returns empty string for input that's purely punctuation", () => {
    expect(slugifyProjectName("!!!")).toBe("")
  })

  it("trims leading / trailing dashes", () => {
    expect(slugifyProjectName("---abc---")).toBe("abc")
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Error classifiers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — classifyTenantUpdateError", () => {
  it("ONBOARDING_ERROR_KIND covers 4 kinds", () => {
    expect(Object.values(ONBOARDING_ERROR_KIND)).toEqual([
      "invalid_input",
      "conflict",
      "rate_limited",
      "service_unavailable",
    ])
  })

  it("400 → invalid_input", () => {
    const out = classifyTenantUpdateError({ status: 400 })
    expect(out.kind).toBe("invalid_input")
    expect(out.message).toBe(TENANT_UPDATE_ERROR_COPY.invalid_input)
  })

  it("422 → invalid_input", () => {
    expect(classifyTenantUpdateError({ status: 422 }).kind).toBe(
      "invalid_input",
    )
  })

  it("409 → conflict", () => {
    expect(classifyTenantUpdateError({ status: 409 }).kind).toBe("conflict")
  })

  it("429 → rate_limited; retryAfter parsed", () => {
    const out = classifyTenantUpdateError({ status: 429, retryAfter: "30" })
    expect(out.kind).toBe("rate_limited")
    expect(out.retryAfterSeconds).toBe(30)
  })

  it("503 → service_unavailable", () => {
    expect(classifyTenantUpdateError({ status: 503 }).kind).toBe(
      "service_unavailable",
    )
  })

  it("null status → service_unavailable", () => {
    expect(classifyTenantUpdateError({ status: null }).kind).toBe(
      "service_unavailable",
    )
  })

  it("unknown 4xx → service_unavailable defensive default", () => {
    expect(classifyTenantUpdateError({ status: 418 }).kind).toBe(
      "service_unavailable",
    )
  })

  it("invalid retryAfter resolves to null", () => {
    expect(
      classifyTenantUpdateError({ status: 429, retryAfter: "garbage" })
        .retryAfterSeconds,
    ).toBeNull()
  })
})

describe("AS.7.8 onboarding-helpers — classifyProjectCreateError", () => {
  it("uses the project copy table (distinct from tenant copy)", () => {
    const out = classifyProjectCreateError({ status: 409 })
    expect(out.message).toBe(PROJECT_CREATE_ERROR_COPY.conflict)
    expect(out.message).not.toBe(TENANT_UPDATE_ERROR_COPY.conflict)
  })

  it("422 → invalid_input", () => {
    expect(classifyProjectCreateError({ status: 422 }).kind).toBe(
      "invalid_input",
    )
  })

  it("503 → service_unavailable", () => {
    expect(classifyProjectCreateError({ status: 503 }).kind).toBe(
      "service_unavailable",
    )
  })
})

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Celebration burst
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("AS.7.8 onboarding-helpers — celebration burst constants", () => {
  it("CELEBRATION_PARTICLE_COUNT pins the row spec value (30)", () => {
    expect(CELEBRATION_PARTICLE_COUNT).toBe(30)
  })

  it("CELEBRATION_PARTICLE_HUE_PALETTE has at least 6 distinct hues", () => {
    expect(CELEBRATION_PARTICLE_HUE_PALETTE.length).toBeGreaterThanOrEqual(6)
    expect(new Set(CELEBRATION_PARTICLE_HUE_PALETTE).size).toBe(
      CELEBRATION_PARTICLE_HUE_PALETTE.length,
    )
  })

  it("CELEBRATION_DURATION_BY_LEVEL is monotonic across levels", () => {
    const off = CELEBRATION_DURATION_BY_LEVEL.off
    const subtle = CELEBRATION_DURATION_BY_LEVEL.subtle
    const normal = CELEBRATION_DURATION_BY_LEVEL.normal
    const dramatic = CELEBRATION_DURATION_BY_LEVEL.dramatic
    expect(off).toBe(0)
    expect(subtle).toBe(0)
    expect(normal).toBeGreaterThan(0)
    expect(dramatic).toBeGreaterThan(normal)
  })

  it("CELEBRATION_DURATION_BY_LEVEL is frozen", () => {
    expect(Object.isFrozen(CELEBRATION_DURATION_BY_LEVEL)).toBe(true)
  })
})

describe("AS.7.8 onboarding-helpers — buildCelebrationParticles", () => {
  it("returns empty for off / subtle", () => {
    expect(buildCelebrationParticles("off")).toEqual([])
    expect(buildCelebrationParticles("subtle")).toEqual([])
  })

  it("returns 30 particles for normal / dramatic by default", () => {
    expect(buildCelebrationParticles("normal").length).toBe(30)
    expect(buildCelebrationParticles("dramatic").length).toBe(30)
  })

  it("respects custom count", () => {
    expect(buildCelebrationParticles("normal", 5).length).toBe(5)
    expect(buildCelebrationParticles("dramatic", 0).length).toBe(0)
  })

  it("returns deterministic byte-equal output across repeated calls", () => {
    const first = buildCelebrationParticles("normal")
    const second = buildCelebrationParticles("normal")
    expect(first).toEqual(second)
  })

  it("particles are frozen", () => {
    const particles = buildCelebrationParticles("dramatic", 3)
    for (const p of particles) {
      expect(Object.isFrozen(p)).toBe(true)
    }
  })

  it("dramatic particles travel further than normal", () => {
    const normal = buildCelebrationParticles("normal", 6)
    const dramatic = buildCelebrationParticles("dramatic", 6)
    const maxNormal = Math.max(...normal.map((p) => p.distancePx))
    const maxDramatic = Math.max(...dramatic.map((p) => p.distancePx))
    expect(maxDramatic).toBeGreaterThan(maxNormal)
  })

  it("each particle's hue is from the palette", () => {
    const particles = buildCelebrationParticles("dramatic")
    for (const p of particles) {
      expect(CELEBRATION_PARTICLE_HUE_PALETTE).toContain(p.hue)
    }
  })

  it("xPx / yPx are rounded to 3 decimals (cross-runner stability)", () => {
    const particles = buildCelebrationParticles("normal", 4)
    for (const p of particles) {
      // The rounded value's fractional part must have ≤ 3 digits.
      const fracX = Math.abs(p.xPx) - Math.floor(Math.abs(p.xPx))
      const fracY = Math.abs(p.yPx) - Math.floor(Math.abs(p.yPx))
      expect(Number(fracX.toFixed(3))).toBe(Number(fracX.toFixed(6)))
      expect(Number(fracY.toFixed(3))).toBe(Number(fracY.toFixed(6)))
    }
  })

  it("negative count clamps to 0", () => {
    expect(buildCelebrationParticles("normal", -5).length).toBe(0)
  })
})

describe("AS.7.8 onboarding-helpers — formatWelcomeAboard", () => {
  it("uses the trimmed display name when provided", () => {
    expect(formatWelcomeAboard("  Yi Hsuan  ")).toBe("Welcome aboard, Yi Hsuan")
  })

  it("falls back to the bare phrase when null / empty / whitespace", () => {
    expect(formatWelcomeAboard(null)).toBe("Welcome aboard")
    expect(formatWelcomeAboard("")).toBe("Welcome aboard")
    expect(formatWelcomeAboard("   ")).toBe("Welcome aboard")
  })
})
