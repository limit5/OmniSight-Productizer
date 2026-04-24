/**
 * V7 #4 (TODO row 2694 / issue #323) — Mobile Build status panel.
 *
 * Live operator view of the Xcode / Gradle (and Flutter / React-Native
 * meta-tool) build pipeline that sits next to the Mobile Workspace.
 * Three concrete surfaces:
 *
 *   1. A header strip — tool + platform + device + variant + phase +
 *      % progress + elapsed — gives the operator an at-a-glance answer
 *      to "what is the toolchain currently doing and how far through is
 *      it".
 *   2. An error list — compile / link / signing errors surfaced with
 *      `file:line:column` links and a short snippet so the operator can
 *      jump straight to the offending call-site without scrolling raw
 *      build log.  Errors are deduped by `(file, line, message)` to
 *      stop Gradle's repeated-phase noise from drowning the panel.
 *   3. Artifact list — every `.ipa` / `.apk` (plus the occasional
 *      `.aab` / `.dSYM.zip`) the backend publishes gets a one-click
 *      download link with byte size + sha256 so the operator can ship
 *      to a device tester or drop straight into the Store dashboard
 *      (V7 row "Store submission dashboard", tracked separately).
 *
 * Live wire-up:
 *   The panel subscribes to the shared SSE stream (lib/api
 *   `subscribeEvents`) and filters for the `mobile_workspace.build.*`
 *   namespace.  Five event names, disjoint from V7 #2
 *   `mobile_workspace.iteration_timeline.*`:
 *
 *     - `mobile_workspace.build.queued`      — dispatcher accepted run
 *     - `mobile_workspace.build.started`     — tool spawned
 *     - `mobile_workspace.build.progress`    — phase / percentage tick
 *     - `mobile_workspace.build.log`         — line(s) for the log tail
 *     - `mobile_workspace.build.error`       — compile / link error
 *     - `mobile_workspace.build.artifact`    — .ipa / .apk published
 *     - `mobile_workspace.build.completed`   — succeeded
 *     - `mobile_workspace.build.failed`      — exit code != 0
 *     - `mobile_workspace.build.cancelled`   — operator-initiated abort
 *
 *   All events MUST carry `session_id` + `build_id` (or
 *   `buildId`); panels mount against a single `sessionId` so cross-
 *   session pollution is impossible.  When the panel's session is
 *   `null` (unmounted / not yet bound) the SSE listener still attaches
 *   but every event is dropped — matches the V0 #6 workspace-scoped
 *   routing contract for cross-surface SSE fan-out.
 *
 * Module-global state audit (SOP Step 1):
 *   N/A — zero module-level mutable state; all state lives in React
 *   `useState` scoped per component instance; SSE subscription uses
 *   the shared `EventSource` owned by `lib/api` which is already
 *   multi-worker / multi-replica safe (the single-origin fan-out is
 *   done in the browser, not the server).
 *
 * Out-of-scope (V7 backlog rows):
 *   - Full build-log viewer with regex search — this panel shows a
 *     tailing log, not a scrollback archive.
 *   - Artifact signature verification UI — the backend attaches
 *     `sha256`; we surface it but the panel does not recompute it.
 *   - `TestFlight` / `Firebase App Distribution` one-click dispatch —
 *     that is the Store submission dashboard's job (V7 row #5).
 *
 * Intentional non-goals:
 *   - The panel does NOT itself start a build; `onStart` / `onCancel`
 *     / `onRetry` / `onDownloadArtifact` are opt-in callbacks the host
 *     page wires up.  This keeps the component a pure render surface.
 */
"use client"

import * as React from "react"
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleStop,
  Clock,
  Cog,
  Download,
  FileDown,
  FileWarning,
  FlaskConical,
  Hammer,
  HardDriveDownload,
  Loader2,
  Package,
  PackageCheck,
  Play,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Separator } from "@/components/ui/separator"

import { subscribeEvents } from "@/lib/api"
import {
  DEVICE_PROFILES,
  type DeviceProfileId,
} from "@/components/omnisight/device-frame"
import type { MobilePlatform } from "@/components/omnisight/mobile-visual-annotator"

// ─── Public shapes ─────────────────────────────────────────────────────────

/**
 * Build tool driving the run.  The panel selects an icon / label from
 * this — the actual tool invocation lives in backend orchestration.
 * Flutter + RN sit alongside Xcode + Gradle because Flutter (`flutter
 * build ios` / `flutter build apk`) and React-Native (`npx react-native
 * run-ios` / `./gradlew assembleRelease`) both wrap the native tool so
 * the log semantics differ enough that surfacing the wrapper matters.
 */
export type MobileBuildTool =
  | "xcodebuild"
  | "gradle"
  | "flutter"
  | "react-native-cli"

/**
 * Phase labels mirror the ordered lifecycle shared by both Xcode
 * (`CreateBuildDirectory` → `CompileC` → `Ld` → `CodeSign` → `Export`)
 * and Gradle (`configure` → `compileKotlin` → `mergeResources` →
 * `packageRelease` → `signingConfig`).  We collapse both vendors'
 * dozens of intermediate steps into the 6 semantic buckets the
 * operator actually reads off the panel.  Unknown / vendor-specific
 * phases fall back to `"other"` so the panel never hides progress.
 */
export type MobileBuildPhase =
  | "queued"
  | "configuring"
  | "compiling"
  | "linking"
  | "packaging"
  | "signing"
  | "exporting"
  | "other"

/**
 * Terminal statuses are `succeeded` / `failed` / `cancelled`; the rest
 * are in-flight.  `idle` means the panel has never seen a build — the
 * empty state.
 */
export type MobileBuildStatus =
  | "idle"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"

/** Variant — matches `MobileBuildConfig.variant` on the workspace page. */
export type MobileBuildVariant = "debug" | "release"

/** Error severity surfaced in the panel. */
export type MobileBuildErrorSeverity = "error" | "warning"

export interface MobileBuildError {
  /** Stable id — usually `${file}:${line}:${col}:${messageHash}`. */
  id: string
  /** Severity — errors always surface; warnings fold into a collapser. */
  severity: MobileBuildErrorSeverity
  /** Short error category (`compile` / `link` / `sign` / ...). */
  category?: string
  /** Source path.  May be relative to project root. */
  file?: string
  /** 1-indexed line number. */
  line?: number
  /** 1-indexed column number. */
  column?: number
  /** The error message itself. */
  message: string
  /** Short code snippet the toolchain surfaced (optional). */
  snippet?: string
  /** ISO8601 timestamp the error was produced. */
  observedAt?: string
}

export interface MobileBuildArtifact {
  /** Stable id — usually matches filename. */
  id: string
  /** Human filename (`App.ipa`, `app-release.apk`). */
  filename: string
  /** Artifact kind — drives the badge + icon. */
  kind: "ipa" | "apk" | "aab" | "dsym" | "mapping" | "other"
  /** Download URL (authenticated or signed — panel doesn't care). */
  downloadUrl: string
  /** Size in bytes for the size badge. */
  byteSize?: number
  /** Hex sha256 for integrity copy-paste. */
  sha256?: string
  /** ISO8601 timestamp the artifact finished uploading. */
  createdAt?: string
}

export interface MobileBuildLogLine {
  /** Stable id — timestamp + hash of the line. */
  id: string
  /** ISO8601 timestamp. */
  ts: string
  /** Line text — already vendor-cleaned (no ANSI). */
  text: string
  /** Level — `info` / `warn` / `error` drive colour, nothing else. */
  level: "info" | "warn" | "error"
}

export interface MobileBuildRun {
  /** Workspace session this build belongs to. */
  sessionId: string
  /** Build id — unique per run, survives retry. */
  buildId: string
  /** Platform — `ios` / `android` / `flutter` / `react-native`. */
  platform: MobilePlatform
  /** Target device profile for emulator install (optional). */
  device?: DeviceProfileId | null
  /** Build tool used. */
  tool: MobileBuildTool
  /** Variant — `debug` / `release`. */
  variant: MobileBuildVariant
  /** High-level status. */
  status: MobileBuildStatus
  /** Coarse phase label. */
  phase: MobileBuildPhase
  /** Free-text phase detail from the tool (shown next to the phase). */
  phaseDetail?: string | null
  /** 0–100 progress estimate. `null` when tool cannot report progress. */
  progress: number | null
  /** ISO8601 queued-at. */
  queuedAt?: string
  /** ISO8601 start-at (first output). */
  startedAt?: string
  /** ISO8601 finish-at. */
  finishedAt?: string
  /** Error list (ring-buffered). */
  errors: MobileBuildError[]
  /** Warnings (separate collapser). */
  warnings: MobileBuildError[]
  /** Artifact list. */
  artifacts: MobileBuildArtifact[]
  /** Log tail (ring-buffered). */
  logTail: MobileBuildLogLine[]
  /** Final exit code.  `null` while in-flight. */
  exitCode: number | null
  /** Free-text failure reason when `status === 'failed'`. */
  failureReason?: string | null
}

// ─── Public constants ──────────────────────────────────────────────────────

/**
 * SSE event namespace prefix — kept as a constant so the disjointness
 * contract with V7 #2 `mobile_workspace.iteration_timeline.*` is
 * testable via `set.isdisjoint` in the unit tests.
 */
export const MOBILE_BUILD_EVENT_PREFIX = "mobile_workspace.build." as const

/**
 * Known event names — every event the panel consumes.  Exported so
 * tests can assert disjointness against sibling event prefixes.
 */
export const MOBILE_BUILD_EVENT_NAMES = Object.freeze([
  `${MOBILE_BUILD_EVENT_PREFIX}queued`,
  `${MOBILE_BUILD_EVENT_PREFIX}started`,
  `${MOBILE_BUILD_EVENT_PREFIX}progress`,
  `${MOBILE_BUILD_EVENT_PREFIX}log`,
  `${MOBILE_BUILD_EVENT_PREFIX}error`,
  `${MOBILE_BUILD_EVENT_PREFIX}artifact`,
  `${MOBILE_BUILD_EVENT_PREFIX}completed`,
  `${MOBILE_BUILD_EVENT_PREFIX}failed`,
  `${MOBILE_BUILD_EVENT_PREFIX}cancelled`,
] as const)

/** Ring-buffer caps — keep SSE bursts from blowing up the panel's DOM. */
export const DEFAULT_MAX_LOG_LINES = 200
export const DEFAULT_MAX_ERRORS = 50
export const DEFAULT_MAX_WARNINGS = 50

/**
 * Default artifact kind → lucide icon.  Exported for storybook — the
 * panel itself keeps the mapping private.
 */
export const ARTIFACT_KIND_LABELS: Readonly<
  Record<MobileBuildArtifact["kind"], string>
> = Object.freeze({
  ipa: "iOS App",
  apk: "Android APK",
  aab: "Android Bundle",
  dsym: "dSYM symbols",
  mapping: "Proguard map",
  other: "Artifact",
})

/**
 * Tool → human label — mirrors the panel's header badge.
 */
export const TOOL_LABELS: Readonly<Record<MobileBuildTool, string>> =
  Object.freeze({
    xcodebuild: "Xcode",
    gradle: "Gradle",
    flutter: "Flutter",
    "react-native-cli": "React Native CLI",
  })

// ─── Pure helpers (exported for tests) ─────────────────────────────────────

/**
 * Resolve the default build tool for a workspace platform.  Flutter /
 * RN map to their wrapper CLIs; iOS → Xcode, Android → Gradle.  Pure
 * so callers driving the panel from a workspace selector can cheaply
 * derive "what tool will run if I don't override it".
 */
export function defaultToolForPlatform(
  platform: MobilePlatform,
): MobileBuildTool {
  switch (platform) {
    case "ios":
      return "xcodebuild"
    case "android":
      return "gradle"
    case "flutter":
      return "flutter"
    case "react-native":
      return "react-native-cli"
  }
}

/**
 * Resolve the artifact kind(s) a platform + variant can produce.
 * iOS → `.ipa`; Android → `.apk` + `.aab`; Flutter / RN → either
 * depending on target sub-platform.  Used as a hint before the
 * backend actually publishes artifacts so the panel can show an
 * "expected output" placeholder.
 */
export function expectedArtifactKinds(
  platform: MobilePlatform,
): readonly MobileBuildArtifact["kind"][] {
  switch (platform) {
    case "ios":
      return ["ipa"]
    case "android":
      return ["apk", "aab"]
    case "flutter":
    case "react-native":
      return ["ipa", "apk"]
  }
}

/**
 * Status → human label.  Mirrors the header badge.  Pure so tests can
 * pin the exact copy without mounting the component.
 */
export function buildStatusLabel(status: MobileBuildStatus): string {
  switch (status) {
    case "idle":
      return "Idle"
    case "queued":
      return "Queued"
    case "running":
      return "Building"
    case "succeeded":
      return "Succeeded"
    case "failed":
      return "Failed"
    case "cancelled":
      return "Cancelled"
  }
}

/**
 * Status → colour CSS var.  Mirrors the traffic-light palette used
 * across the ops surfaces (emerald / amber / red / muted).
 */
export function buildStatusColorVar(status: MobileBuildStatus): string {
  switch (status) {
    case "succeeded":
      return "var(--validation-emerald)"
    case "failed":
      return "var(--critical-red)"
    case "cancelled":
      return "var(--muted-foreground)"
    case "running":
    case "queued":
      return "var(--neural-blue)"
    case "idle":
    default:
      return "var(--muted-foreground)"
  }
}

/**
 * Status → is-terminal predicate.  Terminal runs stop receiving
 * progress ticks and flip the header badge into its final colour.
 */
export function isTerminalBuildStatus(status: MobileBuildStatus): boolean {
  return (
    status === "succeeded" || status === "failed" || status === "cancelled"
  )
}

/**
 * Format a millisecond duration as `h m s`.  Negative / non-finite
 * inputs degrade to `"—"`.  Stable output so tests can pin the exact
 * string.
 */
export function formatBuildDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—"
  const totalSeconds = Math.floor(ms / 1000)
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = totalSeconds % 60
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

/**
 * Format a byte size into `KB / MB / GB` with one decimal.  Used by
 * the artifact size badge.  Zero / negative / non-finite degrade to
 * `"—"`.
 */
export function formatBuildByteSize(
  bytes: number | null | undefined,
): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—"
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${kb.toFixed(1)} KB`
  const mb = kb / 1024
  if (mb < 1024) return `${mb.toFixed(1)} MB`
  const gb = mb / 1024
  return `${gb.toFixed(2)} GB`
}

/**
 * Shorten a long absolute path for the error-list row.  The leading
 * directories collapse into an ellipsis, the filename survives
 * verbatim.  Stable so tests can pin the output.
 */
export function shortenBuildPath(
  path: string | undefined | null,
  maxChars = 48,
): string {
  if (!path || typeof path !== "string") return ""
  if (path.length <= maxChars) return path
  const tail = path.slice(-(maxChars - 1))
  return `…${tail}`
}

/**
 * Derive a percentage from a phase progress payload.  Clamps to
 * `[0, 100]`; returns `null` when the payload cannot be interpreted
 * (the panel then falls back to an indeterminate bar).
 */
export function clampBuildProgress(
  pct: number | null | undefined,
): number | null {
  if (pct == null) return null
  if (!Number.isFinite(pct)) return null
  if (pct < 0) return 0
  if (pct > 100) return 100
  return pct
}

/**
 * Classify a vendor phase string into one of our semantic buckets.
 * Case-insensitive substring match; unknown phases → `"other"`.
 * Exported so the backend-side progress translator has a single
 * source of truth with the panel.
 */
export function classifyBuildPhase(raw: string): MobileBuildPhase {
  if (!raw || typeof raw !== "string") return "other"
  const s = raw.toLowerCase()
  if (/queue/.test(s)) return "queued"
  if (/config|prepare|create.?build.?dir|bootstrap/.test(s)) return "configuring"
  if (/compil|build.?kotlin|build.?swift|dart|babel|ts.?check|metro/.test(s))
    return "compiling"
  if (/ld\b|link/.test(s)) return "linking"
  if (/package|archive|merge.?resource|bundle|assemble/.test(s))
    return "packaging"
  if (/sign|codesign|keystore|provision/.test(s)) return "signing"
  if (/export|upload|ipa|apk|aab/.test(s)) return "exporting"
  return "other"
}

/**
 * Make a ring-buffered push — dedup by id, append, cap.  Pure so the
 * reducer does not need to close over the cap in a ref.
 */
export function pushRingBuffer<T extends { id: string }>(
  existing: T[],
  next: T[],
  cap: number,
): T[] {
  if (cap <= 0) return []
  const seen = new Set<string>()
  const merged: T[] = []
  for (const item of [...existing, ...next]) {
    if (seen.has(item.id)) continue
    seen.add(item.id)
    merged.push(item)
  }
  if (merged.length <= cap) return merged
  return merged.slice(merged.length - cap)
}

/**
 * Compute the elapsed time of a run — `finishedAt - startedAt` when
 * terminal, `now - startedAt` otherwise.  `null` when the run has no
 * `startedAt` yet.
 */
export function elapsedBuildMs(
  run: Pick<MobileBuildRun, "startedAt" | "finishedAt" | "status">,
  now: number = Date.now(),
): number | null {
  if (!run.startedAt) return null
  const start = new Date(run.startedAt).getTime()
  if (!Number.isFinite(start)) return null
  const end = run.finishedAt ? new Date(run.finishedAt).getTime() : now
  if (!Number.isFinite(end)) return null
  return Math.max(0, end - start)
}

/**
 * Build a default empty run — used for the `idle` empty state and for
 * `reset` behaviour when the operator clicks "clear".
 */
export function emptyBuildRun(sessionId: string, platform: MobilePlatform): MobileBuildRun {
  return {
    sessionId,
    buildId: "",
    platform,
    device: null,
    tool: defaultToolForPlatform(platform),
    variant: "debug",
    status: "idle",
    phase: "queued",
    phaseDetail: null,
    progress: null,
    errors: [],
    warnings: [],
    artifacts: [],
    logTail: [],
    exitCode: null,
    failureReason: null,
  }
}

// ─── SSE event shapes ──────────────────────────────────────────────────────

/** Event payload the panel consumes via `subscribeEvents`. */
export interface MobileBuildEvent {
  event: string
  data: Record<string, unknown>
}

/**
 * Narrow an SSE event to "is this for the panel's session + build".
 * Pure so the reducer stays test-friendly.
 */
export function matchBuildEvent(
  event: MobileBuildEvent,
  sessionId: string,
  buildId: string | null,
): boolean {
  if (typeof event.event !== "string") return false
  if (!event.event.startsWith(MOBILE_BUILD_EVENT_PREFIX)) return false
  const d = event.data ?? {}
  const sid = typeof d.session_id === "string"
    ? d.session_id
    : typeof d.sessionId === "string"
      ? d.sessionId
      : null
  if (sid !== sessionId) return false
  if (buildId) {
    const bid = typeof d.build_id === "string"
      ? d.build_id
      : typeof d.buildId === "string"
        ? d.buildId
        : null
    // `queued` / `started` are allowed to land without a prior buildId,
    // they are the events that *create* the buildId.
    if (bid && bid !== buildId) return false
  }
  return true
}

/**
 * Apply one SSE event to the current run state.  Pure reducer (no
 * side-effects).  Unrecognised sub-events degrade to a no-op rather
 * than throwing — the panel must never crash a workspace page.
 */
export function applyBuildEvent(
  run: MobileBuildRun,
  event: MobileBuildEvent,
  caps: {
    maxLog?: number
    maxErrors?: number
    maxWarnings?: number
  } = {},
): MobileBuildRun {
  const maxLog = caps.maxLog ?? DEFAULT_MAX_LOG_LINES
  const maxErrors = caps.maxErrors ?? DEFAULT_MAX_ERRORS
  const maxWarnings = caps.maxWarnings ?? DEFAULT_MAX_WARNINGS
  const d = event.data ?? {}
  const kind = event.event.slice(MOBILE_BUILD_EVENT_PREFIX.length)

  switch (kind) {
    case "queued": {
      const buildId = typeof d.build_id === "string"
        ? d.build_id
        : typeof d.buildId === "string"
          ? d.buildId
          : run.buildId
      return {
        ...run,
        buildId: buildId || run.buildId,
        status: "queued",
        phase: "queued",
        phaseDetail: typeof d.detail === "string" ? d.detail : null,
        queuedAt: typeof d.queued_at === "string" ? d.queued_at : new Date().toISOString(),
        startedAt: undefined,
        finishedAt: undefined,
        progress: 0,
        errors: [],
        warnings: [],
        artifacts: [],
        logTail: [],
        exitCode: null,
        failureReason: null,
        tool: (typeof d.tool === "string" ? (d.tool as MobileBuildTool) : run.tool),
        variant: (typeof d.variant === "string"
          ? (d.variant as MobileBuildVariant)
          : run.variant),
      }
    }
    case "started": {
      return {
        ...run,
        status: "running",
        startedAt: typeof d.started_at === "string" ? d.started_at : new Date().toISOString(),
        phase: classifyBuildPhase(typeof d.phase === "string" ? d.phase : "configuring"),
        phaseDetail: typeof d.detail === "string" ? d.detail : null,
        progress: clampBuildProgress(typeof d.progress === "number" ? d.progress : 0),
      }
    }
    case "progress": {
      const phaseRaw = typeof d.phase === "string" ? d.phase : ""
      return {
        ...run,
        status: run.status === "queued" ? "running" : run.status,
        phase: phaseRaw ? classifyBuildPhase(phaseRaw) : run.phase,
        phaseDetail: typeof d.detail === "string" ? d.detail : run.phaseDetail,
        progress: clampBuildProgress(
          typeof d.progress === "number" ? d.progress : run.progress,
        ),
      }
    }
    case "log": {
      const line = coerceLogLine(d)
      if (!line) return run
      return {
        ...run,
        logTail: pushRingBuffer(run.logTail, [line], maxLog),
      }
    }
    case "error": {
      const err = coerceError(d)
      if (!err) return run
      if (err.severity === "warning") {
        return {
          ...run,
          warnings: pushRingBuffer(run.warnings, [err], maxWarnings),
        }
      }
      return {
        ...run,
        errors: pushRingBuffer(run.errors, [err], maxErrors),
      }
    }
    case "artifact": {
      const art = coerceArtifact(d)
      if (!art) return run
      return {
        ...run,
        artifacts: pushRingBuffer(run.artifacts, [art], 32),
      }
    }
    case "completed": {
      return {
        ...run,
        status: "succeeded",
        finishedAt: typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString(),
        exitCode: 0,
        progress: 100,
        phase: "exporting",
        phaseDetail: typeof d.detail === "string" ? d.detail : null,
      }
    }
    case "failed": {
      return {
        ...run,
        status: "failed",
        finishedAt: typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString(),
        exitCode: typeof d.exit_code === "number" ? d.exit_code : 1,
        failureReason: typeof d.reason === "string" ? d.reason : null,
      }
    }
    case "cancelled": {
      return {
        ...run,
        status: "cancelled",
        finishedAt: typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString(),
        failureReason: typeof d.reason === "string" ? d.reason : "Cancelled by operator",
      }
    }
    default:
      return run
  }
}

function coerceLogLine(d: Record<string, unknown>): MobileBuildLogLine | null {
  const text = typeof d.text === "string"
    ? d.text
    : typeof d.message === "string"
      ? d.message
      : null
  if (!text) return null
  const ts = typeof d.ts === "string"
    ? d.ts
    : typeof d.timestamp === "string"
      ? d.timestamp
      : new Date().toISOString()
  const levelRaw = typeof d.level === "string" ? d.level.toLowerCase() : "info"
  const level: MobileBuildLogLine["level"] =
    levelRaw === "error" ? "error" : levelRaw === "warn" || levelRaw === "warning" ? "warn" : "info"
  const id = typeof d.id === "string" ? d.id : `log-${ts}-${hashForId(text)}`
  return { id, ts, text, level }
}

function coerceError(d: Record<string, unknown>): MobileBuildError | null {
  const message = typeof d.message === "string"
    ? d.message
    : typeof d.text === "string"
      ? d.text
      : null
  if (!message) return null
  const severityRaw = typeof d.severity === "string" ? d.severity.toLowerCase() : "error"
  const severity: MobileBuildErrorSeverity = severityRaw === "warning" || severityRaw === "warn"
    ? "warning"
    : "error"
  const file = typeof d.file === "string" ? d.file : undefined
  const line = typeof d.line === "number" ? d.line : undefined
  const column = typeof d.column === "number" ? d.column : undefined
  const id = typeof d.id === "string"
    ? d.id
    : `err-${file ?? "?"}-${line ?? "?"}-${column ?? "?"}-${hashForId(message)}`
  return {
    id,
    severity,
    category: typeof d.category === "string" ? d.category : undefined,
    file,
    line,
    column,
    message,
    snippet: typeof d.snippet === "string" ? d.snippet : undefined,
    observedAt: typeof d.observed_at === "string"
      ? d.observed_at
      : typeof d.ts === "string"
        ? d.ts
        : new Date().toISOString(),
  }
}

function coerceArtifact(d: Record<string, unknown>): MobileBuildArtifact | null {
  const filename = typeof d.filename === "string"
    ? d.filename
    : typeof d.name === "string"
      ? d.name
      : null
  const downloadUrl = typeof d.download_url === "string"
    ? d.download_url
    : typeof d.downloadUrl === "string"
      ? d.downloadUrl
      : typeof d.url === "string"
        ? d.url
        : null
  if (!filename || !downloadUrl) return null
  const kindRaw = typeof d.kind === "string"
    ? d.kind.toLowerCase()
    : filename.toLowerCase().split(".").pop() ?? ""
  const kind: MobileBuildArtifact["kind"] =
    kindRaw === "ipa"
      ? "ipa"
      : kindRaw === "apk"
        ? "apk"
        : kindRaw === "aab"
          ? "aab"
          : kindRaw === "dsym" || kindRaw === "dsym.zip"
            ? "dsym"
            : kindRaw === "mapping" || kindRaw === "map"
              ? "mapping"
              : "other"
  const byteSize = typeof d.byte_size === "number"
    ? d.byte_size
    : typeof d.byteSize === "number"
      ? d.byteSize
      : typeof d.size === "number"
        ? d.size
        : undefined
  return {
    id: typeof d.id === "string" ? d.id : `art-${filename}`,
    filename,
    kind,
    downloadUrl,
    byteSize,
    sha256: typeof d.sha256 === "string" ? d.sha256 : undefined,
    createdAt: typeof d.created_at === "string"
      ? d.created_at
      : new Date().toISOString(),
  }
}

/** Tiny deterministic id hash — stdlib, no crypto dep. */
function hashForId(text: string): string {
  let h = 0
  for (let i = 0; i < text.length; i++) {
    h = (h * 31 + text.charCodeAt(i)) | 0
  }
  return (h >>> 0).toString(36)
}

// ─── Sub-components ────────────────────────────────────────────────────────

function PhaseIcon({ phase, status }: { phase: MobileBuildPhase; status: MobileBuildStatus }) {
  if (status === "succeeded") {
    return <CheckCircle2 className="size-4" aria-hidden="true" />
  }
  if (status === "failed") {
    return <XCircle className="size-4" aria-hidden="true" />
  }
  if (status === "cancelled") {
    return <CircleStop className="size-4" aria-hidden="true" />
  }
  switch (phase) {
    case "configuring":
      return <Cog className="size-4 animate-spin" aria-hidden="true" />
    case "compiling":
      return <Hammer className="size-4" aria-hidden="true" />
    case "linking":
      return <Package className="size-4" aria-hidden="true" />
    case "packaging":
      return <Package className="size-4" aria-hidden="true" />
    case "signing":
      return <ShieldCheck className="size-4" aria-hidden="true" />
    case "exporting":
      return <PackageCheck className="size-4" aria-hidden="true" />
    case "queued":
      return <Clock className="size-4" aria-hidden="true" />
    default:
      return <Loader2 className="size-4 animate-spin" aria-hidden="true" />
  }
}

function ArtifactIcon({ kind }: { kind: MobileBuildArtifact["kind"] }) {
  switch (kind) {
    case "ipa":
    case "apk":
    case "aab":
      return <Package className="size-4" aria-hidden="true" />
    case "dsym":
      return <HardDriveDownload className="size-4" aria-hidden="true" />
    case "mapping":
      return <FileDown className="size-4" aria-hidden="true" />
    default:
      return <Download className="size-4" aria-hidden="true" />
  }
}

interface BuildErrorRowProps {
  error: MobileBuildError
}

function BuildErrorRow({ error }: BuildErrorRowProps) {
  const location =
    error.file && error.line
      ? `${shortenBuildPath(error.file)}:${error.line}${error.column ? `:${error.column}` : ""}`
      : error.file
        ? shortenBuildPath(error.file)
        : null
  return (
    <li
      data-testid={`mobile-build-error-${error.id}`}
      data-severity={error.severity}
      className={cn(
        "flex flex-col gap-1 rounded-md border px-2 py-1.5 text-xs",
        error.severity === "error"
          ? "border-rose-500/40 bg-rose-500/5"
          : "border-amber-500/40 bg-amber-500/5",
      )}
    >
      <div className="flex items-start gap-1.5">
        {error.severity === "error" ? (
          <XCircle
            className="mt-0.5 size-3.5 shrink-0 text-rose-400"
            aria-label="error"
          />
        ) : (
          <AlertTriangle
            className="mt-0.5 size-3.5 shrink-0 text-amber-400"
            aria-label="warning"
          />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex flex-wrap items-center gap-1.5">
            {error.category && (
              <Badge
                variant="outline"
                className="h-4 px-1 text-[10px] font-mono"
              >
                {error.category}
              </Badge>
            )}
            {location && (
              <code
                data-testid={`mobile-build-error-${error.id}-location`}
                className="font-mono text-[10px] text-muted-foreground"
              >
                {location}
              </code>
            )}
          </div>
          <p
            data-testid={`mobile-build-error-${error.id}-message`}
            className="whitespace-pre-wrap break-words text-foreground"
          >
            {error.message}
          </p>
          {error.snippet && (
            <pre
              data-testid={`mobile-build-error-${error.id}-snippet`}
              className="mt-1 whitespace-pre overflow-x-auto rounded-sm bg-background/60 p-1 font-mono text-[10px] leading-snug text-muted-foreground"
            >
              {error.snippet}
            </pre>
          )}
        </div>
      </div>
    </li>
  )
}

interface BuildArtifactRowProps {
  artifact: MobileBuildArtifact
  onDownload?: (artifact: MobileBuildArtifact) => void
}

function BuildArtifactRow({ artifact, onDownload }: BuildArtifactRowProps) {
  return (
    <li
      data-testid={`mobile-build-artifact-${artifact.id}`}
      data-kind={artifact.kind}
      className="flex items-center justify-between gap-2 rounded-md border border-border/60 bg-background/40 px-2 py-1.5"
    >
      <div className="flex min-w-0 items-center gap-2">
        <ArtifactIcon kind={artifact.kind} />
        <div className="flex min-w-0 flex-col">
          <span
            data-testid={`mobile-build-artifact-${artifact.id}-name`}
            className="truncate text-xs font-medium text-foreground"
          >
            {artifact.filename}
          </span>
          <span className="flex items-center gap-2 text-[10px] font-mono text-muted-foreground">
            <Badge variant="secondary" className="h-4 px-1 text-[10px]">
              {ARTIFACT_KIND_LABELS[artifact.kind]}
            </Badge>
            <span data-testid={`mobile-build-artifact-${artifact.id}-size`}>
              {formatBuildByteSize(artifact.byteSize)}
            </span>
            {artifact.sha256 && (
              <span
                data-testid={`mobile-build-artifact-${artifact.id}-sha`}
                title={`sha256: ${artifact.sha256}`}
                className="truncate"
              >
                sha256:{artifact.sha256.slice(0, 10)}…
              </span>
            )}
          </span>
        </div>
      </div>
      {onDownload ? (
        <Button
          type="button"
          size="sm"
          variant="secondary"
          data-testid={`mobile-build-artifact-${artifact.id}-download`}
          onClick={() => onDownload(artifact)}
          className="h-7 gap-1 px-2 text-xs"
        >
          <Download className="size-3" aria-hidden="true" />
          Download
        </Button>
      ) : (
        <a
          data-testid={`mobile-build-artifact-${artifact.id}-download`}
          href={artifact.downloadUrl}
          download={artifact.filename}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex h-7 items-center gap-1 rounded-md bg-secondary px-2 text-xs text-secondary-foreground hover:bg-secondary/80"
        >
          <Download className="size-3" aria-hidden="true" />
          Download
        </a>
      )}
    </li>
  )
}

// ─── Main panel ───────────────────────────────────────────────────────────

export interface MobileBuildStatusPanelProps {
  /**
   * Workspace session this panel is bound to.  Events targeted at a
   * different session are dropped — matches the V0 #6 workspace-scoped
   * SSE routing contract.
   */
  sessionId: string
  /**
   * Workspace platform.  Used to pick a default tool + expected
   * artifact kinds when the backend has not yet emitted a `queued`
   * event.
   */
  platform: MobilePlatform
  /**
   * Controlled initial run — lets storybook / tests seed a specific
   * run state without mocking the SSE transport.
   */
  initialRun?: MobileBuildRun | null
  /**
   * Controlled run — when set, the panel becomes a pure render surface
   * and stops applying SSE events.  Useful when the host page owns the
   * reducer (e.g. to persist between tab switches).
   */
  run?: MobileBuildRun | null
  /** Fired when the operator clicks "Start". */
  onStart?: () => void
  /** Fired when the operator clicks "Cancel" on a running build. */
  onCancel?: (buildId: string) => void
  /** Fired when the operator clicks "Retry" on a terminal failed build. */
  onRetry?: (buildId: string) => void
  /**
   * Fired when the operator clicks the download button on an artifact
   * row.  When omitted, the row renders a raw `<a download>` link — the
   * browser handles the fetch itself.
   */
  onDownloadArtifact?: (artifact: MobileBuildArtifact) => void
  /**
   * Test seam — inject a custom event source.  The helper gets the
   * same interface as the reducer, not the raw `EventSource`, so
   * tests don't need to simulate the SSE wire format.
   */
  eventTransport?: (
    onEvent: (event: MobileBuildEvent) => void,
  ) => { close: () => void }
  /** Ring-buffer cap overrides (tests). */
  maxLogLines?: number
  maxErrors?: number
  maxWarnings?: number
  /** Test seam — pin "now" for the elapsed display. */
  nowImpl?: () => number
  /** `data-testid` root (defaults to `mobile-build-status-panel`). */
  testId?: string
}

/**
 * `MobileBuildStatusPanel` — the full panel.  See module-level docstring
 * for the contract.
 */
export function MobileBuildStatusPanel(props: MobileBuildStatusPanelProps) {
  const {
    sessionId,
    platform,
    initialRun = null,
    run: controlledRun,
    onStart,
    onCancel,
    onRetry,
    onDownloadArtifact,
    eventTransport,
    maxLogLines = DEFAULT_MAX_LOG_LINES,
    maxErrors = DEFAULT_MAX_ERRORS,
    maxWarnings = DEFAULT_MAX_WARNINGS,
    nowImpl,
    testId = "mobile-build-status-panel",
  } = props

  const [internalRun, setInternalRun] = React.useState<MobileBuildRun>(
    () => initialRun ?? emptyBuildRun(sessionId, platform),
  )
  const run = controlledRun ?? internalRun

  // ── SSE wire-up ────────────────────────────────────────────────────────
  //
  // The panel attaches to the shared `EventSource` via `subscribeEvents`
  // when a transport is not injected; tests pass `eventTransport`
  // directly so they never touch the real network layer.  The
  // subscription is tied to `sessionId` only — the active `buildId`
  // rides on a ref so the subscription is not torn down + rebuilt on
  // every progress tick (which would orphan in-flight listeners under
  // React 19's strict-mode double-render).
  //
  // The controlled-run escape hatch (`controlledRun != null`) bypasses
  // the internal reducer entirely; the host page is expected to drive
  // the state externally (e.g. route-level store).
  const buildIdRef = React.useRef<string>(run.buildId)
  React.useEffect(() => {
    buildIdRef.current = run.buildId
  }, [run.buildId])

  React.useEffect(() => {
    if (controlledRun != null) return
    const handler = (event: MobileBuildEvent) => {
      if (!matchBuildEvent(event, sessionId, buildIdRef.current || null)) return
      setInternalRun((prev) =>
        applyBuildEvent(prev, event, { maxLog: maxLogLines, maxErrors, maxWarnings }),
      )
    }
    if (eventTransport) {
      const h = eventTransport(handler)
      return () => h.close()
    }
    const h = subscribeEvents((ev) =>
      handler({ event: ev.event, data: (ev.data ?? {}) as Record<string, unknown> }),
    )
    return () => h?.close?.()
  }, [
    controlledRun,
    sessionId,
    eventTransport,
    maxLogLines,
    maxErrors,
    maxWarnings,
  ])

  // Re-seed the internal reducer when the bound session changes.
  const lastSessionRef = React.useRef(sessionId)
  React.useEffect(() => {
    if (lastSessionRef.current !== sessionId) {
      lastSessionRef.current = sessionId
      setInternalRun(emptyBuildRun(sessionId, platform))
    }
  }, [sessionId, platform])

  // ── Elapsed ticker — increments 1× per second while running ──────────
  const [tick, setTick] = React.useState(0)
  React.useEffect(() => {
    if (isTerminalBuildStatus(run.status) || run.status === "idle") return
    const id = window.setInterval(() => setTick((t) => t + 1), 1000)
    return () => window.clearInterval(id)
  }, [run.status])

  const now = nowImpl ? nowImpl() : Date.now() + tick * 0
  const elapsedMs = elapsedBuildMs(run, now)
  const elapsedLabel = formatBuildDuration(elapsedMs)

  const progress = run.progress ?? 0
  const progressIndeterminate = run.progress == null

  const deviceLabel = run.device ? DEVICE_PROFILES[run.device].label : null
  const platformLabel = run.platform.replace(/-/g, " ")

  const canStart = onStart != null && (run.status === "idle" || isTerminalBuildStatus(run.status))
  const canCancel = onCancel != null && !isTerminalBuildStatus(run.status) && run.status !== "idle"
  const canRetry = onRetry != null && (run.status === "failed" || run.status === "cancelled")

  const expectedKinds = React.useMemo(
    () => expectedArtifactKinds(platform),
    [platform],
  )

  return (
    <section
      data-testid={testId}
      data-status={run.status}
      data-phase={run.phase}
      data-platform={run.platform}
      data-tool={run.tool}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-2"
    >
      {/* ─── Header ─────────────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-col gap-1.5"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            data-testid={`${testId}-status-badge`}
            variant="outline"
            className="h-5 gap-1 px-1.5 text-[11px]"
            style={{ color: buildStatusColorVar(run.status) }}
          >
            <PhaseIcon phase={run.phase} status={run.status} />
            {buildStatusLabel(run.status)}
          </Badge>
          <Badge
            data-testid={`${testId}-tool-badge`}
            variant="secondary"
            className="h-5 px-1.5 text-[11px]"
          >
            {TOOL_LABELS[run.tool]}
          </Badge>
          <Badge
            data-testid={`${testId}-platform-badge`}
            variant="outline"
            className="h-5 px-1.5 text-[11px] capitalize"
          >
            {platformLabel}
          </Badge>
          <Badge
            data-testid={`${testId}-variant-badge`}
            variant="outline"
            className="h-5 px-1.5 text-[11px] capitalize"
          >
            {run.variant}
          </Badge>
          {deviceLabel && (
            <Badge
              data-testid={`${testId}-device-badge`}
              variant="outline"
              className="h-5 px-1.5 text-[11px]"
            >
              {deviceLabel}
            </Badge>
          )}
          <span
            data-testid={`${testId}-elapsed`}
            className="ml-auto flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
          >
            <Clock className="size-3" aria-hidden="true" />
            {elapsedLabel}
          </span>
        </div>
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex min-w-0 items-center gap-2">
              <span
                data-testid={`${testId}-phase-label`}
                className="truncate text-xs font-medium text-foreground capitalize"
              >
                {run.phase}
              </span>
              {run.phaseDetail && (
                <span
                  data-testid={`${testId}-phase-detail`}
                  className="truncate text-[11px] text-muted-foreground"
                >
                  {run.phaseDetail}
                </span>
              )}
              <span
                data-testid={`${testId}-progress-label`}
                className="ml-auto font-mono text-[10px] text-muted-foreground"
              >
                {progressIndeterminate ? "—" : `${progress.toFixed(0)}%`}
              </span>
            </div>
            <Progress
              data-testid={`${testId}-progress-bar`}
              data-indeterminate={progressIndeterminate ? "true" : "false"}
              value={progressIndeterminate ? 0 : progress}
              className={cn(
                "h-1.5",
                progressIndeterminate && "opacity-60",
              )}
            />
          </div>
        </div>
        <div
          data-testid={`${testId}-actions`}
          className="flex items-center gap-1.5"
        >
          {canStart && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              data-testid={`${testId}-start`}
              onClick={onStart}
              className="h-7 gap-1 px-2 text-xs"
            >
              <Play className="size-3" aria-hidden="true" />
              Start build
            </Button>
          )}
          {canCancel && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid={`${testId}-cancel`}
              onClick={() => onCancel?.(run.buildId)}
              className="h-7 gap-1 px-2 text-xs"
            >
              <CircleStop className="size-3" aria-hidden="true" />
              Cancel
            </Button>
          )}
          {canRetry && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              data-testid={`${testId}-retry`}
              onClick={() => onRetry?.(run.buildId)}
              className="h-7 gap-1 px-2 text-xs"
            >
              <RefreshCw className="size-3" aria-hidden="true" />
              Retry
            </Button>
          )}
          {run.status === "failed" && run.failureReason && (
            <span
              data-testid={`${testId}-failure-reason`}
              className="ml-2 truncate text-[11px] text-rose-400"
            >
              {run.failureReason}
            </span>
          )}
        </div>
      </header>

      <Separator />

      {/* ─── Error list ────────────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-errors`}
        data-error-count={run.errors.length}
        data-warning-count={run.warnings.length}
        className="flex flex-col gap-1"
      >
        <header className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <FileWarning className="size-3.5" aria-hidden="true" />
            Errors
            <Badge
              data-testid={`${testId}-errors-count`}
              variant="outline"
              className="ml-1 h-4 px-1 text-[10px]"
            >
              {run.errors.length}
            </Badge>
            {run.warnings.length > 0 && (
              <Badge
                data-testid={`${testId}-warnings-count`}
                variant="outline"
                className="ml-1 h-4 px-1 text-[10px]"
              >
                {run.warnings.length} warn
              </Badge>
            )}
          </span>
        </header>
        {run.errors.length === 0 && run.warnings.length === 0 ? (
          <p
            data-testid={`${testId}-errors-empty`}
            className="rounded-md border border-dashed border-border/60 px-2 py-2 text-center text-[11px] text-muted-foreground"
          >
            {run.status === "succeeded"
              ? "No errors — build clean."
              : "No errors yet."}
          </p>
        ) : (
          <ul className="flex flex-col gap-1" data-testid={`${testId}-errors-list`}>
            {run.errors.map((e) => (
              <BuildErrorRow key={e.id} error={e} />
            ))}
            {run.warnings.map((w) => (
              <BuildErrorRow key={w.id} error={w} />
            ))}
          </ul>
        )}
      </section>

      <Separator />

      {/* ─── Artifacts ─────────────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-artifacts`}
        data-artifact-count={run.artifacts.length}
        className="flex flex-col gap-1"
      >
        <header className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <Package className="size-3.5" aria-hidden="true" />
            Artifacts
            <Badge
              data-testid={`${testId}-artifacts-count`}
              variant="outline"
              className="ml-1 h-4 px-1 text-[10px]"
            >
              {run.artifacts.length}
            </Badge>
          </span>
        </header>
        {run.artifacts.length === 0 ? (
          <p
            data-testid={`${testId}-artifacts-empty`}
            className="rounded-md border border-dashed border-border/60 px-2 py-2 text-center text-[11px] text-muted-foreground"
          >
            No artifacts yet — expected{" "}
            <span className="font-mono text-foreground">
              {expectedKinds.map((k) => `.${k}`).join(" / ")}
            </span>
            {" "}on success.
          </p>
        ) : (
          <ul className="flex flex-col gap-1" data-testid={`${testId}-artifacts-list`}>
            {run.artifacts.map((a) => (
              <BuildArtifactRow key={a.id} artifact={a} onDownload={onDownloadArtifact} />
            ))}
          </ul>
        )}
      </section>

      {/* ─── Log tail (collapsible) ──────────────────────────────────────── */}
      {run.logTail.length > 0 && (
        <details
          data-testid={`${testId}-log-tail`}
          className="rounded-md border border-border/40 bg-background/30"
        >
          <summary className="flex cursor-pointer items-center gap-1 px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            <ChevronDown className="size-3.5 shrink-0" aria-hidden="true" />
            Log tail
            <Badge
              variant="outline"
              className="ml-1 h-4 px-1 text-[10px]"
              data-testid={`${testId}-log-tail-count`}
            >
              {run.logTail.length}
            </Badge>
          </summary>
          <pre
            data-testid={`${testId}-log-tail-body`}
            className="m-0 max-h-48 overflow-auto p-2 font-mono text-[10px] leading-snug"
          >
            {run.logTail.map((line) => (
              <span
                key={line.id}
                data-testid={`${testId}-log-tail-line-${line.id}`}
                data-level={line.level}
                className={cn(
                  "block whitespace-pre",
                  line.level === "error"
                    ? "text-rose-400"
                    : line.level === "warn"
                      ? "text-amber-400"
                      : "text-muted-foreground",
                )}
              >
                {line.text}
              </span>
            ))}
          </pre>
        </details>
      )}

      {/* ─── Diagnostics footer ─────────────────────────────────────────── */}
      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span className="flex items-center gap-1">
          <FlaskConical className="size-3" aria-hidden="true" />
          Session
          <code className="ml-1 font-mono text-[10px]">
            {shortenBuildPath(run.sessionId, 24)}
          </code>
        </span>
        {run.buildId && (
          <span
            data-testid={`${testId}-build-id`}
            className="flex items-center gap-1"
          >
            build
            <code className="ml-1 font-mono text-[10px]">
              {shortenBuildPath(run.buildId, 24)}
            </code>
          </span>
        )}
      </footer>
    </section>
  )
}

export default MobileBuildStatusPanel
