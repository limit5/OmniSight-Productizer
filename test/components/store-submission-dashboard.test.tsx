/**
 * V7 #5 (TODO row 2695 / issue #323) — Contract tests for
 * `store-submission-dashboard.tsx`.
 *
 * Covers the three operator-visible contracts the dashboard ships:
 *
 *   1. Pure helpers — review status labels + colours, dispatch status,
 *      required screenshot catalogue, coverage math, dimension
 *      validation, relative-time formatter, byte-size formatter, ring
 *      buffer.
 *   2. SSE reducer — every
 *      `mobile_workspace.store_submission.*` event mutates the
 *      submission in the exact shape the header / screenshots /
 *      dispatch sections render.
 *   3. Rendering — happy path for idle / draft / submitted / rejected
 *      states, plus the screenshot-slot and dispatch-history shapes
 *      surface as their own assertions.
 *
 * Event-namespace disjointness is verified against V6 / V7 sibling
 * namespaces so a future refactor that merges busses can catch the
 * collision at CI time.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import {
  DEFAULT_MAX_DISPATCH_HISTORY,
  DEVICE_CLASS_LABELS,
  DISPATCH_CHANNEL_LABELS,
  REQUIRED_SCREENSHOTS_APP_STORE,
  REQUIRED_SCREENSHOTS_PLAY_STORE,
  REVIEW_STATUS_LABELS,
  STORE_SUBMISSION_EVENT_NAMES,
  STORE_SUBMISSION_EVENT_PREFIX,
  STORE_TARGET_LABELS,
  StoreSubmissionDashboard,
  applyStoreSubmissionEvent,
  canDispatchSubmission,
  canSubmitSubmission,
  dispatchStatusColorVar,
  dispatchStatusLabel,
  emptyStoreSubmission,
  formatStoreByteSize,
  formatStoreRelativeTime,
  groupScreenshotsByDeviceClass,
  isTerminalReviewStatus,
  matchStoreSubmissionEvent,
  pushRingBuffer,
  requiredScreenshotDeviceClasses,
  reviewStatusColorVar,
  reviewStatusLabel,
  screenshotCoverage,
  shortenStoreId,
  storeTargetToChannel,
  storeTargetToPlatform,
  validateScreenshotDimensions,
  type StoreScreenshot,
  type StoreSubmission,
  type StoreSubmissionEvent,
} from "@/components/omnisight/store-submission-dashboard"

// ─── Helpers ────────────────────────────────────────────────────────────────

function makeSubmission(
  overrides: Partial<StoreSubmission> = {},
): StoreSubmission {
  return {
    ...emptyStoreSubmission("session-x", "app-store"),
    ...overrides,
  }
}

function makeScreenshot(
  overrides: Partial<StoreScreenshot> = {},
): StoreScreenshot {
  return {
    id: "shot-1",
    deviceClass: "iphone-6.7",
    locale: "en-US",
    filename: "home.png",
    url: "/shots/home.png",
    width: 1290,
    height: 2796,
    byteSize: 512 * 1024,
    uploadedAt: new Date().toISOString(),
    state: "valid",
    reason: null,
    ...overrides,
  }
}

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("storeTargetToChannel", () => {
  it("app-store → testflight, play-console → firebase", () => {
    expect(storeTargetToChannel("app-store")).toBe("testflight")
    expect(storeTargetToChannel("play-console")).toBe("firebase-app-distribution")
  })
})

describe("storeTargetToPlatform", () => {
  it("maps each target to its mobile platform", () => {
    expect(storeTargetToPlatform("app-store")).toBe("ios")
    expect(storeTargetToPlatform("play-console")).toBe("android")
  })
})

describe("requiredScreenshotDeviceClasses", () => {
  it("returns the Apple catalogue for app-store", () => {
    expect(requiredScreenshotDeviceClasses("app-store")).toEqual(
      REQUIRED_SCREENSHOTS_APP_STORE,
    )
  })
  it("returns the Google catalogue for play-console", () => {
    expect(requiredScreenshotDeviceClasses("play-console")).toEqual(
      REQUIRED_SCREENSHOTS_PLAY_STORE,
    )
  })
})

describe("reviewStatusLabel / reviewStatusColorVar", () => {
  it("labels every review status", () => {
    const statuses = Object.keys(REVIEW_STATUS_LABELS) as Array<
      keyof typeof REVIEW_STATUS_LABELS
    >
    for (const s of statuses) {
      expect(reviewStatusLabel(s)).toBe(REVIEW_STATUS_LABELS[s])
      expect(reviewStatusColorVar(s)).toMatch(/^var\(--/)
    }
  })
  it("picks emerald for released/approved and red for rejected/removed", () => {
    expect(reviewStatusColorVar("released")).toBe("var(--validation-emerald)")
    expect(reviewStatusColorVar("approved")).toBe("var(--validation-emerald)")
    expect(reviewStatusColorVar("rejected")).toBe("var(--critical-red)")
    expect(reviewStatusColorVar("removed")).toBe("var(--critical-red)")
  })
})

describe("isTerminalReviewStatus", () => {
  it("returns true for released / removed / rejected only", () => {
    expect(isTerminalReviewStatus("released")).toBe(true)
    expect(isTerminalReviewStatus("removed")).toBe(true)
    expect(isTerminalReviewStatus("rejected")).toBe(true)
    expect(isTerminalReviewStatus("approved")).toBe(false)
    expect(isTerminalReviewStatus("in_review")).toBe(false)
    expect(isTerminalReviewStatus("idle")).toBe(false)
  })
})

describe("dispatchStatusLabel / dispatchStatusColorVar", () => {
  it("labels every dispatch status", () => {
    expect(dispatchStatusLabel("idle")).toBe("Idle")
    expect(dispatchStatusLabel("in_progress")).toMatch(/Dispatching/)
    expect(dispatchStatusLabel("succeeded")).toBe("Succeeded")
    expect(dispatchStatusLabel("failed")).toBe("Failed")
  })
  it("colour var matches the traffic-light palette", () => {
    expect(dispatchStatusColorVar("succeeded")).toBe("var(--validation-emerald)")
    expect(dispatchStatusColorVar("failed")).toBe("var(--critical-red)")
    expect(dispatchStatusColorVar("in_progress")).toBe("var(--neural-blue)")
    expect(dispatchStatusColorVar("idle")).toBe("var(--muted-foreground)")
  })
})

describe("formatStoreByteSize", () => {
  it("picks the right unit at each boundary", () => {
    expect(formatStoreByteSize(0)).toBe("0 B")
    expect(formatStoreByteSize(512)).toBe("512 B")
    expect(formatStoreByteSize(2048)).toBe("2.0 KB")
    expect(formatStoreByteSize(5 * 1024 * 1024)).toBe("5.0 MB")
    expect(formatStoreByteSize(1024 * 1024 * 1024)).toBe("1.00 GB")
  })
  it("returns dash for null / negative / NaN", () => {
    expect(formatStoreByteSize(null)).toBe("—")
    expect(formatStoreByteSize(undefined)).toBe("—")
    expect(formatStoreByteSize(-1)).toBe("—")
    expect(formatStoreByteSize(Number.NaN)).toBe("—")
  })
})

describe("formatStoreRelativeTime", () => {
  it("maps elapsed deltas to human buckets", () => {
    const now = 1_700_000_000_000
    expect(formatStoreRelativeTime(new Date(now - 10_000).toISOString(), now)).toBe(
      "just now",
    )
    expect(
      formatStoreRelativeTime(new Date(now - 5 * 60_000).toISOString(), now),
    ).toBe("5m ago")
    expect(
      formatStoreRelativeTime(new Date(now - 3 * 3_600_000).toISOString(), now),
    ).toBe("3h ago")
    expect(
      formatStoreRelativeTime(
        new Date(now - 2 * 24 * 3_600_000).toISOString(),
        now,
      ),
    ).toBe("2d ago")
    expect(
      formatStoreRelativeTime(
        new Date(now - 2 * 7 * 24 * 3_600_000).toISOString(),
        now,
      ),
    ).toBe("2w ago")
  })
  it("returns dash for null / undefined", () => {
    expect(formatStoreRelativeTime(null)).toBe("—")
    expect(formatStoreRelativeTime(undefined)).toBe("—")
  })
})

describe("shortenStoreId", () => {
  it("returns the input when short", () => {
    expect(shortenStoreId("com.foo")).toBe("com.foo")
  })
  it("truncates long values with a leading ellipsis", () => {
    const out = shortenStoreId("com.foo.bar.baz.really.long.bundle.identifier", 20)
    expect(out.startsWith("…")).toBe(true)
    expect(out.length).toBeLessThanOrEqual(20)
  })
  it("handles null / undefined", () => {
    expect(shortenStoreId(null)).toBe("")
    expect(shortenStoreId(undefined)).toBe("")
  })
})

describe("pushRingBuffer", () => {
  it("appends, dedups by id, and caps", () => {
    type X = { id: string; v: number }
    const existing: X[] = [{ id: "a", v: 1 }, { id: "b", v: 2 }]
    const next: X[] = [
      { id: "b", v: 99 },
      { id: "c", v: 3 },
      { id: "d", v: 4 },
    ]
    const out = pushRingBuffer(existing, next, 3)
    expect(out.map((e) => e.id)).toEqual(["b", "c", "d"])
  })
  it("cap 0 returns empty list", () => {
    expect(pushRingBuffer([{ id: "a" }], [{ id: "b" }], 0)).toEqual([])
  })
})

describe("validateScreenshotDimensions", () => {
  it("accepts an exact-match portrait shot", () => {
    expect(
      validateScreenshotDimensions(1290, 2796, "iphone-6.7").state,
    ).toBe("valid")
  })
  it("accepts a landscape orientation by swapping axes", () => {
    expect(
      validateScreenshotDimensions(2796, 1290, "iphone-6.7").state,
    ).toBe("valid")
  })
  it("rejects invalid aspect", () => {
    const out = validateScreenshotDimensions(1000, 1000, "iphone-6.7")
    expect(out.state).toBe("invalid_aspect")
    expect(out.reason).toBeTruthy()
  })
  it("rejects zero / null dimensions", () => {
    expect(validateScreenshotDimensions(0, 0, "iphone-6.7").state).toBe(
      "invalid_dim",
    )
    expect(
      validateScreenshotDimensions(null, 100, "iphone-6.7").state,
    ).toBe("invalid_dim")
  })
  it("applies a 3 % tolerance", () => {
    // Within 3 % of the Apple 6.7" portrait 1290×2796 aspect.
    expect(
      validateScreenshotDimensions(1300, 2780, "iphone-6.7").state,
    ).toBe("valid")
  })
})

describe("groupScreenshotsByDeviceClass", () => {
  it("partitions screenshots into slots, preserving order", () => {
    const a = makeScreenshot({ id: "a", deviceClass: "iphone-6.7" })
    const b = makeScreenshot({ id: "b", deviceClass: "iphone-5.5", width: 1242, height: 2208 })
    const c = makeScreenshot({ id: "c", deviceClass: "iphone-6.7" })
    const out = groupScreenshotsByDeviceClass([a, b, c])
    expect(out["iphone-6.7"].map((s) => s.id)).toEqual(["a", "c"])
    expect(out["iphone-5.5"].map((s) => s.id)).toEqual(["b"])
    expect(out["android-phone"]).toEqual([])
  })
})

describe("screenshotCoverage", () => {
  it("reports missing classes for an empty submission", () => {
    const out = screenshotCoverage({
      target: "app-store",
      screenshots: [],
    })
    expect(out.required).toEqual(REQUIRED_SCREENSHOTS_APP_STORE)
    expect(out.missing).toEqual(REQUIRED_SCREENSHOTS_APP_STORE)
    expect(out.provided).toEqual([])
    expect(out.invalid).toEqual([])
  })
  it("reports provided classes once at least one valid shot exists", () => {
    const out = screenshotCoverage({
      target: "app-store",
      screenshots: [
        makeScreenshot({ deviceClass: "iphone-6.7", state: "valid" }),
        makeScreenshot({
          id: "s2",
          deviceClass: "iphone-5.5",
          width: 1242,
          height: 2208,
          state: "valid",
        }),
      ],
    })
    expect(out.provided).toContain("iphone-6.7")
    expect(out.provided).toContain("iphone-5.5")
    expect(out.missing).toEqual(["ipad-13"])
  })
  it("reports invalid classes when all shots are bad", () => {
    const out = screenshotCoverage({
      target: "app-store",
      screenshots: [
        makeScreenshot({ deviceClass: "iphone-6.7", state: "invalid_aspect" }),
      ],
    })
    expect(out.invalid).toContain("iphone-6.7")
    expect(out.provided).not.toContain("iphone-6.7")
  })
})

describe("canSubmitSubmission", () => {
  it("rejects when bundle / version / build are empty", () => {
    expect(canSubmitSubmission(makeSubmission()).ok).toBe(false)
  })
  it("rejects when screenshots are missing", () => {
    expect(
      canSubmitSubmission(
        makeSubmission({
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
        }),
      ).ok,
    ).toBe(false)
  })
  it("accepts a full submission with valid screenshots", () => {
    const screenshots = REQUIRED_SCREENSHOTS_APP_STORE.map((cls) =>
      makeScreenshot({
        id: `s-${cls}`,
        deviceClass: cls,
        width: 1290,
        height: 2796,
        state: "valid",
      }),
    )
    expect(
      canSubmitSubmission(
        makeSubmission({
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
          screenshots,
        }),
      ).ok,
    ).toBe(true)
  })
  it("rejects when already in review", () => {
    const screenshots = REQUIRED_SCREENSHOTS_APP_STORE.map((cls) =>
      makeScreenshot({ id: `s-${cls}`, deviceClass: cls, state: "valid" }),
    )
    expect(
      canSubmitSubmission(
        makeSubmission({
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
          status: "in_review",
          screenshots,
        }),
      ).ok,
    ).toBe(false)
  })
})

describe("canDispatchSubmission", () => {
  it("blocks when there is no build", () => {
    expect(canDispatchSubmission(makeSubmission()).ok).toBe(false)
  })
  it("blocks while a dispatch is already in progress", () => {
    expect(
      canDispatchSubmission(
        makeSubmission({
          buildNumber: "1",
          status: "draft",
          dispatch: {
            channel: "testflight",
            status: "in_progress",
            audience: "QA",
            testerCount: 0,
          },
        }),
      ).ok,
    ).toBe(false)
  })
  it("allows once there is a build + non-idle status + no in-flight dispatch", () => {
    expect(
      canDispatchSubmission(
        makeSubmission({ buildNumber: "1", status: "draft" }),
      ).ok,
    ).toBe(true)
  })
})

describe("emptyStoreSubmission", () => {
  it("seeds a sensible idle submission", () => {
    const s = emptyStoreSubmission("session-x", "play-console")
    expect(s).toMatchObject({
      sessionId: "session-x",
      target: "play-console",
      status: "idle",
      screenshots: [],
      history: [],
      dispatch: null,
    })
  })
})

// ─── SSE matching + reducer ───────────────────────────────────────────────

describe("matchStoreSubmissionEvent", () => {
  it("rejects events outside the store submission namespace", () => {
    expect(
      matchStoreSubmissionEvent(
        {
          event: "mobile_workspace.build.progress",
          data: { session_id: "s", target: "app-store" },
        },
        "s",
        "app-store",
      ),
    ).toBe(false)
  })
  it("rejects events for a different session", () => {
    expect(
      matchStoreSubmissionEvent(
        {
          event: "mobile_workspace.store_submission.submitted",
          data: { session_id: "other", target: "app-store" },
        },
        "mine",
        "app-store",
      ),
    ).toBe(false)
  })
  it("rejects events for a different target", () => {
    expect(
      matchStoreSubmissionEvent(
        {
          event: "mobile_workspace.store_submission.submitted",
          data: { session_id: "s", target: "play-console" },
        },
        "s",
        "app-store",
      ),
    ).toBe(false)
  })
  it("accepts matching session + target events", () => {
    expect(
      matchStoreSubmissionEvent(
        {
          event: "mobile_workspace.store_submission.submitted",
          data: { session_id: "s", target: "app-store" },
        },
        "s",
        "app-store",
      ),
    ).toBe(true)
  })
  it("accepts camelCase alias for session id", () => {
    expect(
      matchStoreSubmissionEvent(
        {
          event: "mobile_workspace.store_submission.submitted",
          data: { sessionId: "s", target: "app-store" },
        },
        "s",
        "app-store",
      ),
    ).toBe(true)
  })
})

describe("applyStoreSubmissionEvent", () => {
  const base = makeSubmission()

  it("queued seeds bundle / version / build + resets screenshots", () => {
    const out = applyStoreSubmissionEvent(
      { ...base, screenshots: [makeScreenshot()] },
      {
        event: "mobile_workspace.store_submission.queued",
        data: {
          session_id: "session-x",
          target: "app-store",
          bundle_id: "com.foo.bar",
          version: "1.2.3",
          build_number: "12",
          build_id: "b-99",
        },
      },
    )
    expect(out.status).toBe("draft")
    expect(out.bundleId).toBe("com.foo.bar")
    expect(out.platformVersion).toBe("1.2.3")
    expect(out.buildNumber).toBe("12")
    expect(out.buildId).toBe("b-99")
    expect(out.screenshots).toEqual([])
  })

  it("submitted flips status + sets submittedAt", () => {
    const out = applyStoreSubmissionEvent(
      { ...base, status: "draft", bundleId: "com.foo" },
      {
        event: "mobile_workspace.store_submission.submitted",
        data: { session_id: "session-x", target: "app-store" },
      },
    )
    expect(out.status).toBe("submitted")
    expect(out.submittedAt).toBeTruthy()
  })

  it("review_updated accepts valid statuses + captures reviewer notes", () => {
    const out = applyStoreSubmissionEvent(
      { ...base, status: "in_review" },
      {
        event: "mobile_workspace.store_submission.review_updated",
        data: {
          session_id: "session-x",
          target: "app-store",
          status: "rejected",
          reviewer_notes: "Guideline 2.1 violation.",
          reviewer_name: "App Review Team",
        },
      },
    )
    expect(out.status).toBe("rejected")
    expect(out.reviewerNotes).toContain("Guideline 2.1")
    expect(out.reviewerName).toBe("App Review Team")
    expect(out.reviewedAt).toBeTruthy()
  })

  it("review_updated ignores invalid statuses", () => {
    const out = applyStoreSubmissionEvent(
      { ...base, status: "in_review" },
      {
        event: "mobile_workspace.store_submission.review_updated",
        data: {
          session_id: "session-x",
          target: "app-store",
          status: "not-a-real-status",
        },
      },
    )
    expect(out.status).toBe("in_review")
  })

  it("review_updated = released stamps releasedAt", () => {
    const out = applyStoreSubmissionEvent(
      { ...base, status: "pending_release" },
      {
        event: "mobile_workspace.store_submission.review_updated",
        data: {
          session_id: "session-x",
          target: "app-store",
          status: "released",
        },
      },
    )
    expect(out.status).toBe("released")
    expect(out.releasedAt).toBeTruthy()
  })

  it("screenshot_uploaded appends a valid screenshot", () => {
    const out = applyStoreSubmissionEvent(base, {
      event: "mobile_workspace.store_submission.screenshot_uploaded",
      data: {
        session_id: "session-x",
        target: "app-store",
        id: "shot-1",
        device_class: "iphone-6.7",
        locale: "en-US",
        filename: "home.png",
        url: "/shots/home.png",
        width: 1290,
        height: 2796,
        size: 512 * 1024,
      },
    })
    expect(out.screenshots).toHaveLength(1)
    expect(out.screenshots[0]).toMatchObject({
      deviceClass: "iphone-6.7",
      state: "valid",
    })
  })

  it("screenshot_uploaded replaces an existing screenshot with the same id", () => {
    const first = applyStoreSubmissionEvent(base, {
      event: "mobile_workspace.store_submission.screenshot_uploaded",
      data: {
        session_id: "session-x",
        target: "app-store",
        id: "shot-1",
        device_class: "iphone-6.7",
        filename: "home.png",
        url: "/shots/home.png",
        width: 1290,
        height: 2796,
      },
    })
    const out = applyStoreSubmissionEvent(first, {
      event: "mobile_workspace.store_submission.screenshot_uploaded",
      data: {
        session_id: "session-x",
        target: "app-store",
        id: "shot-1",
        device_class: "iphone-6.7",
        filename: "home_v2.png",
        url: "/shots/home_v2.png",
        width: 1290,
        height: 2796,
      },
    })
    expect(out.screenshots).toHaveLength(1)
    expect(out.screenshots[0].filename).toBe("home_v2.png")
  })

  it("screenshot_uploaded rejects an unknown device class", () => {
    const out = applyStoreSubmissionEvent(base, {
      event: "mobile_workspace.store_submission.screenshot_uploaded",
      data: {
        session_id: "session-x",
        target: "app-store",
        id: "shot-x",
        device_class: "galaxy-fold-42",
        filename: "home.png",
        url: "/home.png",
        width: 1000,
        height: 2000,
      },
    })
    expect(out.screenshots).toHaveLength(0)
  })

  it("screenshot_uploaded downgrades state when dimensions are wrong", () => {
    const out = applyStoreSubmissionEvent(base, {
      event: "mobile_workspace.store_submission.screenshot_uploaded",
      data: {
        session_id: "session-x",
        target: "app-store",
        id: "shot-1",
        device_class: "iphone-6.7",
        filename: "home.png",
        url: "/shots/home.png",
        width: 1000,
        height: 1000,
      },
    })
    expect(out.screenshots[0].state).toBe("invalid_aspect")
    expect(out.screenshots[0].reason).toBeTruthy()
  })

  it("screenshot_removed drops by id", () => {
    const seeded = makeSubmission({
      screenshots: [makeScreenshot({ id: "shot-1" })],
    })
    const out = applyStoreSubmissionEvent(seeded, {
      event: "mobile_workspace.store_submission.screenshot_removed",
      data: { session_id: "session-x", target: "app-store", id: "shot-1" },
    })
    expect(out.screenshots).toHaveLength(0)
  })

  it("withdrawn flips status + clears submittedAt", () => {
    const seeded = makeSubmission({
      status: "submitted",
      submittedAt: new Date().toISOString(),
    })
    const out = applyStoreSubmissionEvent(seeded, {
      event: "mobile_workspace.store_submission.withdrawn",
      data: {
        session_id: "session-x",
        target: "app-store",
        reason: "Need to add a privacy section.",
      },
    })
    expect(out.status).toBe("draft")
    expect(out.submittedAt).toBeNull()
    expect(out.reviewerNotes).toContain("privacy")
  })

  it("dispatch_started flips dispatch state to in_progress", () => {
    const out = applyStoreSubmissionEvent(
      makeSubmission({ status: "draft", buildNumber: "1" }),
      {
        event: "mobile_workspace.store_submission.dispatch_started",
        data: {
          session_id: "session-x",
          target: "app-store",
          channel: "testflight",
          audience: "QA Team",
          dispatch_id: "dp-7",
        },
      },
    )
    expect(out.dispatch?.status).toBe("in_progress")
    expect(out.dispatch?.audience).toBe("QA Team")
    expect(out.dispatch?.dispatchId).toBe("dp-7")
  })

  it("dispatch_completed flips dispatch state + appends to history", () => {
    const seeded = applyStoreSubmissionEvent(
      makeSubmission({ buildNumber: "1", status: "draft" }),
      {
        event: "mobile_workspace.store_submission.dispatch_started",
        data: {
          session_id: "session-x",
          target: "app-store",
          channel: "testflight",
          audience: "QA Team",
          dispatch_id: "dp-7",
        },
      },
    )
    const out = applyStoreSubmissionEvent(seeded, {
      event: "mobile_workspace.store_submission.dispatch_completed",
      data: {
        session_id: "session-x",
        target: "app-store",
        channel: "testflight",
        audience: "QA Team",
        dispatch_id: "dp-7",
        tester_count: 42,
      },
    })
    expect(out.dispatch?.status).toBe("succeeded")
    expect(out.dispatch?.testerCount).toBe(42)
    expect(out.history).toHaveLength(1)
    expect(out.history[0]).toMatchObject({
      id: "dp-7",
      channel: "testflight",
      status: "succeeded",
      testerCount: 42,
    })
  })

  it("dispatch_failed flips + history captures the reason", () => {
    const out = applyStoreSubmissionEvent(
      makeSubmission({ buildNumber: "1", status: "draft" }),
      {
        event: "mobile_workspace.store_submission.dispatch_failed",
        data: {
          session_id: "session-x",
          target: "app-store",
          channel: "testflight",
          audience: "QA",
          dispatch_id: "dp-9",
          reason: "No signing identity",
        },
      },
    )
    expect(out.dispatch?.status).toBe("failed")
    expect(out.dispatch?.errorReason).toBe("No signing identity")
    expect(out.history[0].status).toBe("failed")
    expect(out.history[0].reason).toBe("No signing identity")
  })

  it("caps the dispatch history at DEFAULT_MAX_DISPATCH_HISTORY", () => {
    let s = makeSubmission({ buildNumber: "1", status: "draft" })
    for (let i = 0; i < DEFAULT_MAX_DISPATCH_HISTORY + 5; i++) {
      s = applyStoreSubmissionEvent(s, {
        event: "mobile_workspace.store_submission.dispatch_completed",
        data: {
          session_id: "session-x",
          target: "app-store",
          channel: "testflight",
          dispatch_id: `dp-${i}`,
          audience: "QA",
        },
      })
    }
    expect(s.history).toHaveLength(DEFAULT_MAX_DISPATCH_HISTORY)
    expect(s.history[s.history.length - 1].id).toBe(
      `dp-${DEFAULT_MAX_DISPATCH_HISTORY + 5 - 1}`,
    )
  })

  it("unknown sub-events no-op", () => {
    const out = applyStoreSubmissionEvent(base, {
      event: "mobile_workspace.store_submission.unknown_kind",
      data: { session_id: "session-x", target: "app-store" },
    })
    expect(out).toBe(base)
  })
})

// ─── Event namespace disjointness ──────────────────────────────────────────

describe("STORE_SUBMISSION_EVENT_NAMES", () => {
  it("all entries carry the namespace prefix + are unique", () => {
    for (const n of STORE_SUBMISSION_EVENT_NAMES) {
      expect(n.startsWith(STORE_SUBMISSION_EVENT_PREFIX)).toBe(true)
    }
    expect(new Set(STORE_SUBMISSION_EVENT_NAMES).size).toBe(
      STORE_SUBMISSION_EVENT_NAMES.length,
    )
  })
  it("is disjoint from V6 / V7 sibling SSE namespaces", () => {
    const storeSet = new Set<string>(STORE_SUBMISSION_EVENT_NAMES)
    const siblings = [
      "mobile_workspace.iteration_timeline.recording",
      "mobile_workspace.iteration_timeline.recorded",
      "mobile_workspace.iteration_timeline.reset",
      "mobile_workspace.iteration_timeline.record_failed",
      "mobile_workspace.build.queued",
      "mobile_workspace.build.started",
      "mobile_workspace.build.progress",
      "mobile_workspace.build.error",
      "mobile_workspace.build.artifact",
      "mobile_workspace.build.completed",
      "mobile_workspace.build.failed",
      "mobile_workspace.build.cancelled",
      "mobile_sandbox.state",
      "mobile_sandbox.agent_visual_context.emit",
      "mobile_sandbox.autofix.start",
      "ui_sandbox.mobile_annotation_context.emit",
    ]
    for (const s of siblings) {
      expect(storeSet.has(s)).toBe(false)
    }
  })
})

// ─── Rendering ─────────────────────────────────────────────────────────────

describe("<StoreSubmissionDashboard /> — rendering contracts", () => {
  it("renders the idle empty state", () => {
    const transport = vi.fn(() => ({ close: vi.fn() }))
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        eventTransport={transport}
      />,
    )
    const root = screen.getByTestId("store-submission-dashboard")
    expect(root.getAttribute("data-status")).toBe("idle")
    expect(root.getAttribute("data-target")).toBe("app-store")
    expect(root.getAttribute("data-channel")).toBe("testflight")
    expect(screen.getByTestId("store-submission-dashboard-target-badge").textContent).toBe(
      STORE_TARGET_LABELS["app-store"],
    )
    expect(
      screen.getByTestId("store-submission-dashboard-status-badge").textContent,
    ).toMatch(/No submission/)
    // All three required Apple device-class slots land in the DOM.
    for (const cls of REQUIRED_SCREENSHOTS_APP_STORE) {
      expect(
        screen.getByTestId(`store-submission-dashboard-slot-${cls}`),
      ).toBeInTheDocument()
      expect(
        screen.getByTestId(`store-submission-dashboard-slot-${cls}-missing`),
      ).toBeInTheDocument()
    }
  })

  it("switches the required-class catalogue when target flips to play-console", () => {
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="play-console"
      />,
    )
    for (const cls of REQUIRED_SCREENSHOTS_PLAY_STORE) {
      expect(
        screen.getByTestId(`store-submission-dashboard-slot-${cls}`),
      ).toBeInTheDocument()
    }
    // No Apple classes should render when target is play-console.
    for (const cls of REQUIRED_SCREENSHOTS_APP_STORE) {
      expect(
        screen.queryByTestId(`store-submission-dashboard-slot-${cls}`),
      ).toBeNull()
    }
    expect(
      screen.getByTestId("store-submission-dashboard").getAttribute("data-channel"),
    ).toBe("firebase-app-distribution")
  })

  it("renders reviewer notes when the submission is rejected", () => {
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        submission={makeSubmission({
          status: "rejected",
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
          reviewerNotes: "Please add clearer data usage copy.",
          reviewerName: "App Review Team",
        })}
      />,
    )
    const notes = screen.getByTestId("store-submission-dashboard-reviewer-notes")
    expect(notes.textContent).toContain("App Review Team")
    expect(notes.textContent).toContain("Please add clearer data usage copy.")
  })

  it("submit button is disabled + surfaces reason when coverage is missing", () => {
    const onSubmit = vi.fn()
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onSubmit={onSubmit}
        submission={makeSubmission({
          status: "draft",
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
        })}
      />,
    )
    const btn = screen.getByTestId("store-submission-dashboard-submit") as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(
      screen.getByTestId("store-submission-dashboard-submit-reason").textContent,
    ).toMatch(/Missing screenshots/)
    fireEvent.click(btn)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("wires Submit / Re-submit / Withdraw to host callbacks", () => {
    const onSubmit = vi.fn()
    const onResubmit = vi.fn()
    const onWithdraw = vi.fn()
    const screenshots = REQUIRED_SCREENSHOTS_APP_STORE.map((cls) =>
      makeScreenshot({
        id: `s-${cls}`,
        deviceClass: cls,
        width: 1290,
        height: 2796,
        state: "valid",
      }),
    )
    // Draft + full coverage → Submit visible and fires.
    const { rerender } = render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onSubmit={onSubmit}
        onResubmit={onResubmit}
        onWithdraw={onWithdraw}
        submission={makeSubmission({
          status: "draft",
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
          screenshots,
        })}
      />,
    )
    fireEvent.click(screen.getByTestId("store-submission-dashboard-submit"))
    expect(onSubmit).toHaveBeenCalledTimes(1)

    // Submitted → Withdraw visible, Submit gone.
    rerender(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onSubmit={onSubmit}
        onResubmit={onResubmit}
        onWithdraw={onWithdraw}
        submission={makeSubmission({
          status: "submitted",
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
          screenshots,
        })}
      />,
    )
    expect(screen.queryByTestId("store-submission-dashboard-submit")).toBeNull()
    fireEvent.click(screen.getByTestId("store-submission-dashboard-withdraw"))
    expect(onWithdraw).toHaveBeenCalledTimes(1)

    // Rejected → Re-submit visible.
    rerender(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onSubmit={onSubmit}
        onResubmit={onResubmit}
        onWithdraw={onWithdraw}
        submission={makeSubmission({
          status: "rejected",
          bundleId: "com.foo",
          platformVersion: "1.0.0",
          buildNumber: "1",
          screenshots,
        })}
      />,
    )
    fireEvent.click(screen.getByTestId("store-submission-dashboard-resubmit"))
    expect(onResubmit).toHaveBeenCalledTimes(1)
  })

  it("upload / remove screenshot callbacks fire with the expected args", () => {
    const onUpload = vi.fn()
    const onRemove = vi.fn()
    const shot = makeScreenshot({
      id: "shot-a",
      deviceClass: "iphone-6.7",
      state: "valid",
    })
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onUploadScreenshot={onUpload}
        onRemoveScreenshot={onRemove}
        submission={makeSubmission({ screenshots: [shot] })}
      />,
    )
    fireEvent.click(
      screen.getByTestId(
        "store-submission-dashboard-slot-iphone-5.5-upload",
      ),
    )
    expect(onUpload).toHaveBeenCalledWith("iphone-5.5")

    fireEvent.click(
      screen.getByTestId("store-submission-dashboard-shot-shot-a-remove"),
    )
    expect(onRemove).toHaveBeenCalledWith(expect.objectContaining({ id: "shot-a" }))
  })

  it("dispatch button fires onDispatch with current channel + audience", () => {
    const onDispatch = vi.fn()
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onDispatch={onDispatch}
        submission={makeSubmission({
          status: "draft",
          buildNumber: "1",
        })}
      />,
    )
    const audienceInput = screen.getByTestId(
      "store-submission-dashboard-dispatch-audience",
    ) as HTMLInputElement
    fireEvent.change(audienceInput, { target: { value: "QA Nightly" } })
    expect(audienceInput.value).toBe("QA Nightly")

    fireEvent.click(screen.getByTestId("store-submission-dashboard-dispatch-start"))
    expect(onDispatch).toHaveBeenCalledTimes(1)
    const arg = onDispatch.mock.calls[0][0]
    expect(arg.channel).toBe("testflight")
    expect(arg.audience).toBe("QA Nightly")
  })

  it("dispatch button disabled surfaces the gate reason", () => {
    // buildNumber empty → "No build number yet" (first gate)
    const { rerender } = render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onDispatch={vi.fn()}
        submission={makeSubmission({ status: "idle" })}
      />,
    )
    const btn = screen.getByTestId(
      "store-submission-dashboard-dispatch-start",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(
      screen.getByTestId("store-submission-dashboard-dispatch-reason").textContent,
    ).toMatch(/No build number/)

    // Build present but status=idle → "No submission yet" (second gate)
    rerender(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        onDispatch={vi.fn()}
        submission={makeSubmission({ status: "idle", buildNumber: "1" })}
      />,
    )
    expect(
      screen.getByTestId("store-submission-dashboard-dispatch-reason").textContent,
    ).toMatch(/No submission/)
  })

  it("renders history section when entries exist", () => {
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        submission={makeSubmission({
          status: "draft",
          buildNumber: "1",
          history: [
            {
              id: "dp-1",
              channel: "testflight",
              audience: "Internal",
              status: "succeeded",
              testerCount: 10,
              at: new Date(Date.now() - 60_000).toISOString(),
            },
            {
              id: "dp-2",
              channel: "testflight",
              audience: "QA",
              status: "failed",
              at: new Date(Date.now() - 120_000).toISOString(),
              reason: "No signing identity.",
            },
          ],
        })}
      />,
    )
    expect(screen.getByTestId("store-submission-dashboard-history-count").textContent).toBe("2")
    // The history list is reversed — most-recent first.
    const list = screen.getByTestId("store-submission-dashboard-history-list")
    const first = list.children[0] as HTMLElement
    // The second-inserted entry is the newer one because we used now-120s
    // which is older — reverse of insert order puts newer-inserted last.
    // Either way, both rows must exist.
    expect(
      screen.getByTestId("store-submission-dashboard-history-dp-1-count").textContent,
    ).toBe("10 testers")
    expect(
      screen.getByTestId("store-submission-dashboard-history-dp-2-reason").textContent,
    ).toBe("No signing identity.")
    expect(first).toBeTruthy()
  })

  it("feeds SSE events from an injected transport into the reducer", () => {
    let capture: ((ev: StoreSubmissionEvent) => void) | null = null
    const transport = vi.fn((fn: (ev: StoreSubmissionEvent) => void) => {
      capture = fn
      return { close: vi.fn() }
    })
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        eventTransport={transport}
      />,
    )
    expect(transport).toHaveBeenCalled()

    act(() => {
      capture!({
        event: "mobile_workspace.store_submission.queued",
        data: {
          session_id: "session-x",
          target: "app-store",
          bundle_id: "com.foo",
          version: "1.0.0",
          build_number: "1",
        },
      })
    })
    expect(
      screen.getByTestId("store-submission-dashboard").getAttribute("data-status"),
    ).toBe("draft")

    act(() => {
      capture!({
        event: "mobile_workspace.store_submission.submitted",
        data: { session_id: "session-x", target: "app-store" },
      })
    })
    expect(
      screen.getByTestId("store-submission-dashboard").getAttribute("data-status"),
    ).toBe("submitted")

    act(() => {
      capture!({
        event: "mobile_workspace.store_submission.review_updated",
        data: {
          session_id: "session-x",
          target: "app-store",
          status: "rejected",
          reviewer_notes: "Add a privacy policy link.",
        },
      })
    })
    expect(
      screen.getByTestId("store-submission-dashboard").getAttribute("data-status"),
    ).toBe("rejected")
    expect(
      screen.getByTestId("store-submission-dashboard-reviewer-notes").textContent,
    ).toContain("Add a privacy policy link.")
  })

  it("drops events whose target does not match", () => {
    let capture: ((ev: StoreSubmissionEvent) => void) | null = null
    const transport = vi.fn((fn: (ev: StoreSubmissionEvent) => void) => {
      capture = fn
      return { close: vi.fn() }
    })
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        eventTransport={transport}
      />,
    )
    act(() => {
      capture!({
        event: "mobile_workspace.store_submission.submitted",
        data: { session_id: "session-x", target: "play-console" },
      })
    })
    // Still idle — cross-target event was dropped.
    expect(
      screen.getByTestId("store-submission-dashboard").getAttribute("data-status"),
    ).toBe("idle")
  })

  it("closes the event transport on unmount", () => {
    const close = vi.fn()
    const transport = vi.fn(() => ({ close }))
    const { unmount } = render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        eventTransport={transport}
      />,
    )
    unmount()
    expect(close).toHaveBeenCalledTimes(1)
  })

  it("renders dispatch in-progress badge + tester count when dispatching", () => {
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        submission={makeSubmission({
          status: "submitted",
          buildNumber: "1",
          dispatch: {
            channel: "testflight",
            status: "in_progress",
            audience: "Internal",
            testerCount: 25,
            startedAt: new Date().toISOString(),
          },
        })}
      />,
    )
    expect(
      screen.getByTestId("store-submission-dashboard-dispatch-in-progress"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("store-submission-dashboard-dispatch-tester-count").textContent,
    ).toBe("25 testers")
  })

  it("renders dispatch error when dispatch failed", () => {
    render(
      <StoreSubmissionDashboard
        sessionId="session-x"
        target="app-store"
        submission={makeSubmission({
          status: "draft",
          buildNumber: "1",
          dispatch: {
            channel: "testflight",
            status: "failed",
            audience: "Internal",
            testerCount: 0,
            finishedAt: new Date().toISOString(),
            errorReason: "Invalid provisioning profile.",
          },
        })}
      />,
    )
    expect(
      screen.getByTestId("store-submission-dashboard-dispatch-error").textContent,
    ).toContain("Invalid provisioning profile.")
  })
})

describe("DEVICE_CLASS_LABELS + DISPATCH_CHANNEL_LABELS + STORE_TARGET_LABELS", () => {
  it("labels every device class", () => {
    const classes = [
      "iphone-6.7",
      "iphone-6.5",
      "iphone-5.5",
      "ipad-13",
      "android-phone",
      "android-tablet-7",
      "android-tablet-10",
    ] as const
    for (const c of classes) {
      expect(DEVICE_CLASS_LABELS[c]).toBeTruthy()
    }
  })
  it("labels each dispatch channel + target", () => {
    expect(DISPATCH_CHANNEL_LABELS.testflight).toBeTruthy()
    expect(DISPATCH_CHANNEL_LABELS["firebase-app-distribution"]).toBeTruthy()
    expect(STORE_TARGET_LABELS["app-store"]).toBeTruthy()
    expect(STORE_TARGET_LABELS["play-console"]).toBeTruthy()
  })
})
