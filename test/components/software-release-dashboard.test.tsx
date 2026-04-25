/**
 * V8 #2 (TODO row 2703 / issue #324) — Contract tests for
 * `software-release-dashboard.tsx`.
 *
 * Mirrors the V7 #4 mobile-build-status-panel + V7 #5 store-submission
 * test shape:
 *
 *   1. Pure helpers — duration, byte-size, relative-time, framework
 *      filter, rollup status, rollup counts, total artifact bytes,
 *      artifact coercion via the reducer.
 *   2. SSE reducer — every `software_workspace.release.*` event mutates
 *      the snapshot in the exact shape the grid renders.
 *   3. Rendering — happy path for idle / in-flight / passed / failed
 *      states; grid row counts + stable order; download wiring; retry
 *      / cancel callback wiring; framework filter greying out N/A
 *      rows; rollup badge colour mapping.
 *
 * Event-namespace disjointness is verified against V7 / V8 sibling
 * namespaces so a future refactor that merges busses can catch the
 * collision at CI time.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import {
  RELEASE_EVENT_NAMES,
  RELEASE_EVENT_PREFIX,
  RELEASE_ROLLUP_STATUS_LABELS,
  RELEASE_TARGET_ORDER,
  RELEASE_TARGET_STATUS_LABELS,
  SoftwareReleaseDashboard,
  applyFrameworkFilter,
  applyReleaseEvent,
  buildTargetOption,
  clampReleaseProgress,
  computeRollupCounts,
  computeRollupStatus,
  emptyReleaseSnapshot,
  formatReleaseByteSize,
  formatReleaseDuration,
  formatReleaseRelativeTime,
  isReleaseShippable,
  isTerminalReleaseTargetStatus,
  matchReleaseEvent,
  releaseTargetStatusColorVar,
  releaseTargetStatusLabel,
  totalArtifactBytes,
  type ReleaseEvent,
  type ReleaseSnapshot,
  type ReleaseTargetState,
} from "@/components/omnisight/software-release-dashboard"
import {
  BUILD_TARGET_OPTIONS,
  LANGUAGE_OPTIONS,
  type BuildTarget,
  type FrameworkOption,
} from "@/app/workspace/software/page"

// ─── Helpers ────────────────────────────────────────────────────────────────

function makeSnapshot(
  overrides: Partial<ReleaseSnapshot> = {},
  perTarget: Partial<Record<BuildTarget, Partial<ReleaseTargetState>>> = {},
): ReleaseSnapshot {
  const base = emptyReleaseSnapshot("session-x", overrides.releaseId ?? "rel-1")
  const targets = { ...base.targets }
  for (const [target, patch] of Object.entries(perTarget)) {
    const t = target as BuildTarget
    targets[t] = { ...targets[t], ...patch }
  }
  return { ...base, ...overrides, targets }
}

function pythonFastApi(): FrameworkOption {
  const lang = LANGUAGE_OPTIONS.find((l) => l.id === "python")!
  return lang.frameworks.find((f) => f.id === "python:fastapi")!
}

function tsExpress(): FrameworkOption {
  const lang = LANGUAGE_OPTIONS.find((l) => l.id === "typescript")!
  return lang.frameworks.find((f) => f.id === "ts:express")!
}

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("releaseTargetStatusLabel", () => {
  it("maps every status to its label", () => {
    expect(releaseTargetStatusLabel("pending")).toBe("Pending")
    expect(releaseTargetStatusLabel("queued")).toBe("Queued")
    expect(releaseTargetStatusLabel("building")).toBe("Building")
    expect(releaseTargetStatusLabel("passed")).toBe("Passed")
    expect(releaseTargetStatusLabel("failed")).toBe("Failed")
    expect(releaseTargetStatusLabel("skipped")).toBe("Skipped")
    expect(releaseTargetStatusLabel("cancelled")).toBe("Cancelled")
    expect(releaseTargetStatusLabel("not_applicable")).toBe("N/A")
  })
})

describe("releaseTargetStatusColorVar", () => {
  it("emerald for passed, red for failed, blue for in-flight, muted otherwise", () => {
    expect(releaseTargetStatusColorVar("passed")).toBe(
      "var(--validation-emerald)",
    )
    expect(releaseTargetStatusColorVar("failed")).toBe("var(--critical-red)")
    expect(releaseTargetStatusColorVar("building")).toBe("var(--neural-blue)")
    expect(releaseTargetStatusColorVar("queued")).toBe("var(--neural-blue)")
    expect(releaseTargetStatusColorVar("pending")).toBe(
      "var(--muted-foreground)",
    )
    expect(releaseTargetStatusColorVar("skipped")).toBe(
      "var(--muted-foreground)",
    )
    expect(releaseTargetStatusColorVar("not_applicable")).toBe(
      "var(--muted-foreground)",
    )
    expect(releaseTargetStatusColorVar("cancelled")).toBe(
      "var(--muted-foreground)",
    )
  })
})

describe("isTerminalReleaseTargetStatus", () => {
  it("returns true for passed/failed/skipped/cancelled/not_applicable", () => {
    for (const s of ["passed", "failed", "skipped", "cancelled", "not_applicable"] as const) {
      expect(isTerminalReleaseTargetStatus(s)).toBe(true)
    }
    for (const s of ["pending", "queued", "building"] as const) {
      expect(isTerminalReleaseTargetStatus(s)).toBe(false)
    }
  })
})

describe("formatReleaseDuration", () => {
  it("handles seconds / minutes / hours", () => {
    expect(formatReleaseDuration(500)).toBe("0s")
    expect(formatReleaseDuration(45_500)).toBe("45s")
    expect(formatReleaseDuration(61_000)).toBe("1m 1s")
    expect(formatReleaseDuration(3_600_000)).toBe("1h 0m 0s")
    expect(formatReleaseDuration(3_723_000)).toBe("1h 2m 3s")
  })
  it("returns dash for null / negative / NaN", () => {
    expect(formatReleaseDuration(null)).toBe("—")
    expect(formatReleaseDuration(undefined)).toBe("—")
    expect(formatReleaseDuration(-1)).toBe("—")
    expect(formatReleaseDuration(Number.NaN)).toBe("—")
    expect(formatReleaseDuration(Number.POSITIVE_INFINITY)).toBe("—")
  })
})

describe("formatReleaseByteSize", () => {
  it("picks the right unit at each boundary", () => {
    expect(formatReleaseByteSize(0)).toBe("0 B")
    expect(formatReleaseByteSize(512)).toBe("512 B")
    expect(formatReleaseByteSize(2048)).toBe("2.0 KB")
    expect(formatReleaseByteSize(5 * 1024 * 1024)).toBe("5.0 MB")
    expect(formatReleaseByteSize(1024 * 1024 * 1024)).toBe("1.00 GB")
  })
  it("returns dash for null / negative / NaN", () => {
    expect(formatReleaseByteSize(null)).toBe("—")
    expect(formatReleaseByteSize(undefined)).toBe("—")
    expect(formatReleaseByteSize(-1)).toBe("—")
    expect(formatReleaseByteSize(Number.NaN)).toBe("—")
  })
})

describe("formatReleaseRelativeTime", () => {
  it("buckets into just-now / minutes / hours / days / weeks", () => {
    const now = 1_700_000_000_000
    expect(formatReleaseRelativeTime(new Date(now - 5_000).toISOString(), now)).toBe(
      "just now",
    )
    expect(
      formatReleaseRelativeTime(new Date(now - 5 * 60_000).toISOString(), now),
    ).toBe("5m ago")
    expect(
      formatReleaseRelativeTime(new Date(now - 2 * 3_600_000).toISOString(), now),
    ).toBe("2h ago")
    expect(
      formatReleaseRelativeTime(
        new Date(now - 3 * 24 * 3_600_000).toISOString(),
        now,
      ),
    ).toBe("3d ago")
    expect(
      formatReleaseRelativeTime(
        new Date(now - 12 * 7 * 24 * 3_600_000).toISOString(),
        now,
      ),
    ).toBe("12w ago")
  })
  it("returns dash for null / undefined / non-string", () => {
    expect(formatReleaseRelativeTime(null)).toBe("—")
    expect(formatReleaseRelativeTime(undefined)).toBe("—")
  })
})

describe("clampReleaseProgress", () => {
  it("clamps to [0, 100]", () => {
    expect(clampReleaseProgress(-10)).toBe(0)
    expect(clampReleaseProgress(150)).toBe(100)
    expect(clampReleaseProgress(50)).toBe(50)
  })
  it("returns null for null / NaN / Infinity", () => {
    expect(clampReleaseProgress(null)).toBeNull()
    expect(clampReleaseProgress(undefined)).toBeNull()
    expect(clampReleaseProgress(Number.NaN)).toBeNull()
    expect(clampReleaseProgress(Number.POSITIVE_INFINITY)).toBeNull()
  })
})

describe("buildTargetOption", () => {
  it("resolves every BuildTarget id to its catalogue entry", () => {
    for (const opt of BUILD_TARGET_OPTIONS) {
      const found = buildTargetOption(opt.id)
      expect(found?.id).toBe(opt.id)
      expect(found?.label).toBe(opt.label)
    }
  })
})

describe("emptyReleaseSnapshot", () => {
  it("seeds every BuildTarget in pending status", () => {
    const snap = emptyReleaseSnapshot("session-x", "rel-1")
    expect(snap.sessionId).toBe("session-x")
    expect(snap.releaseId).toBe("rel-1")
    for (const opt of BUILD_TARGET_OPTIONS) {
      expect(snap.targets[opt.id]).toBeDefined()
      expect(snap.targets[opt.id].status).toBe("pending")
      expect(snap.targets[opt.id].artifact).toBeNull()
    }
  })
})

// ─── Framework filter ──────────────────────────────────────────────────────

describe("applyFrameworkFilter", () => {
  it("greys out targets the framework cannot emit", () => {
    const snap = emptyReleaseSnapshot("session-x", "rel-1")
    const filtered = applyFrameworkFilter(snap, pythonFastApi())
    // FastAPI default targets: docker / helm / wheel.
    expect(filtered.targets.docker.status).toBe("pending")
    expect(filtered.targets.helm.status).toBe("pending")
    expect(filtered.targets.wheel.status).toBe("pending")
    // Targets the framework cannot emit are flipped to N/A.
    expect(filtered.targets.jar.status).toBe("not_applicable")
    expect(filtered.targets.dmg.status).toBe("not_applicable")
    expect(filtered.targets.npm.status).toBe("not_applicable")
    // frameworkId / frameworkLabel are surfaced.
    expect(filtered.frameworkId).toBe("python:fastapi")
    expect(filtered.frameworkLabel).toBe("FastAPI")
  })

  it("preserves targets that are already in a terminal state", () => {
    const snap = makeSnapshot({}, {
      jar: { status: "passed", artifact: { id: "j1", target: "jar", filename: "app.jar", downloadUrl: "/app.jar", byteSize: 100, sha256: null, createdAt: null, contentType: null } },
    })
    const filtered = applyFrameworkFilter(snap, pythonFastApi())
    // jar isn't allowed by Python:FastAPI but already passed → keep.
    expect(filtered.targets.jar.status).toBe("passed")
  })

  it("flipping a previously-NA target back to pending when framework allows", () => {
    let snap = emptyReleaseSnapshot("session-x", "rel-1")
    snap = applyFrameworkFilter(snap, pythonFastApi())
    expect(snap.targets.npm.status).toBe("not_applicable")
    snap = applyFrameworkFilter(snap, tsExpress())
    expect(snap.targets.npm.status).toBe("pending")
  })

  it("returns the same instance when no targets need to change", () => {
    const snap = applyFrameworkFilter(
      emptyReleaseSnapshot("session-x", "rel-1"),
      pythonFastApi(),
    )
    const again = applyFrameworkFilter(snap, pythonFastApi())
    expect(again).toBe(snap)
  })

  it("returns the input untouched when framework is null", () => {
    const snap = emptyReleaseSnapshot("session-x", "rel-1")
    expect(applyFrameworkFilter(snap, null)).toBe(snap)
  })
})

// ─── Rollup status / counts ────────────────────────────────────────────────

describe("computeRollupStatus", () => {
  it("idle when all relevant rows are pending", () => {
    expect(computeRollupStatus(emptyReleaseSnapshot("s", "r"))).toBe("idle")
  })
  it("in_progress when any row is building or queued", () => {
    expect(
      computeRollupStatus(makeSnapshot({}, { docker: { status: "building" } })),
    ).toBe("in_progress")
    expect(
      computeRollupStatus(makeSnapshot({}, { docker: { status: "queued" } })),
    ).toBe("in_progress")
  })
  it("passed when every relevant row passed", () => {
    const allPass = emptyReleaseSnapshot("s", "r")
    for (const k of Object.keys(allPass.targets) as BuildTarget[]) {
      allPass.targets[k] = { ...allPass.targets[k], status: "passed" }
    }
    expect(computeRollupStatus(allPass)).toBe("passed")
  })
  it("partial when some passed and some failed", () => {
    const snap = makeSnapshot(
      {},
      {
        docker: { status: "passed" },
        helm: { status: "failed" },
      },
    )
    expect(computeRollupStatus(snap)).toBe("partial")
  })
  it("failed when failed rows but no passed rows", () => {
    const snap = makeSnapshot({}, { docker: { status: "failed" } })
    expect(computeRollupStatus(snap)).toBe("failed")
  })
  it("ignores not_applicable / skipped rows when computing rollup", () => {
    // All non-NA rows are in N/A or pending → still idle.
    const snap = applyFrameworkFilter(
      emptyReleaseSnapshot("s", "r"),
      pythonFastApi(),
    )
    expect(computeRollupStatus(snap)).toBe("idle")
  })
})

describe("computeRollupCounts", () => {
  it("partitions every target into one of the eight buckets", () => {
    const snap = makeSnapshot(
      {},
      {
        docker: { status: "passed" },
        helm: { status: "passed" },
        deb: { status: "failed" },
        rpm: { status: "building" },
        msi: { status: "queued" },
        dmg: { status: "skipped" },
        wheel: { status: "cancelled" },
        npm: { status: "not_applicable" },
        jar: { status: "not_applicable" },
        binary: { status: "pending" },
      },
    )
    const c = computeRollupCounts(snap)
    expect(c.total).toBe(BUILD_TARGET_OPTIONS.length)
    expect(c.passed).toBe(2)
    expect(c.failed).toBe(1)
    expect(c.inFlight).toBe(2)
    expect(c.skipped).toBe(1)
    expect(c.cancelled).toBe(1)
    expect(c.notApplicable).toBe(2)
    expect(c.pending).toBe(1)
  })
})

describe("totalArtifactBytes", () => {
  it("sums byte size from artifacts that have it", () => {
    const snap = makeSnapshot(
      {},
      {
        docker: {
          artifact: {
            id: "d1",
            target: "docker",
            filename: "image.tar",
            downloadUrl: "/d",
            byteSize: 1_000_000,
            sha256: null,
            createdAt: null,
            contentType: null,
          },
        },
        wheel: {
          artifact: {
            id: "w1",
            target: "wheel",
            filename: "x.whl",
            downloadUrl: "/w",
            byteSize: 5_000,
            sha256: null,
            createdAt: null,
            contentType: null,
          },
        },
      },
    )
    expect(totalArtifactBytes(snap)).toBe(1_005_000)
  })
  it("returns 0 when no artifacts have a byte size", () => {
    expect(totalArtifactBytes(emptyReleaseSnapshot("s", "r"))).toBe(0)
  })
})

describe("isReleaseShippable", () => {
  it("only true when the rollup is fully passed", () => {
    expect(isReleaseShippable(emptyReleaseSnapshot("s", "r"))).toBe(false)
    const allPass = emptyReleaseSnapshot("s", "r")
    for (const k of Object.keys(allPass.targets) as BuildTarget[]) {
      allPass.targets[k] = { ...allPass.targets[k], status: "passed" }
    }
    expect(isReleaseShippable(allPass)).toBe(true)
  })
})

// ─── SSE event matching + reducer ──────────────────────────────────────────

describe("matchReleaseEvent", () => {
  it("rejects events outside the release namespace", () => {
    expect(
      matchReleaseEvent(
        { event: "mobile_workspace.build.queued", data: { session_id: "s" } },
        "s",
        null,
      ),
    ).toBe(false)
  })
  it("rejects events for a different session", () => {
    expect(
      matchReleaseEvent(
        { event: "software_workspace.release.queued", data: { session_id: "other" } },
        "mine",
        null,
      ),
    ).toBe(false)
  })
  it("accepts events that match the session and (optionally) the release id", () => {
    expect(
      matchReleaseEvent(
        {
          event: "software_workspace.release.queued",
          data: { session_id: "s", release_id: "r" },
        },
        "s",
        null,
      ),
    ).toBe(true)
    expect(
      matchReleaseEvent(
        {
          event: "software_workspace.release.target_started",
          data: { session_id: "s", release_id: "r" },
        },
        "s",
        "r",
      ),
    ).toBe(true)
    expect(
      matchReleaseEvent(
        {
          event: "software_workspace.release.target_started",
          data: { session_id: "s", release_id: "other" },
        },
        "s",
        "r",
      ),
    ).toBe(false)
  })
  it("treats camelCase aliases as equivalent", () => {
    expect(
      matchReleaseEvent(
        {
          event: "software_workspace.release.queued",
          data: { sessionId: "s", releaseId: "r" },
        },
        "s",
        null,
      ),
    ).toBe(true)
  })
})

describe("applyReleaseEvent", () => {
  const base = makeSnapshot({}, { docker: { status: "pending" } })

  it("queued resets every target buffer + sets the release id", () => {
    const out = applyReleaseEvent(
      makeSnapshot({}, {
        docker: {
          status: "passed",
          artifact: {
            id: "x",
            target: "docker",
            filename: "x",
            downloadUrl: "/x",
            byteSize: 10,
            sha256: null,
            createdAt: null,
            contentType: null,
          },
        },
      }),
      {
        event: "software_workspace.release.queued",
        data: { session_id: "session-x", release_id: "rel-2" },
      },
    )
    expect(out.releaseId).toBe("rel-2")
    expect(out.targets.docker.status).toBe("pending")
    expect(out.targets.docker.artifact).toBeNull()
    expect(out.queuedAt).toBeTruthy()
  })

  it("target_queued flips a single target", () => {
    const out = applyReleaseEvent(base, {
      event: "software_workspace.release.target_queued",
      data: { session_id: "session-x", release_id: "rel-1", target: "docker" },
    })
    expect(out.targets.docker.status).toBe("queued")
    expect(out.targets.docker.queuedAt).toBeTruthy()
    expect(out.targets.helm.status).toBe("pending")
  })

  it("target_started flips queued → building + sets startedAt", () => {
    const out = applyReleaseEvent(
      makeSnapshot({}, { docker: { status: "queued" } }),
      {
        event: "software_workspace.release.target_started",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "docker",
          progress: 5,
          detail: "compile-amd64",
        },
      },
    )
    expect(out.targets.docker.status).toBe("building")
    expect(out.targets.docker.progress).toBe(5)
    expect(out.targets.docker.detail).toBe("compile-amd64")
    expect(out.targets.docker.startedAt).toBeTruthy()
  })

  it("target_progress auto-promotes queued → building", () => {
    const out = applyReleaseEvent(
      makeSnapshot({}, { docker: { status: "queued" } }),
      {
        event: "software_workspace.release.target_progress",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "docker",
          progress: 42,
        },
      },
    )
    expect(out.targets.docker.status).toBe("building")
    expect(out.targets.docker.progress).toBe(42)
  })

  it("target_succeeded flips to passed + 100% + computes durationMs + attaches artifact", () => {
    const startedAt = new Date(Date.now() - 5_000).toISOString()
    const finishedAt = new Date().toISOString()
    const out = applyReleaseEvent(
      makeSnapshot({}, {
        docker: { status: "building", startedAt, progress: 50 },
      }),
      {
        event: "software_workspace.release.target_succeeded",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "docker",
          finished_at: finishedAt,
          filename: "image.tar.gz",
          download_url: "/artifacts/image.tar.gz",
          byte_size: 500_000,
          sha256: "deadbeef",
        },
      },
    )
    expect(out.targets.docker.status).toBe("passed")
    expect(out.targets.docker.progress).toBe(100)
    expect(out.targets.docker.finishedAt).toBe(finishedAt)
    expect(out.targets.docker.durationMs).toBeGreaterThanOrEqual(4_000)
    expect(out.targets.docker.artifact?.filename).toBe("image.tar.gz")
    expect(out.targets.docker.artifact?.byteSize).toBe(500_000)
    expect(out.targets.docker.artifact?.sha256).toBe("deadbeef")
  })

  it("target_failed captures reason + finishedAt + duration", () => {
    const startedAt = new Date(Date.now() - 3_000).toISOString()
    const out = applyReleaseEvent(
      makeSnapshot({}, { docker: { status: "building", startedAt } }),
      {
        event: "software_workspace.release.target_failed",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "docker",
          reason: "docker build: COPY /no/such/file failed",
        },
      },
    )
    expect(out.targets.docker.status).toBe("failed")
    expect(out.targets.docker.failureReason).toContain("COPY")
    expect(out.targets.docker.durationMs).toBeGreaterThanOrEqual(2_000)
  })

  it("target_cancelled captures default reason", () => {
    const out = applyReleaseEvent(
      makeSnapshot({}, { docker: { status: "building" } }),
      {
        event: "software_workspace.release.target_cancelled",
        data: { session_id: "session-x", release_id: "rel-1", target: "docker" },
      },
    )
    expect(out.targets.docker.status).toBe("cancelled")
    expect(out.targets.docker.failureReason).toBe("Cancelled by operator")
  })

  it("artifact_uploaded attaches an artifact without flipping status", () => {
    const out = applyReleaseEvent(
      makeSnapshot({}, { wheel: { status: "passed" } }),
      {
        event: "software_workspace.release.artifact_uploaded",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "wheel",
          filename: "omnisight-0.1.0-py3-none-any.whl",
          download_url: "/artifacts/omnisight-0.1.0.whl",
          byte_size: 1_234,
        },
      },
    )
    expect(out.targets.wheel.artifact?.filename).toBe(
      "omnisight-0.1.0-py3-none-any.whl",
    )
    expect(out.targets.wheel.status).toBe("passed")
  })

  it("ignores events with an unknown target id", () => {
    const out = applyReleaseEvent(base, {
      event: "software_workspace.release.target_started",
      data: { session_id: "session-x", release_id: "rel-1", target: "fake-target" },
    })
    expect(out).toBe(base)
  })

  it("ignores events without a target except for queued", () => {
    const out = applyReleaseEvent(base, {
      event: "software_workspace.release.target_progress",
      data: { session_id: "session-x", release_id: "rel-1" },
    })
    expect(out).toBe(base)
  })

  it("unknown sub-events are no-ops rather than throwing", () => {
    const out = applyReleaseEvent(base, {
      event: "software_workspace.release.unknown_thing",
      data: { session_id: "session-x", target: "docker" },
    })
    expect(out).toBe(base)
  })

  it("artifact_uploaded with no filename / url is dropped", () => {
    const out = applyReleaseEvent(base, {
      event: "software_workspace.release.artifact_uploaded",
      data: { session_id: "session-x", release_id: "rel-1", target: "docker" },
    })
    expect(out.targets.docker.artifact).toBeNull()
  })
})

// ─── Event namespace disjointness ──────────────────────────────────────────

describe("RELEASE_EVENT_NAMES", () => {
  it("has one entry per documented event name, all prefixed with the release namespace", () => {
    for (const name of RELEASE_EVENT_NAMES) {
      expect(name.startsWith(RELEASE_EVENT_PREFIX)).toBe(true)
    }
    expect(new Set(RELEASE_EVENT_NAMES).size).toBe(RELEASE_EVENT_NAMES.length)
    // 1 release-level + 6 target-level + 1 artifact-level.
    expect(RELEASE_EVENT_NAMES.length).toBe(8)
  })
  it("is disjoint from V7 / V8 sibling SSE namespaces", () => {
    const releaseSet = new Set<string>(RELEASE_EVENT_NAMES)
    const siblings = [
      "mobile_workspace.iteration_timeline.recorded",
      "mobile_workspace.iteration_timeline.reset",
      "mobile_workspace.build.queued",
      "mobile_workspace.build.started",
      "mobile_workspace.build.progress",
      "mobile_workspace.build.completed",
      "mobile_workspace.build.failed",
      "mobile_workspace.store_submission.submitted",
      "mobile_workspace.store_submission.review_updated",
      "mobile_workspace.store_submission.dispatch_started",
      "software_workspace.terminal_stream.append",
    ]
    for (const s of siblings) {
      expect(releaseSet.has(s)).toBe(false)
    }
  })
})

describe("RELEASE_TARGET_ORDER", () => {
  it("matches BUILD_TARGET_OPTIONS order one-to-one", () => {
    expect([...RELEASE_TARGET_ORDER]).toEqual(
      BUILD_TARGET_OPTIONS.map((o) => o.id),
    )
  })
})

describe("RELEASE_TARGET_STATUS_LABELS / RELEASE_ROLLUP_STATUS_LABELS", () => {
  it("covers every status / rollup variant", () => {
    for (const s of [
      "pending",
      "queued",
      "building",
      "passed",
      "failed",
      "skipped",
      "cancelled",
      "not_applicable",
    ] as const) {
      expect(RELEASE_TARGET_STATUS_LABELS[s]).toBeTruthy()
    }
    for (const s of [
      "idle",
      "in_progress",
      "passed",
      "partial",
      "failed",
    ] as const) {
      expect(RELEASE_ROLLUP_STATUS_LABELS[s]).toBeTruthy()
    }
  })
})

// ─── Rendering ─────────────────────────────────────────────────────────────

describe("<SoftwareReleaseDashboard /> — rendering contracts", () => {
  it("renders the idle empty state with all 10 target rows in stable order", () => {
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        eventTransport={vi.fn(() => ({ close: vi.fn() }))}
      />,
    )
    const root = screen.getByTestId("software-release-dashboard")
    expect(root.getAttribute("data-rollup")).toBe("idle")
    // Every BuildTarget renders a row.
    for (const opt of BUILD_TARGET_OPTIONS) {
      const row = screen.getByTestId(`software-release-dashboard-row-${opt.id}`)
      expect(row).toBeInTheDocument()
      expect(row.getAttribute("data-status")).toBe("pending")
    }
    // Counts surface the rollup partition.
    expect(
      screen.getByTestId("software-release-dashboard-counts-passed").textContent,
    ).toMatch(/0/)
    expect(
      screen.getByTestId("software-release-dashboard-counts-failed").textContent,
    ).toMatch(/0/)
  })

  it("renders the artifact rows + download links when present", () => {
    const snap = makeSnapshot(
      {},
      {
        docker: {
          status: "passed",
          progress: 100,
          finishedAt: new Date().toISOString(),
          durationMs: 12_000,
          artifact: {
            id: "d1",
            target: "docker",
            filename: "omnisight:0.1.0",
            downloadUrl: "/artifacts/docker/omnisight-0.1.0.tar",
            byteSize: 25 * 1024 * 1024,
            sha256: "deadbeefcafebabe",
            createdAt: null,
            contentType: null,
          },
        },
        wheel: {
          status: "passed",
          finishedAt: new Date().toISOString(),
          artifact: {
            id: "w1",
            target: "wheel",
            filename: "omnisight-0.1.0-py3-none-any.whl",
            downloadUrl: "/artifacts/wheel/omnisight-0.1.0.whl",
            byteSize: 1024 * 800,
            sha256: null,
            createdAt: null,
            contentType: null,
          },
        },
      },
    )
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        snapshot={snap}
      />,
    )
    expect(
      screen.getByTestId(
        "software-release-dashboard-row-docker-artifact-name",
      ).textContent,
    ).toBe("omnisight:0.1.0")
    expect(
      screen.getByTestId(
        "software-release-dashboard-row-docker-artifact-size",
      ).textContent,
    ).toBe("25.0 MB")
    const link = screen.getByTestId(
      "software-release-dashboard-row-docker-artifact-download",
    )
    expect(link.tagName.toLowerCase()).toBe("a")
    expect(link.getAttribute("href")).toBe("/artifacts/docker/omnisight-0.1.0.tar")
    // wheel artifact also rendered.
    expect(
      screen.getByTestId(
        "software-release-dashboard-row-wheel-artifact-name",
      ).textContent,
    ).toContain(".whl")
  })

  it("calls onDownloadArtifact callback when injected, rather than rendering a bare link", () => {
    const onDownload = vi.fn()
    const snap = makeSnapshot(
      {},
      {
        docker: {
          status: "passed",
          artifact: {
            id: "d1",
            target: "docker",
            filename: "image.tar",
            downloadUrl: "/x",
            byteSize: 100,
            sha256: null,
            createdAt: null,
            contentType: null,
          },
        },
      },
    )
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        snapshot={snap}
        onDownloadArtifact={onDownload}
      />,
    )
    const btn = screen.getByTestId(
      "software-release-dashboard-row-docker-artifact-download",
    )
    fireEvent.click(btn)
    expect(onDownload).toHaveBeenCalledTimes(1)
    expect(onDownload.mock.calls[0][0]).toMatchObject({
      target: "docker",
      filename: "image.tar",
    })
  })

  it("retry button shows on a failed target and fires the callback", () => {
    const onRetry = vi.fn()
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        snapshot={makeSnapshot(
          {},
          { docker: { status: "failed", failureReason: "boom" } },
        )}
        onRetryTarget={onRetry}
      />,
    )
    fireEvent.click(
      screen.getByTestId("software-release-dashboard-row-docker-retry"),
    )
    expect(onRetry).toHaveBeenCalledWith("docker")
    // Failure reason inline.
    expect(
      screen.getByTestId("software-release-dashboard-row-docker-failure")
        .textContent,
    ).toContain("boom")
  })

  it("cancel button shows on an in-flight target and fires the callback", () => {
    const onCancel = vi.fn()
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        snapshot={makeSnapshot({}, { docker: { status: "building" } })}
        onCancelTarget={onCancel}
      />,
    )
    fireEvent.click(
      screen.getByTestId("software-release-dashboard-row-docker-cancel"),
    )
    expect(onCancel).toHaveBeenCalledWith("docker")
  })

  it("trigger button is disabled while a build is in_progress", () => {
    const onTrigger = vi.fn()
    const { rerender } = render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        snapshot={makeSnapshot({}, { docker: { status: "building" } })}
        onTriggerRelease={onTrigger}
      />,
    )
    const btn = screen.getByTestId("software-release-dashboard-trigger")
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    rerender(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        snapshot={makeSnapshot({}, { docker: { status: "passed" } })}
        onTriggerRelease={onTrigger}
      />,
    )
    expect(
      (screen.getByTestId(
        "software-release-dashboard-trigger",
      ) as HTMLButtonElement).disabled,
    ).toBe(false)
    fireEvent.click(screen.getByTestId("software-release-dashboard-trigger"))
    expect(onTrigger).toHaveBeenCalledTimes(1)
  })

  it("framework filter greys out N/A target rows", () => {
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        framework={pythonFastApi()}
      />,
    )
    expect(
      screen
        .getByTestId("software-release-dashboard-row-jar")
        .getAttribute("data-status"),
    ).toBe("not_applicable")
    expect(
      screen
        .getByTestId("software-release-dashboard-row-docker")
        .getAttribute("data-status"),
    ).toBe("pending")
    expect(
      screen.getByTestId("software-release-dashboard-framework-badge").textContent,
    ).toBe("FastAPI")
  })

  it("feeds SSE events from an injected transport into the reducer", () => {
    let capture: ((ev: ReleaseEvent) => void) | null = null
    const transport = vi.fn((fn: (ev: ReleaseEvent) => void) => {
      capture = fn
      return { close: vi.fn() }
    })
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        eventTransport={transport}
      />,
    )
    expect(transport).toHaveBeenCalled()

    // queued → docker queued → started → succeeded.
    act(() => {
      capture!({
        event: "software_workspace.release.queued",
        data: { session_id: "session-x", release_id: "rel-1" },
      })
    })
    expect(
      screen
        .getByTestId("software-release-dashboard")
        .getAttribute("data-rollup"),
    ).toBe("idle")

    act(() => {
      capture!({
        event: "software_workspace.release.target_started",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "docker",
          progress: 30,
        },
      })
    })
    expect(
      screen
        .getByTestId("software-release-dashboard-row-docker")
        .getAttribute("data-status"),
    ).toBe("building")
    expect(
      screen
        .getByTestId("software-release-dashboard")
        .getAttribute("data-rollup"),
    ).toBe("in_progress")

    act(() => {
      capture!({
        event: "software_workspace.release.target_succeeded",
        data: {
          session_id: "session-x",
          release_id: "rel-1",
          target: "docker",
          filename: "image.tar",
          download_url: "/artifacts/image.tar",
          byte_size: 9_000_000,
        },
      })
    })
    expect(
      screen
        .getByTestId("software-release-dashboard-row-docker")
        .getAttribute("data-status"),
    ).toBe("passed")
    expect(
      screen.getByTestId("software-release-dashboard-row-docker-artifact-name")
        .textContent,
    ).toBe("image.tar")
  })

  it("closes the event transport on unmount", () => {
    const close = vi.fn()
    const transport = vi.fn(() => ({ close }))
    const { unmount } = render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        eventTransport={transport}
      />,
    )
    unmount()
    expect(close).toHaveBeenCalledTimes(1)
  })

  it("ignores SSE events for a different session", () => {
    let capture: ((ev: ReleaseEvent) => void) | null = null
    const transport = vi.fn((fn: (ev: ReleaseEvent) => void) => {
      capture = fn
      return { close: vi.fn() }
    })
    render(
      <SoftwareReleaseDashboard
        sessionId="session-x"
        releaseId="rel-1"
        eventTransport={transport}
      />,
    )
    act(() => {
      capture!({
        event: "software_workspace.release.target_succeeded",
        data: {
          session_id: "OTHER",
          release_id: "rel-1",
          target: "docker",
          filename: "image.tar",
          download_url: "/x",
        },
      })
    })
    expect(
      screen
        .getByTestId("software-release-dashboard-row-docker")
        .getAttribute("data-status"),
    ).toBe("pending")
  })
})
