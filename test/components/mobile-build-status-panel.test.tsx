/**
 * V7 #4 (TODO row 2694 / issue #323) — Contract tests for
 * `mobile-build-status-panel.tsx`.
 *
 * Covers the three operator-visible contracts the panel ships:
 *
 *   1. Pure helpers — duration, byte-size, phase classifier, progress
 *      clamp, ring buffer, etc.  These are what the backend translator
 *      shares with the UI, so stable inputs → stable outputs is the
 *      only way to make sure the wire format holds up.
 *   2. SSE reducer — every `mobile_workspace.build.*` event mutates
 *      the run in the exact shape the header / errors / artifacts
 *      sections render.
 *   3. Rendering — happy path for idle / running / succeeded / failed
 *      states, plus the error-row and artifact-row shape each
 *      surface as their own assertions.
 *
 * Event-namespace disjointness is verified against V6/V7 sibling
 * namespaces so a future refactor that merges busses can catch the
 * collision at CI time.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import {
  ARTIFACT_KIND_LABELS,
  DEFAULT_MAX_ERRORS,
  DEFAULT_MAX_LOG_LINES,
  DEFAULT_MAX_WARNINGS,
  MOBILE_BUILD_EVENT_NAMES,
  MOBILE_BUILD_EVENT_PREFIX,
  MobileBuildStatusPanel,
  TOOL_LABELS,
  applyBuildEvent,
  buildStatusColorVar,
  buildStatusLabel,
  classifyBuildPhase,
  clampBuildProgress,
  defaultToolForPlatform,
  elapsedBuildMs,
  emptyBuildRun,
  expectedArtifactKinds,
  formatBuildByteSize,
  formatBuildDuration,
  isTerminalBuildStatus,
  matchBuildEvent,
  pushRingBuffer,
  shortenBuildPath,
  type MobileBuildEvent,
  type MobileBuildRun,
} from "@/components/omnisight/mobile-build-status-panel"

// ─── Helpers ────────────────────────────────────────────────────────────────

function makeRun(overrides: Partial<MobileBuildRun> = {}): MobileBuildRun {
  return {
    ...emptyBuildRun("session-x", "ios"),
    ...overrides,
  }
}

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("defaultToolForPlatform", () => {
  it("maps ios → xcodebuild, android → gradle, flutter → flutter, rn → react-native-cli", () => {
    expect(defaultToolForPlatform("ios")).toBe("xcodebuild")
    expect(defaultToolForPlatform("android")).toBe("gradle")
    expect(defaultToolForPlatform("flutter")).toBe("flutter")
    expect(defaultToolForPlatform("react-native")).toBe("react-native-cli")
  })
})

describe("expectedArtifactKinds", () => {
  it("iOS only produces .ipa", () => {
    expect(expectedArtifactKinds("ios")).toEqual(["ipa"])
  })
  it("Android produces .apk and .aab", () => {
    expect(expectedArtifactKinds("android")).toEqual(["apk", "aab"])
  })
  it("Flutter / RN span both platforms", () => {
    expect(expectedArtifactKinds("flutter")).toEqual(["ipa", "apk"])
    expect(expectedArtifactKinds("react-native")).toEqual(["ipa", "apk"])
  })
})

describe("buildStatusLabel", () => {
  it("maps each status to the expected label", () => {
    expect(buildStatusLabel("idle")).toBe("Idle")
    expect(buildStatusLabel("queued")).toBe("Queued")
    expect(buildStatusLabel("running")).toBe("Building")
    expect(buildStatusLabel("succeeded")).toBe("Succeeded")
    expect(buildStatusLabel("failed")).toBe("Failed")
    expect(buildStatusLabel("cancelled")).toBe("Cancelled")
  })
})

describe("buildStatusColorVar", () => {
  it("green for succeeded, red for failed, muted for cancelled / idle, blue for in-flight", () => {
    expect(buildStatusColorVar("succeeded")).toBe("var(--validation-emerald)")
    expect(buildStatusColorVar("failed")).toBe("var(--critical-red)")
    expect(buildStatusColorVar("cancelled")).toBe("var(--muted-foreground)")
    expect(buildStatusColorVar("running")).toBe("var(--neural-blue)")
    expect(buildStatusColorVar("queued")).toBe("var(--neural-blue)")
    expect(buildStatusColorVar("idle")).toBe("var(--muted-foreground)")
  })
})

describe("isTerminalBuildStatus", () => {
  it("returns true only for succeeded / failed / cancelled", () => {
    expect(isTerminalBuildStatus("succeeded")).toBe(true)
    expect(isTerminalBuildStatus("failed")).toBe(true)
    expect(isTerminalBuildStatus("cancelled")).toBe(true)
    expect(isTerminalBuildStatus("idle")).toBe(false)
    expect(isTerminalBuildStatus("queued")).toBe(false)
    expect(isTerminalBuildStatus("running")).toBe(false)
  })
})

describe("formatBuildDuration", () => {
  it("formats seconds / minutes / hours", () => {
    expect(formatBuildDuration(500)).toBe("0s")
    expect(formatBuildDuration(1_000)).toBe("1s")
    expect(formatBuildDuration(45_500)).toBe("45s")
    expect(formatBuildDuration(61_000)).toBe("1m 1s")
    expect(formatBuildDuration(3_600_000)).toBe("1h 0m 0s")
    expect(formatBuildDuration(3_723_000)).toBe("1h 2m 3s")
  })
  it("returns dash for null / negative / NaN", () => {
    expect(formatBuildDuration(null)).toBe("—")
    expect(formatBuildDuration(undefined)).toBe("—")
    expect(formatBuildDuration(-1)).toBe("—")
    expect(formatBuildDuration(Number.NaN)).toBe("—")
    expect(formatBuildDuration(Number.POSITIVE_INFINITY)).toBe("—")
  })
})

describe("formatBuildByteSize", () => {
  it("picks the right unit at each boundary", () => {
    expect(formatBuildByteSize(0)).toBe("0 B")
    expect(formatBuildByteSize(512)).toBe("512 B")
    expect(formatBuildByteSize(2048)).toBe("2.0 KB")
    expect(formatBuildByteSize(5 * 1024 * 1024)).toBe("5.0 MB")
    expect(formatBuildByteSize(1024 * 1024 * 1024)).toBe("1.00 GB")
  })
  it("returns dash for null / negative / NaN", () => {
    expect(formatBuildByteSize(null)).toBe("—")
    expect(formatBuildByteSize(undefined)).toBe("—")
    expect(formatBuildByteSize(-1)).toBe("—")
    expect(formatBuildByteSize(Number.NaN)).toBe("—")
  })
})

describe("shortenBuildPath", () => {
  it("returns the input when it is short enough", () => {
    expect(shortenBuildPath("a.swift")).toBe("a.swift")
  })
  it("collapses leading path into an ellipsis", () => {
    const long = "ios/App/Modules/Submodule/Deep/Deeper/ContentView.swift"
    const short = shortenBuildPath(long, 20)
    expect(short.startsWith("…")).toBe(true)
    expect(short.length).toBeLessThanOrEqual(20)
  })
  it("handles null / undefined", () => {
    expect(shortenBuildPath(null)).toBe("")
    expect(shortenBuildPath(undefined)).toBe("")
  })
})

describe("clampBuildProgress", () => {
  it("clamps to [0, 100]", () => {
    expect(clampBuildProgress(-10)).toBe(0)
    expect(clampBuildProgress(150)).toBe(100)
    expect(clampBuildProgress(50)).toBe(50)
  })
  it("returns null for null / NaN", () => {
    expect(clampBuildProgress(null)).toBeNull()
    expect(clampBuildProgress(undefined)).toBeNull()
    expect(clampBuildProgress(Number.NaN)).toBeNull()
    expect(clampBuildProgress(Number.POSITIVE_INFINITY)).toBeNull()
  })
})

describe("classifyBuildPhase", () => {
  it("groups Xcode + Gradle vendor strings into semantic buckets", () => {
    expect(classifyBuildPhase("queued")).toBe("queued")
    expect(classifyBuildPhase("CreateBuildDirectory")).toBe("configuring")
    expect(classifyBuildPhase("configure")).toBe("configuring")
    expect(classifyBuildPhase("CompileSwift")).toBe("compiling")
    expect(classifyBuildPhase("compileKotlin")).toBe("compiling")
    expect(classifyBuildPhase("metro")).toBe("compiling")
    expect(classifyBuildPhase("Ld")).toBe("linking")
    expect(classifyBuildPhase("packageRelease")).toBe("packaging")
    expect(classifyBuildPhase("assembleRelease")).toBe("packaging")
    expect(classifyBuildPhase("CodeSign")).toBe("signing")
    expect(classifyBuildPhase("provisioning")).toBe("signing")
    expect(classifyBuildPhase("Export")).toBe("exporting")
    expect(classifyBuildPhase("uploadIPA")).toBe("exporting")
  })
  it("unknown / empty degrades to other", () => {
    expect(classifyBuildPhase("")).toBe("other")
    expect(classifyBuildPhase("weird.vendor.phase.xyz")).toBe("other")
  })
})

describe("pushRingBuffer", () => {
  it("appends, dedups by id, and caps", () => {
    type X = { id: string; v: number }
    const existing: X[] = [{ id: "a", v: 1 }, { id: "b", v: 2 }]
    const next: X[] = [{ id: "b", v: 99 }, { id: "c", v: 3 }, { id: "d", v: 4 }]
    const out = pushRingBuffer(existing, next, 3)
    // Dedup keeps the first occurrence; cap keeps last 3 → b (original), c, d.
    expect(out.map((e) => e.id)).toEqual(["b", "c", "d"])
  })
  it("cap 0 returns an empty list", () => {
    expect(pushRingBuffer([{ id: "a" }], [{ id: "b" }], 0)).toEqual([])
  })
})

describe("elapsedBuildMs", () => {
  it("returns null without startedAt", () => {
    expect(elapsedBuildMs({ startedAt: undefined, finishedAt: undefined, status: "idle" })).toBeNull()
  })
  it("uses finishedAt for terminal runs, now for in-flight", () => {
    const now = 1_700_000_000_000
    expect(
      elapsedBuildMs(
        { startedAt: new Date(now - 5000).toISOString(), finishedAt: undefined, status: "running" },
        now,
      ),
    ).toBe(5000)
    expect(
      elapsedBuildMs(
        {
          startedAt: new Date(now - 5000).toISOString(),
          finishedAt: new Date(now - 1000).toISOString(),
          status: "succeeded",
        },
        now,
      ),
    ).toBe(4000)
  })
})

describe("emptyBuildRun", () => {
  it("seeds a sensible idle run", () => {
    const run = emptyBuildRun("session-abc", "android")
    expect(run).toMatchObject({
      sessionId: "session-abc",
      platform: "android",
      status: "idle",
      tool: "gradle",
      variant: "debug",
      progress: null,
      errors: [],
      warnings: [],
      artifacts: [],
      logTail: [],
      exitCode: null,
    })
  })
})

// ─── SSE event matching + reducer ──────────────────────────────────────────

describe("matchBuildEvent", () => {
  it("rejects events outside the build namespace", () => {
    expect(
      matchBuildEvent(
        { event: "mobile_workspace.iteration_timeline.recorded", data: { session_id: "s" } },
        "s",
        null,
      ),
    ).toBe(false)
  })
  it("rejects events for a different session", () => {
    expect(
      matchBuildEvent(
        { event: "mobile_workspace.build.progress", data: { session_id: "other" } },
        "mine",
        null,
      ),
    ).toBe(false)
  })
  it("accepts events that match the session and (optionally) the build id", () => {
    expect(
      matchBuildEvent(
        { event: "mobile_workspace.build.started", data: { session_id: "s", build_id: "b" } },
        "s",
        null,
      ),
    ).toBe(true)
    expect(
      matchBuildEvent(
        { event: "mobile_workspace.build.progress", data: { session_id: "s", build_id: "b" } },
        "s",
        "b",
      ),
    ).toBe(true)
    expect(
      matchBuildEvent(
        { event: "mobile_workspace.build.progress", data: { session_id: "s", build_id: "c" } },
        "s",
        "b",
      ),
    ).toBe(false)
  })
  it("treats the camelCase alias as equivalent", () => {
    expect(
      matchBuildEvent(
        { event: "mobile_workspace.build.started", data: { sessionId: "s", buildId: "b" } },
        "s",
        null,
      ),
    ).toBe(true)
  })
})

describe("applyBuildEvent", () => {
  const base: MobileBuildRun = makeRun()

  it("queued seeds the build id + resets buffers", () => {
    const ev: MobileBuildEvent = {
      event: "mobile_workspace.build.queued",
      data: { session_id: "session-x", build_id: "b-42", tool: "gradle", variant: "release" },
    }
    const out = applyBuildEvent(base, ev)
    expect(out.status).toBe("queued")
    expect(out.buildId).toBe("b-42")
    expect(out.tool).toBe("gradle")
    expect(out.variant).toBe("release")
    expect(out.errors).toEqual([])
    expect(out.artifacts).toEqual([])
    expect(out.progress).toBe(0)
  })

  it("started flips to running and classifies the phase", () => {
    const after = applyBuildEvent(
      { ...base, status: "queued", buildId: "b-1" },
      {
        event: "mobile_workspace.build.started",
        data: { session_id: "session-x", build_id: "b-1", phase: "CompileSwift", progress: 12 },
      },
    )
    expect(after.status).toBe("running")
    expect(after.phase).toBe("compiling")
    expect(after.progress).toBe(12)
    expect(after.startedAt).toBeTruthy()
  })

  it("progress updates phase + progress only (no status change when running)", () => {
    const after = applyBuildEvent(
      { ...base, status: "running", buildId: "b-1" },
      {
        event: "mobile_workspace.build.progress",
        data: { session_id: "session-x", build_id: "b-1", phase: "Ld", progress: 55, detail: "link-time" },
      },
    )
    expect(after.status).toBe("running")
    expect(after.phase).toBe("linking")
    expect(after.progress).toBe(55)
    expect(after.phaseDetail).toBe("link-time")
  })

  it("progress auto-promotes queued → running when the first tick lands", () => {
    const after = applyBuildEvent(
      { ...base, status: "queued", buildId: "b-1" },
      {
        event: "mobile_workspace.build.progress",
        data: { session_id: "session-x", build_id: "b-1", progress: 5 },
      },
    )
    expect(after.status).toBe("running")
  })

  it("error appends to the errors ring buffer", () => {
    const after = applyBuildEvent(
      base,
      {
        event: "mobile_workspace.build.error",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          file: "ios/App/ContentView.swift",
          line: 42,
          column: 17,
          message: "cannot find 'Foo' in scope",
          category: "compile",
          snippet: "let x = Foo()",
        },
      },
    )
    expect(after.errors).toHaveLength(1)
    expect(after.errors[0]).toMatchObject({
      file: "ios/App/ContentView.swift",
      line: 42,
      column: 17,
      message: "cannot find 'Foo' in scope",
      category: "compile",
      severity: "error",
    })
  })

  it("error with severity=warning lands in warnings instead", () => {
    const after = applyBuildEvent(
      base,
      {
        event: "mobile_workspace.build.error",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          severity: "warning",
          message: "deprecated API",
        },
      },
    )
    expect(after.errors).toHaveLength(0)
    expect(after.warnings).toHaveLength(1)
    expect(after.warnings[0].severity).toBe("warning")
  })

  it("artifact appends, preserving kind inferred from filename", () => {
    const after = applyBuildEvent(
      base,
      {
        event: "mobile_workspace.build.artifact",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          filename: "app-release.apk",
          download_url: "/artifacts/app-release.apk",
          size: 42 * 1024 * 1024,
          sha256: "abc123",
        },
      },
    )
    expect(after.artifacts).toHaveLength(1)
    expect(after.artifacts[0]).toMatchObject({
      filename: "app-release.apk",
      kind: "apk",
      downloadUrl: "/artifacts/app-release.apk",
      byteSize: 42 * 1024 * 1024,
      sha256: "abc123",
    })
  })

  it("artifact with explicit kind overrides extension inference", () => {
    const after = applyBuildEvent(
      base,
      {
        event: "mobile_workspace.build.artifact",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          filename: "unknown.blob",
          download_url: "/blob",
          kind: "dsym",
        },
      },
    )
    expect(after.artifacts[0].kind).toBe("dsym")
  })

  it("completed marks succeeded + exit 0 + progress 100", () => {
    const after = applyBuildEvent(
      { ...base, status: "running", buildId: "b-1" },
      {
        event: "mobile_workspace.build.completed",
        data: { session_id: "session-x", build_id: "b-1" },
      },
    )
    expect(after.status).toBe("succeeded")
    expect(after.exitCode).toBe(0)
    expect(after.progress).toBe(100)
    expect(after.finishedAt).toBeTruthy()
  })

  it("failed marks failed + captures exit code + reason", () => {
    const after = applyBuildEvent(
      { ...base, status: "running", buildId: "b-1" },
      {
        event: "mobile_workspace.build.failed",
        data: { session_id: "session-x", build_id: "b-1", exit_code: 65, reason: "code sign failed" },
      },
    )
    expect(after.status).toBe("failed")
    expect(after.exitCode).toBe(65)
    expect(after.failureReason).toBe("code sign failed")
  })

  it("cancelled marks cancelled + default reason", () => {
    const after = applyBuildEvent(
      { ...base, status: "running", buildId: "b-1" },
      {
        event: "mobile_workspace.build.cancelled",
        data: { session_id: "session-x", build_id: "b-1" },
      },
    )
    expect(after.status).toBe("cancelled")
    expect(after.failureReason).toBe("Cancelled by operator")
  })

  it("log appends to log tail ring buffer + caps it", () => {
    let run = base
    for (let i = 0; i < DEFAULT_MAX_LOG_LINES + 10; i++) {
      run = applyBuildEvent(run, {
        event: "mobile_workspace.build.log",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          id: `line-${i}`,
          text: `line ${i}`,
          level: "info",
          ts: new Date().toISOString(),
        },
      })
    }
    expect(run.logTail.length).toBe(DEFAULT_MAX_LOG_LINES)
    expect(run.logTail[run.logTail.length - 1].text).toBe(
      `line ${DEFAULT_MAX_LOG_LINES + 10 - 1}`,
    )
  })

  it("caps errors list at DEFAULT_MAX_ERRORS", () => {
    let run = base
    for (let i = 0; i < DEFAULT_MAX_ERRORS + 5; i++) {
      run = applyBuildEvent(run, {
        event: "mobile_workspace.build.error",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          id: `err-${i}`,
          message: `err ${i}`,
          file: "a.swift",
          line: i + 1,
        },
      })
    }
    expect(run.errors.length).toBe(DEFAULT_MAX_ERRORS)
  })

  it("caps warnings list at DEFAULT_MAX_WARNINGS", () => {
    let run = base
    for (let i = 0; i < DEFAULT_MAX_WARNINGS + 5; i++) {
      run = applyBuildEvent(run, {
        event: "mobile_workspace.build.error",
        data: {
          session_id: "session-x",
          build_id: "b-1",
          id: `warn-${i}`,
          message: `warn ${i}`,
          severity: "warning",
        },
      })
    }
    expect(run.warnings.length).toBe(DEFAULT_MAX_WARNINGS)
  })

  it("unknown sub-events are a no-op rather than throwing", () => {
    const out = applyBuildEvent(base, {
      event: "mobile_workspace.build.unknown_kind",
      data: { session_id: "session-x" },
    })
    expect(out).toEqual(base)
  })
})

// ─── Event namespace disjointness ──────────────────────────────────────────

describe("MOBILE_BUILD_EVENT_NAMES", () => {
  it("has one entry per documented event name, all prefixed with the build namespace", () => {
    for (const name of MOBILE_BUILD_EVENT_NAMES) {
      expect(name.startsWith(MOBILE_BUILD_EVENT_PREFIX)).toBe(true)
    }
    expect(new Set(MOBILE_BUILD_EVENT_NAMES).size).toBe(MOBILE_BUILD_EVENT_NAMES.length)
  })
  it("is disjoint from V6 / V7 sibling SSE namespaces", () => {
    const buildSet = new Set<string>(MOBILE_BUILD_EVENT_NAMES)
    const siblings = [
      "mobile_workspace.iteration_timeline.recording",
      "mobile_workspace.iteration_timeline.recorded",
      "mobile_workspace.iteration_timeline.reset",
      "mobile_workspace.iteration_timeline.record_failed",
      "mobile_sandbox.state",
      "mobile_sandbox.agent_visual_context.emit",
      "mobile_sandbox.autofix.start",
      "ui_sandbox.mobile_annotation_context.emit",
    ]
    for (const s of siblings) {
      expect(buildSet.has(s)).toBe(false)
    }
  })
})

// ─── Rendering ─────────────────────────────────────────────────────────────

describe("<MobileBuildStatusPanel /> — rendering contracts", () => {
  it("renders the idle empty state when given an idle run", () => {
    const transport = vi.fn(() => ({ close: vi.fn() }))
    render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        eventTransport={transport}
      />,
    )
    const root = screen.getByTestId("mobile-build-status-panel")
    expect(root.getAttribute("data-status")).toBe("idle")
    expect(screen.getByTestId("mobile-build-status-panel-status-badge").textContent).toMatch(
      /Idle/,
    )
    expect(screen.getByTestId("mobile-build-status-panel-tool-badge").textContent).toBe(
      TOOL_LABELS.xcodebuild,
    )
    expect(screen.getByTestId("mobile-build-status-panel-errors-empty")).toBeInTheDocument()
    expect(screen.getByTestId("mobile-build-status-panel-artifacts-empty")).toBeInTheDocument()
    expect(
      screen.getByTestId("mobile-build-status-panel-progress-label").textContent,
    ).toBe("—")
  })

  it("renders errors + artifacts when given a failed-with-partial-output run", () => {
    const run: MobileBuildRun = makeRun({
      buildId: "b-11",
      status: "failed",
      phase: "compiling",
      progress: 47,
      exitCode: 65,
      failureReason: "Swift compile error",
      errors: [
        {
          id: "e1",
          severity: "error",
          category: "compile",
          file: "ios/App/ContentView.swift",
          line: 42,
          column: 17,
          message: "cannot find 'Foo' in scope",
        },
      ],
      artifacts: [
        {
          id: "a1",
          filename: "App.ipa",
          kind: "ipa",
          downloadUrl: "/artifacts/App.ipa",
          byteSize: 12 * 1024 * 1024,
          sha256: "deadbeefcafebabedeadbeefcafebabedeadbeefcafebabedeadbeefcafebabe",
        },
      ],
    })
    render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        run={run}
      />,
    )
    expect(
      screen.getByTestId("mobile-build-status-panel").getAttribute("data-status"),
    ).toBe("failed")
    expect(
      screen.getByTestId("mobile-build-status-panel-failure-reason").textContent,
    ).toContain("Swift compile error")
    // Error row — location + message.
    expect(screen.getByTestId("mobile-build-error-e1-location").textContent).toMatch(
      /ContentView.swift:42:17/,
    )
    expect(screen.getByTestId("mobile-build-error-e1-message").textContent).toBe(
      "cannot find 'Foo' in scope",
    )
    // Artifact row — label + size + download link.
    expect(screen.getByTestId("mobile-build-artifact-a1-name").textContent).toBe("App.ipa")
    expect(screen.getByTestId("mobile-build-artifact-a1-size").textContent).toBe("12.0 MB")
    const downloadLink = screen.getByTestId("mobile-build-artifact-a1-download")
    expect(downloadLink.tagName.toLowerCase()).toBe("a")
    expect(downloadLink.getAttribute("href")).toBe("/artifacts/App.ipa")
    // Counts surfaced in the section headers.
    expect(screen.getByTestId("mobile-build-status-panel-errors-count").textContent).toBe("1")
    expect(
      screen.getByTestId("mobile-build-status-panel-artifacts-count").textContent,
    ).toBe("1")
  })

  it("shows the progress percentage when present, indeterminate otherwise", () => {
    const { rerender } = render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="android"
        run={makeRun({ platform: "android", tool: "gradle", status: "running", progress: 72 })}
      />,
    )
    expect(screen.getByTestId("mobile-build-status-panel-progress-label").textContent).toBe("72%")
    expect(
      screen
        .getByTestId("mobile-build-status-panel-progress-bar")
        .getAttribute("data-indeterminate"),
    ).toBe("false")

    rerender(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="android"
        run={makeRun({ platform: "android", tool: "gradle", status: "running", progress: null })}
      />,
    )
    expect(screen.getByTestId("mobile-build-status-panel-progress-label").textContent).toBe("—")
    expect(
      screen
        .getByTestId("mobile-build-status-panel-progress-bar")
        .getAttribute("data-indeterminate"),
    ).toBe("true")
  })

  it("wires the Start / Cancel / Retry buttons to the host callbacks", () => {
    const onStart = vi.fn()
    const onCancel = vi.fn()
    const onRetry = vi.fn()

    const { rerender } = render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        onStart={onStart}
        onCancel={onCancel}
        onRetry={onRetry}
      />,
    )
    // Idle → Start is visible.
    fireEvent.click(screen.getByTestId("mobile-build-status-panel-start"))
    expect(onStart).toHaveBeenCalledTimes(1)

    // Running → Cancel is visible, Start / Retry gone.
    rerender(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        onStart={onStart}
        onCancel={onCancel}
        onRetry={onRetry}
        run={makeRun({ buildId: "b-7", status: "running" })}
      />,
    )
    fireEvent.click(screen.getByTestId("mobile-build-status-panel-cancel"))
    expect(onCancel).toHaveBeenCalledWith("b-7")
    expect(screen.queryByTestId("mobile-build-status-panel-start")).toBeNull()

    // Failed → Retry is visible.
    rerender(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        onStart={onStart}
        onCancel={onCancel}
        onRetry={onRetry}
        run={makeRun({ buildId: "b-7", status: "failed" })}
      />,
    )
    fireEvent.click(screen.getByTestId("mobile-build-status-panel-retry"))
    expect(onRetry).toHaveBeenCalledWith("b-7")
  })

  it("calls onDownloadArtifact callback when injected, rather than rendering a bare link", () => {
    const onDownload = vi.fn()
    render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="android"
        onDownloadArtifact={onDownload}
        run={makeRun({
          buildId: "b-9",
          platform: "android",
          tool: "gradle",
          status: "succeeded",
          artifacts: [
            {
              id: "a-apk",
              filename: "app-release.apk",
              kind: "apk",
              downloadUrl: "/artifacts/app-release.apk",
              byteSize: 9 * 1024 * 1024,
            },
          ],
        })}
      />,
    )
    const btn = screen.getByTestId("mobile-build-artifact-a-apk-download")
    fireEvent.click(btn)
    expect(onDownload).toHaveBeenCalledTimes(1)
    expect(onDownload.mock.calls[0][0]).toMatchObject({
      filename: "app-release.apk",
      kind: "apk",
    })
  })

  it("feeds SSE events from an injected transport into the reducer", () => {
    let capture: ((ev: MobileBuildEvent) => void) | null = null
    const transport = vi.fn((fn: (ev: MobileBuildEvent) => void) => {
      capture = fn
      return { close: vi.fn() }
    })
    render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        eventTransport={transport}
      />,
    )
    expect(transport).toHaveBeenCalled()

    // queued → started → progress → completed should tick the panel through its states.
    act(() => {
      capture!({
        event: "mobile_workspace.build.queued",
        data: { session_id: "session-x", build_id: "b-q1" },
      })
    })
    expect(
      screen.getByTestId("mobile-build-status-panel").getAttribute("data-status"),
    ).toBe("queued")

    act(() => {
      capture!({
        event: "mobile_workspace.build.started",
        data: { session_id: "session-x", build_id: "b-q1", phase: "CompileSwift", progress: 10 },
      })
    })
    expect(
      screen.getByTestId("mobile-build-status-panel").getAttribute("data-status"),
    ).toBe("running")
    expect(
      screen.getByTestId("mobile-build-status-panel").getAttribute("data-phase"),
    ).toBe("compiling")

    act(() => {
      capture!({
        event: "mobile_workspace.build.error",
        data: {
          session_id: "session-x",
          build_id: "b-q1",
          id: "e-1",
          message: "oh no",
        },
      })
    })
    expect(screen.getByTestId("mobile-build-error-e-1-message").textContent).toBe("oh no")

    act(() => {
      capture!({
        event: "mobile_workspace.build.artifact",
        data: {
          session_id: "session-x",
          build_id: "b-q1",
          id: "a-1",
          filename: "App.ipa",
          download_url: "/App.ipa",
        },
      })
    })
    expect(screen.getByTestId("mobile-build-artifact-a-1-name").textContent).toBe("App.ipa")

    act(() => {
      capture!({
        event: "mobile_workspace.build.completed",
        data: { session_id: "session-x", build_id: "b-q1" },
      })
    })
    expect(
      screen.getByTestId("mobile-build-status-panel").getAttribute("data-status"),
    ).toBe("succeeded")
  })

  it("closes the event transport on unmount", () => {
    const close = vi.fn()
    const transport = vi.fn(() => ({ close }))
    const { unmount } = render(
      <MobileBuildStatusPanel
        sessionId="session-x"
        platform="ios"
        eventTransport={transport}
      />,
    )
    unmount()
    expect(close).toHaveBeenCalledTimes(1)
  })

  it("surfaces expected artifact kinds in the empty-artifacts placeholder per platform", () => {
    const { rerender } = render(
      <MobileBuildStatusPanel sessionId="session-x" platform="ios" />,
    )
    expect(
      screen.getByTestId("mobile-build-status-panel-artifacts-empty").textContent,
    ).toMatch(/\.ipa/)

    rerender(<MobileBuildStatusPanel sessionId="session-x" platform="android" />)
    expect(
      screen.getByTestId("mobile-build-status-panel-artifacts-empty").textContent,
    ).toMatch(/\.apk/)
    expect(
      screen.getByTestId("mobile-build-status-panel-artifacts-empty").textContent,
    ).toMatch(/\.aab/)
  })
})

describe("ARTIFACT_KIND_LABELS", () => {
  it("labels every artifact kind", () => {
    const kinds = ["ipa", "apk", "aab", "dsym", "mapping", "other"] as const
    for (const k of kinds) {
      expect(ARTIFACT_KIND_LABELS[k]).toBeTruthy()
    }
  })
})
