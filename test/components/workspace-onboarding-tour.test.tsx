/**
 * V9 #3 (TODO row 2711, #325) — Contract tests for
 * `components/omnisight/workspace-onboarding-tour.tsx`.
 *
 * Covers:
 *   - Pure helpers (`onboardingStorageKey`, `clampStepIndex`,
 *     `progressPercent`, `resolveTourParam`).
 *   - Six-step canonical sequence (id order + per-locale copy presence).
 *   - Auto-trigger logic: opens on first visit (no storage flag), stays
 *     closed when flag is set, opens on `?tour=1` even when flag is set.
 *   - Step navigation: Next / Back, terminal Done, dot indicators.
 *   - Dismiss reasons: Skip → "skipped", final Done → "completed".
 *   - Persistence: dismissal writes the seen flag exactly once.
 *   - Per-workspace framework hint: appears on step 1 only, content
 *     differs per workspace type.
 *   - Locale propagation: zh-TW / ja copy renders when `localeOverride`
 *     is set.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import * as React from "react"

import {
  WorkspaceOnboardingTour,
  ONBOARDING_STEPS,
  CTA_LABELS,
  FRAMEWORK_HINTS,
  DIALOG_LABELS,
  onboardingStorageKey,
  clampStepIndex,
  progressPercent,
  resolveTourParam,
  type OnboardingStepId,
} from "@/components/omnisight/workspace-onboarding-tour"

// Radix `Dialog` mounts a focus-trap that calls `hasPointerCapture`
// while transitioning — both unimplemented in jsdom.
if (typeof Element !== "undefined") {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => undefined
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => undefined
  }
}

interface FakeStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
  removeItem?: (key: string) => void
  /** Inspection helper for tests. */
  _inner: Map<string, string>
}

function makeFakeStorage(seed: Record<string, string> = {}): FakeStorage {
  const inner = new Map(Object.entries(seed))
  return {
    _inner: inner,
    getItem: (k) => (inner.has(k) ? (inner.get(k) as string) : null),
    setItem: (k, v) => {
      inner.set(k, v)
    },
    removeItem: (k) => {
      inner.delete(k)
    },
  }
}

function makeFakeParams(record: Record<string, string> = {}): {
  get: (key: string) => string | null
} {
  return {
    get: (k) => (k in record ? record[k] : null),
  }
}

beforeEach(() => {
  // Strip any `?tour` param leaked from a previous test or from jsdom's
  // initial location parsing — we control that surface via searchParamsImpl.
  if (typeof window !== "undefined") {
    try {
      window.history.replaceState(null, "", "/")
    } catch {
      /* ignore */
    }
  }
})

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("WorkspaceOnboardingTour — pure helpers", () => {
  it("onboardingStorageKey is stable + per-type", () => {
    expect(onboardingStorageKey("web")).toBe("omnisight:workspace:web:onboarding-seen")
    expect(onboardingStorageKey("mobile")).toBe("omnisight:workspace:mobile:onboarding-seen")
    expect(onboardingStorageKey("software")).toBe(
      "omnisight:workspace:software:onboarding-seen",
    )
    // No two workspace types share a key.
    const keys = new Set(["web", "mobile", "software"].map((t) => onboardingStorageKey(t as never)))
    expect(keys.size).toBe(3)
  })

  it("clampStepIndex bounds the input into [0, n-1]", () => {
    expect(clampStepIndex(-5)).toBe(0)
    expect(clampStepIndex(0)).toBe(0)
    expect(clampStepIndex(3)).toBe(3)
    expect(clampStepIndex(ONBOARDING_STEPS.length - 1)).toBe(ONBOARDING_STEPS.length - 1)
    expect(clampStepIndex(99)).toBe(ONBOARDING_STEPS.length - 1)
    // Non-finite input falls to 0 instead of NaN propagating.
    expect(clampStepIndex(Number.NaN)).toBe(0)
    expect(clampStepIndex(Number.POSITIVE_INFINITY)).toBe(0)
    // Float input is floored, not rounded.
    expect(clampStepIndex(2.7)).toBe(2)
  })

  it("progressPercent is monotone and ends at 100", () => {
    const pcts = ONBOARDING_STEPS.map((_, i) => progressPercent(i))
    for (let i = 1; i < pcts.length; i++) {
      expect(pcts[i]).toBeGreaterThan(pcts[i - 1])
    }
    expect(pcts[pcts.length - 1]).toBe(100)
    // Out-of-range still yields a sensible percentage.
    expect(progressPercent(99)).toBe(100)
    expect(progressPercent(-99)).toBe(progressPercent(0))
  })

  it("resolveTourParam supports id, 1-based int, and null", () => {
    // Step ids round-trip to their index.
    ONBOARDING_STEPS.forEach((s, i) => {
      expect(resolveTourParam(s.id)).toBe(i)
    })
    // Numeric 1-based.
    expect(resolveTourParam("1")).toBe(0)
    expect(resolveTourParam("6")).toBe(5)
    // Out of range still triggers the tour at step 0 (don't strand the user).
    expect(resolveTourParam("999")).toBe(0)
    expect(resolveTourParam("0")).toBe(0)
    // Truthy non-numeric values trigger at step 0.
    expect(resolveTourParam("yes")).toBe(0)
    // Empty / null / undefined all suppress.
    expect(resolveTourParam("")).toBeNull()
    expect(resolveTourParam(null)).toBeNull()
    expect(resolveTourParam(undefined)).toBeNull()
  })
})

// ─── Step catalogue invariants ─────────────────────────────────────────────

describe("WorkspaceOnboardingTour — step catalogue", () => {
  it("has exactly six steps in the contractual order", () => {
    expect(ONBOARDING_STEPS).toHaveLength(6)
    const ids: OnboardingStepId[] = ONBOARDING_STEPS.map((s) => s.id)
    expect(ids).toEqual(["framework", "describe", "ai-work", "preview", "annotate", "deploy"])
  })

  it("every step has copy in every supported locale", () => {
    const locales = ["en", "zh-TW", "zh-CN", "ja"] as const
    for (const step of ONBOARDING_STEPS) {
      for (const loc of locales) {
        const c = step.copy[loc]
        expect(c, `step=${step.id} locale=${loc}`).toBeDefined()
        expect(c.title.length).toBeGreaterThan(0)
        expect(c.body.length).toBeGreaterThan(0)
      }
    }
  })

  it("CTA labels and dialog chrome cover all locales", () => {
    for (const loc of ["en", "zh-TW", "zh-CN", "ja"] as const) {
      expect(CTA_LABELS[loc].next.length).toBeGreaterThan(0)
      expect(CTA_LABELS[loc].back.length).toBeGreaterThan(0)
      expect(CTA_LABELS[loc].skip.length).toBeGreaterThan(0)
      expect(CTA_LABELS[loc].done.length).toBeGreaterThan(0)
      expect(DIALOG_LABELS[loc].ariaLabel.length).toBeGreaterThan(0)
    }
  })

  it("framework hint differs per workspace type", () => {
    const webHint = FRAMEWORK_HINTS.web.en
    const mobileHint = FRAMEWORK_HINTS.mobile.en
    const softwareHint = FRAMEWORK_HINTS.software.en
    expect(webHint).not.toBe(mobileHint)
    expect(mobileHint).not.toBe(softwareHint)
    expect(softwareHint).not.toBe(webHint)
    expect(webHint).toMatch(/Next\.js|shadcn/i)
    expect(mobileHint).toMatch(/iOS|Android|Flutter/i)
    expect(softwareHint).toMatch(/Python|TypeScript|Go|Rust/i)
  })
})

// ─── Auto-trigger logic ────────────────────────────────────────────────────

describe("WorkspaceOnboardingTour — auto-trigger", () => {
  it("opens automatically when no seen flag is set", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(screen.getByTestId("workspace-onboarding-dialog")).toBeInTheDocument()
    expect(
      screen.getByTestId("workspace-onboarding-step-title").textContent,
    ).toMatch(/1 \/ 6/)
    // data-step-id stamp lets parents grep for current step.
    expect(
      screen.getByTestId("workspace-onboarding-dialog").getAttribute("data-step-id"),
    ).toBe("framework")
  })

  it("stays closed when seen flag is already set", () => {
    const storage = makeFakeStorage({
      [onboardingStorageKey("web")]: "1",
    })
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(screen.queryByTestId("workspace-onboarding-dialog")).not.toBeInTheDocument()
  })

  it("autoShow=false suppresses first-visit auto-trigger", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        autoShow={false}
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(screen.queryByTestId("workspace-onboarding-dialog")).not.toBeInTheDocument()
  })

  it("?tour=1 forces the tour open even when seen flag is set", () => {
    const storage = makeFakeStorage({
      [onboardingStorageKey("mobile")]: "1",
    })
    render(
      <WorkspaceOnboardingTour
        type="mobile"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams({ tour: "1" })}
        localeOverride="en"
      />,
    )
    expect(screen.getByTestId("workspace-onboarding-dialog")).toBeInTheDocument()
  })

  it("?tour=<step-id> jumps to that step", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="software"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams({ tour: "preview" })}
        localeOverride="en"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-dialog").getAttribute("data-step-id"),
    ).toBe("preview")
    expect(
      screen.getByTestId("workspace-onboarding-step-title").textContent,
    ).toMatch(/4 \/ 6/)
  })
})

// ─── Step navigation ───────────────────────────────────────────────────────

describe("WorkspaceOnboardingTour — step navigation", () => {
  it("Next advances; Back decrements; Back disabled at step 1", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    const dialog = screen.getByTestId("workspace-onboarding-dialog")
    const next = screen.getByTestId("workspace-onboarding-next")
    const back = screen.getByTestId("workspace-onboarding-back")

    expect(back).toBeDisabled()
    expect(dialog.getAttribute("data-step-index")).toBe("0")

    fireEvent.click(next)
    expect(dialog.getAttribute("data-step-index")).toBe("1")
    expect(back).not.toBeDisabled()

    fireEvent.click(next)
    expect(dialog.getAttribute("data-step-index")).toBe("2")

    fireEvent.click(back)
    expect(dialog.getAttribute("data-step-index")).toBe("1")
  })

  it("Next on the final step closes with reason=completed and persists seen", () => {
    const storage = makeFakeStorage()
    const onClose = vi.fn()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
        onClose={onClose}
      />,
    )
    // Click Next 5 times to reach the final (index 5) step.
    const next = screen.getByTestId("workspace-onboarding-next")
    for (let i = 0; i < ONBOARDING_STEPS.length - 1; i++) {
      fireEvent.click(next)
    }
    expect(
      screen.getByTestId("workspace-onboarding-dialog").getAttribute("data-step-index"),
    ).toBe(String(ONBOARDING_STEPS.length - 1))
    // Last step Next acts as Done — fires completed + persists.
    fireEvent.click(next)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledWith("completed")
    expect(storage.getItem(onboardingStorageKey("web"))).toBe("1")
    expect(screen.queryByTestId("workspace-onboarding-dialog")).not.toBeInTheDocument()
  })

  it("Skip closes with reason=skipped and persists seen", () => {
    const storage = makeFakeStorage()
    const onClose = vi.fn()
    render(
      <WorkspaceOnboardingTour
        type="mobile"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
        onClose={onClose}
      />,
    )
    fireEvent.click(screen.getByTestId("workspace-onboarding-skip"))
    expect(onClose).toHaveBeenCalledWith("skipped")
    expect(storage.getItem(onboardingStorageKey("mobile"))).toBe("1")
    expect(screen.queryByTestId("workspace-onboarding-dialog")).not.toBeInTheDocument()
  })

  it("step dots reflect active + done state", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="software"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    const next = screen.getByTestId("workspace-onboarding-next")
    fireEvent.click(next) // step 1
    fireEvent.click(next) // step 2
    expect(
      screen
        .getByTestId("workspace-onboarding-step-dot-framework")
        .getAttribute("data-done"),
    ).toBe("true")
    expect(
      screen
        .getByTestId("workspace-onboarding-step-dot-describe")
        .getAttribute("data-done"),
    ).toBe("true")
    expect(
      screen
        .getByTestId("workspace-onboarding-step-dot-ai-work")
        .getAttribute("data-active"),
    ).toBe("true")
    expect(
      screen
        .getByTestId("workspace-onboarding-step-dot-deploy")
        .getAttribute("data-done"),
    ).toBe("false")
  })

  it("progress readout matches step 1-based index", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-progress-readout").textContent,
    ).toMatch(/^\s*1\s*\/\s*6\s*$/)
    fireEvent.click(screen.getByTestId("workspace-onboarding-next"))
    expect(
      screen.getByTestId("workspace-onboarding-progress-readout").textContent,
    ).toMatch(/^\s*2\s*\/\s*6\s*$/)
  })
})

// ─── Per-workspace framework hint ──────────────────────────────────────────

describe("WorkspaceOnboardingTour — framework hint", () => {
  it("shows on step 1 only", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-framework-hint"),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("workspace-onboarding-next"))
    expect(
      screen.queryByTestId("workspace-onboarding-framework-hint"),
    ).not.toBeInTheDocument()
  })

  it("renders the per-type framework copy", () => {
    const storage = makeFakeStorage()
    const { unmount } = render(
      <WorkspaceOnboardingTour
        type="mobile"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-framework-hint").textContent,
    ).toMatch(/iOS|Android|Flutter|React Native/i)
    unmount()

    const storage2 = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="software"
        storageImpl={storage2}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-framework-hint").textContent,
    ).toMatch(/Python|TypeScript|Go|Rust/i)
  })
})

// ─── Locale propagation ───────────────────────────────────────────────────

describe("WorkspaceOnboardingTour — locale", () => {
  it("renders zh-TW copy when localeOverride='zh-TW'", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="zh-TW"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-step-title").textContent,
    ).toContain("選擇框架")
    // CTA label localised too.
    expect(screen.getByTestId("workspace-onboarding-skip").textContent).toContain("跳過")
  })

  it("renders ja copy when localeOverride='ja'", () => {
    const storage = makeFakeStorage()
    render(
      <WorkspaceOnboardingTour
        type="web"
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="ja"
      />,
    )
    expect(
      screen.getByTestId("workspace-onboarding-step-title").textContent,
    ).toContain("フレームワーク")
  })
})

// ─── forceOpen test seam ───────────────────────────────────────────────────

describe("WorkspaceOnboardingTour — forceOpen", () => {
  it("forceOpen ignores storage flag and opens immediately", () => {
    const storage = makeFakeStorage({
      [onboardingStorageKey("web")]: "1",
    })
    render(
      <WorkspaceOnboardingTour
        type="web"
        forceOpen
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
      />,
    )
    expect(screen.getByTestId("workspace-onboarding-dialog")).toBeInTheDocument()
  })

  it("forceOpen does NOT persist a seen flag on dismissal", () => {
    const storage = makeFakeStorage()
    const onClose = vi.fn()
    render(
      <WorkspaceOnboardingTour
        type="software"
        forceOpen
        storageImpl={storage}
        searchParamsImpl={makeFakeParams()}
        localeOverride="en"
        onClose={onClose}
      />,
    )
    fireEvent.click(screen.getByTestId("workspace-onboarding-skip"))
    expect(onClose).toHaveBeenCalledWith("skipped")
    // forceOpen suppresses persistence so tests don't leave state behind.
    expect(storage.getItem(onboardingStorageKey("software"))).toBeNull()
  })
})
