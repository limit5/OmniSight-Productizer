/**
 * V8 #2 (TODO row 2703 / issue #324) — Software multi-platform release
 * dashboard.
 *
 * The Software sibling of `mobile-build-status-panel.tsx` (V7 #4) and
 * `store-submission-dashboard.tsx` (V7 #5).  Where the mobile build
 * panel is "one platform per panel, scrubbing through phases", this
 * dashboard is "ten platforms in a single grid, each row showing the
 * `[ status / artifact / download / last-build-at ]` snapshot the
 * operator needs in order to know whether a release is shippable".
 *
 * Three operator-visible surfaces:
 *
 *   1. Header strip — release id (semver) + overall release status
 *      derived from the per-target rows (`pending` / `building` /
 *      `passed` / `failed`) + total artifact count + a short "ship-it"
 *      gate ("3/10 targets passed; 2 failed; 5 pending — not shippable
 *      yet") so the operator can decide at a glance whether the
 *      release is ready.  Total bytes + latest build timestamp (relative
 *      "12m ago") trail on the right.
 *   2. Status grid — one row per `BuildTarget` (10 entries: Docker /
 *      Helm / .deb / .rpm / .msi / .dmg / wheel / npm / jar / native
 *      binary).  Each row carries:
 *        - target badge (icon + label + description)
 *        - status badge (colour-coded `pending` ⏳ / `building` 🔨 /
 *          `passed` ✅ / `failed` ❌ / `skipped` ⏭ / `cancelled` 🚫)
 *        - build duration / queued-at relative time
 *        - artifact filename + size + sha256 + Download link/button
 *        - failed → reason inline + Retry button when host wires it up
 *      Targets that the active framework can't produce (e.g. `.jar`
 *      under Python) render with a `not-applicable` muted slot but
 *      stay in the grid so position stays stable across language
 *      switches.
 *   3. Diagnostics footer — session id + release id + framework hint so
 *      a screenshot of the dashboard is enough for ops triage.
 *
 * Live wire-up:
 *   The dashboard subscribes to the shared SSE stream (lib/api
 *   `subscribeEvents`) and filters for the
 *   `software_workspace.release.*` namespace.  Eight event names,
 *   disjoint from V7 #2 / V7 #4 / V7 #5 / V8 #1 sibling buckets:
 *
 *     - `software_workspace.release.queued`             — release dispatched
 *     - `software_workspace.release.target_queued`      — single target queued
 *     - `software_workspace.release.target_started`     — single target build started
 *     - `software_workspace.release.target_progress`    — % progress tick
 *     - `software_workspace.release.target_succeeded`   — target produced artifact(s)
 *     - `software_workspace.release.target_failed`      — target hit error
 *     - `software_workspace.release.target_cancelled`   — operator-initiated abort
 *     - `software_workspace.release.artifact_uploaded`  — artifact landed in object store
 *
 *   All events MUST carry `session_id` + `release_id` + `target` (the
 *   `release.queued` event is the only exception — it does not require
 *   a target since it bootstraps the release shell).  Dashboards are
 *   bound to a single `sessionId` so cross-session events get dropped
 *   silently.
 *
 * Module-global state audit (SOP Step 1):
 *   N/A — zero module-level mutable state.  All state lives in React
 *   `useState` scoped per component instance; SSE subscription uses
 *   the shared `EventSource` owned by `lib/api`.  The frozen
 *   constants `RELEASE_EVENT_NAMES` / `BUILD_TARGET_ICON_LABELS` /
 *   `RELEASE_TARGET_STATUS_LABELS` are imported by every worker from
 *   the same source module — SOP Step 1 qualifying answer #1.
 *
 * Read-after-write audit:
 *   N/A — no async / DB / pool / lock interaction.  Reducer is pure;
 *   useEffect cascades over `subscribeEvents` use the standard React
 *   batched setState semantics.
 *
 * Intentional non-goals:
 *   - The dashboard does NOT itself trigger a release; `onTriggerRelease`
 *     / `onRetryTarget` / `onCancelTarget` / `onDownloadArtifact` are
 *     opt-in callbacks the host page wires up to backend REST endpoints.
 *   - The dashboard does NOT recompute artifact sha256; it surfaces
 *     what the backend reports.
 *   - Per-target build log tail is intentionally NOT surfaced here —
 *     that lives in the central terminal output viewer (V8 #1).  The
 *     dashboard is the at-a-glance grid, not the deep-dive log reader.
 */
"use client"

import * as React from "react"
import {
  AlertTriangle,
  Boxes,
  CheckCircle2,
  CircleStop,
  Clock,
  Coffee,
  Cpu,
  Disc3,
  Download,
  FileBox,
  HardDriveDownload,
  Hourglass,
  Loader2,
  Package,
  PackageCheck,
  RefreshCw,
  Rocket,
  Server,
  ShieldCheck,
  SkipForward,
  XCircle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Separator } from "@/components/ui/separator"

import { subscribeEvents } from "@/lib/api"
import {
  BUILD_TARGET_OPTIONS,
  type BuildTarget,
  type BuildTargetOption,
  type FrameworkOption,
  targetsForFramework,
} from "@/app/workspace/software/page"

// ─── Public shapes ─────────────────────────────────────────────────────────

/**
 * Per-target build status.  Mirrors the lifecycle the SSE producer
 * reports.  `not_applicable` is a render-only state used when the
 * active framework cannot emit the target (e.g. `.jar` under Python);
 * the dashboard keeps the row in place so the grid layout stays stable
 * across language switches.
 */
export type ReleaseTargetStatus =
  | "pending"
  | "queued"
  | "building"
  | "passed"
  | "failed"
  | "skipped"
  | "cancelled"
  | "not_applicable"

/** Overall release rollup status — derived from the per-target rows. */
export type ReleaseRollupStatus =
  | "idle"
  | "in_progress"
  | "passed"
  | "partial"
  | "failed"

export interface ReleaseArtifact {
  /** Stable id — usually `${target}:${filename}` or a hash. */
  id: string
  /** Target this artifact belongs to. */
  target: BuildTarget
  /** Human filename (`omnisight-0.1.0.tar.gz`, `omnisight_0.1.0_amd64.deb`). */
  filename: string
  /** Authenticated or signed download URL. */
  downloadUrl: string
  /** Size in bytes; `null` when not known yet. */
  byteSize?: number | null
  /** Hex sha256 for integrity copy-paste. */
  sha256?: string | null
  /** ISO-8601 timestamp the artifact finished uploading. */
  createdAt?: string | null
  /** Free-text content type / OCI ref / etc. */
  contentType?: string | null
}

export interface ReleaseTargetState {
  /** Stable target id (the same `BuildTarget` enum the page uses). */
  target: BuildTarget
  /** Per-target status. */
  status: ReleaseTargetStatus
  /** 0–100 build progress; `null` when toolchain doesn't report it. */
  progress: number | null
  /** Free-text status detail (`"compile-amd64"`, `"helm-package"`, …). */
  detail?: string | null
  /** ISO-8601 queued-at. */
  queuedAt?: string | null
  /** ISO-8601 start-at. */
  startedAt?: string | null
  /** ISO-8601 finish-at. */
  finishedAt?: string | null
  /** Latest artifact for the target — `null` until first upload. */
  artifact: ReleaseArtifact | null
  /** Free-text failure reason when `status === 'failed'`. */
  failureReason?: string | null
  /** Build duration in ms.  Computed from start/finish when present. */
  durationMs?: number | null
}

export interface ReleaseSnapshot {
  /** Workspace session this release belongs to. */
  sessionId: string
  /** Stable release id — semver, git sha, or backend uuid. */
  releaseId: string
  /** Active framework label (`"FastAPI"`) — used to filter the grid. */
  frameworkLabel?: string | null
  /** Active framework id (`"python:fastapi"`). */
  frameworkId?: string | null
  /** Map keyed by `BuildTarget` — every target the dashboard tracks. */
  targets: Record<BuildTarget, ReleaseTargetState>
  /** ISO-8601 release-queued timestamp. */
  queuedAt?: string | null
  /** ISO-8601 release-finished timestamp. */
  finishedAt?: string | null
}

// ─── Public constants ──────────────────────────────────────────────────────

/**
 * SSE event namespace prefix — kept as a constant so the disjointness
 * contract with sibling namespaces is testable via `set.isdisjoint` in
 * the unit tests.
 */
export const RELEASE_EVENT_PREFIX = "software_workspace.release." as const

/**
 * Known event names — every event the dashboard consumes.  Exported so
 * tests can assert disjointness against sibling event prefixes.
 */
export const RELEASE_EVENT_NAMES = Object.freeze([
  `${RELEASE_EVENT_PREFIX}queued`,
  `${RELEASE_EVENT_PREFIX}target_queued`,
  `${RELEASE_EVENT_PREFIX}target_started`,
  `${RELEASE_EVENT_PREFIX}target_progress`,
  `${RELEASE_EVENT_PREFIX}target_succeeded`,
  `${RELEASE_EVENT_PREFIX}target_failed`,
  `${RELEASE_EVENT_PREFIX}target_cancelled`,
  `${RELEASE_EVENT_PREFIX}artifact_uploaded`,
] as const)

/** Stable display order — matches `BUILD_TARGET_OPTIONS` so the grid
 *  position never changes across language switches. */
export const RELEASE_TARGET_ORDER: readonly BuildTarget[] = Object.freeze(
  BUILD_TARGET_OPTIONS.map((o) => o.id),
)

/** Per-status human label.  Pure so tests can pin the exact copy. */
export const RELEASE_TARGET_STATUS_LABELS: Readonly<
  Record<ReleaseTargetStatus, string>
> = Object.freeze({
  pending: "Pending",
  queued: "Queued",
  building: "Building",
  passed: "Passed",
  failed: "Failed",
  skipped: "Skipped",
  cancelled: "Cancelled",
  not_applicable: "N/A",
})

/** Per-rollup human label. */
export const RELEASE_ROLLUP_STATUS_LABELS: Readonly<
  Record<ReleaseRollupStatus, string>
> = Object.freeze({
  idle: "Idle",
  in_progress: "In progress",
  passed: "All targets passed",
  partial: "Partial",
  failed: "Failed",
})

// ─── Pure helpers (exported for tests) ─────────────────────────────────────

/**
 * Resolve the `BuildTargetOption` for a target — looks up against the
 * `BUILD_TARGET_OPTIONS` catalogue.  Returns `null` if the input is
 * not a known target (defensive, the caller controls the union but
 * the runtime can drift if backend leaks a new id).
 */
export function buildTargetOption(target: BuildTarget): BuildTargetOption | null {
  return BUILD_TARGET_OPTIONS.find((o) => o.id === target) ?? null
}

/**
 * Status → CSS colour variable.  Mirrors the traffic-light palette used
 * across the ops surfaces (emerald / amber / red / muted / neural-blue).
 */
export function releaseTargetStatusColorVar(
  status: ReleaseTargetStatus,
): string {
  switch (status) {
    case "passed":
      return "var(--validation-emerald)"
    case "failed":
      return "var(--critical-red)"
    case "cancelled":
    case "skipped":
    case "not_applicable":
      return "var(--muted-foreground)"
    case "building":
    case "queued":
      return "var(--neural-blue)"
    case "pending":
    default:
      return "var(--muted-foreground)"
  }
}

/** Status → human label.  Thin wrapper around the constant. */
export function releaseTargetStatusLabel(status: ReleaseTargetStatus): string {
  return RELEASE_TARGET_STATUS_LABELS[status]
}

/** Status → is-terminal predicate. */
export function isTerminalReleaseTargetStatus(
  status: ReleaseTargetStatus,
): boolean {
  return (
    status === "passed" ||
    status === "failed" ||
    status === "skipped" ||
    status === "cancelled" ||
    status === "not_applicable"
  )
}

/**
 * Format a millisecond duration as `h m s`.  Negative / non-finite
 * inputs degrade to `"—"`.  Stable output so tests can pin the exact
 * string.
 */
export function formatReleaseDuration(ms: number | null | undefined): string {
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
 * Format a byte size into `KB / MB / GB` with one decimal.  Negative /
 * non-finite degrade to `"—"`.
 */
export function formatReleaseByteSize(
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
 * Relative-time formatter — `"just now"`, `"5m ago"`, `"2h ago"`,
 * `"3d ago"`, `"12w ago"`.  Falls back to the raw ISO string when the
 * input is unparseable.  Pure + deterministic given `now`.
 */
export function formatReleaseRelativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso || typeof iso !== "string") return "—"
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return iso
  const delta = Math.max(0, now - t)
  if (delta < 60_000) return "just now"
  const minutes = Math.floor(delta / 60_000)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  const weeks = Math.floor(days / 7)
  return `${weeks}w ago`
}

/** Clamp an arbitrary progress value into `[0, 100]`. */
export function clampReleaseProgress(
  pct: number | null | undefined,
): number | null {
  if (pct == null) return null
  if (!Number.isFinite(pct)) return null
  if (pct < 0) return 0
  if (pct > 100) return 100
  return pct
}

/**
 * Empty release snapshot — used for the idle empty state.  Every target
 * starts in `pending` so the grid renders all 10 rows on first paint.
 */
export function emptyReleaseSnapshot(
  sessionId: string,
  releaseId: string = "",
): ReleaseSnapshot {
  const targets = {} as Record<BuildTarget, ReleaseTargetState>
  for (const opt of BUILD_TARGET_OPTIONS) {
    targets[opt.id] = {
      target: opt.id,
      status: "pending",
      progress: null,
      detail: null,
      queuedAt: null,
      startedAt: null,
      finishedAt: null,
      artifact: null,
      failureReason: null,
      durationMs: null,
    }
  }
  return {
    sessionId,
    releaseId,
    frameworkId: null,
    frameworkLabel: null,
    targets,
    queuedAt: null,
    finishedAt: null,
  }
}

/**
 * Mark targets that the active framework cannot emit as
 * `not_applicable` so the grid greys them out.  Pure so the host page
 * can drive the framework hint without re-mounting the dashboard.
 *
 * Targets already in a terminal state stay put — only `pending` rows
 * are flipped to `not_applicable` (operator might have changed
 * frameworks mid-flight; we don't lose the prior result).
 */
export function applyFrameworkFilter(
  snapshot: ReleaseSnapshot,
  framework: FrameworkOption | null,
): ReleaseSnapshot {
  if (!framework) return snapshot
  const allowed = new Set<BuildTarget>(targetsForFramework(framework).map((t) => t.id))
  const next = { ...snapshot.targets }
  let changed = false
  for (const opt of BUILD_TARGET_OPTIONS) {
    const t = next[opt.id]
    if (allowed.has(opt.id)) {
      if (t.status === "not_applicable") {
        next[opt.id] = { ...t, status: "pending" }
        changed = true
      }
      continue
    }
    if (t.status === "pending" || t.status === "not_applicable") {
      if (t.status !== "not_applicable") changed = true
      next[opt.id] = { ...t, status: "not_applicable" }
    }
  }
  if (!changed) return snapshot
  return {
    ...snapshot,
    frameworkId: framework.id,
    frameworkLabel: framework.label,
    targets: next,
  }
}

/**
 * Compute the rollup status from per-target rows.  Order of precedence:
 *
 *   - any `building` / `queued` → `in_progress`
 *   - else any `failed` → `partial` (if any `passed`) or `failed` (none)
 *   - else any `passed` → `passed` (if every relevant row passed) or
 *                          `partial` (mix of passed + skipped/N-A)
 *   - else → `idle`
 *
 * `not_applicable` / `skipped` rows are excluded from the "any failed
 * but some passed" branching so a Python release isn't reported as
 * `partial` just because `.jar` is N/A.
 */
export function computeRollupStatus(
  snapshot: Pick<ReleaseSnapshot, "targets">,
): ReleaseRollupStatus {
  const rows = Object.values(snapshot.targets)
  const relevant = rows.filter(
    (r) => r.status !== "not_applicable" && r.status !== "skipped",
  )
  if (relevant.length === 0) return "idle"
  let inFlight = 0
  let passed = 0
  let failed = 0
  let pendingCount = 0
  for (const r of relevant) {
    if (r.status === "queued" || r.status === "building") inFlight++
    else if (r.status === "passed") passed++
    else if (r.status === "failed" || r.status === "cancelled") failed++
    else pendingCount++
  }
  if (inFlight > 0) return "in_progress"
  if (failed > 0) return passed > 0 ? "partial" : "failed"
  if (passed > 0 && passed === relevant.length) return "passed"
  if (passed > 0) return "partial"
  // pendingCount > 0 only at this point.
  return pendingCount > 0 ? "idle" : "idle"
}

/**
 * Compute the rollup counts.  Used by the header strip + the unit
 * tests.
 */
export function computeRollupCounts(
  snapshot: Pick<ReleaseSnapshot, "targets">,
): {
  total: number
  pending: number
  inFlight: number
  passed: number
  failed: number
  cancelled: number
  skipped: number
  notApplicable: number
} {
  const rows = Object.values(snapshot.targets)
  let pending = 0
  let inFlight = 0
  let passed = 0
  let failed = 0
  let cancelled = 0
  let skipped = 0
  let notApplicable = 0
  for (const r of rows) {
    switch (r.status) {
      case "pending":
        pending++
        break
      case "queued":
      case "building":
        inFlight++
        break
      case "passed":
        passed++
        break
      case "failed":
        failed++
        break
      case "cancelled":
        cancelled++
        break
      case "skipped":
        skipped++
        break
      case "not_applicable":
        notApplicable++
        break
    }
  }
  return {
    total: rows.length,
    pending,
    inFlight,
    passed,
    failed,
    cancelled,
    skipped,
    notApplicable,
  }
}

/** Total artifact byte size across all targets — for the header badge. */
export function totalArtifactBytes(
  snapshot: Pick<ReleaseSnapshot, "targets">,
): number {
  let total = 0
  for (const r of Object.values(snapshot.targets)) {
    if (r.artifact?.byteSize && Number.isFinite(r.artifact.byteSize)) {
      total += r.artifact.byteSize
    }
  }
  return total
}

/**
 * Whether the release is shippable — every relevant (non-N/A,
 * non-skipped) target has `passed`.  Pure so the "Ship release" CTA
 * gate is easy to unit-test.
 */
export function isReleaseShippable(
  snapshot: Pick<ReleaseSnapshot, "targets">,
): boolean {
  return computeRollupStatus(snapshot) === "passed"
}

// ─── SSE event shapes ──────────────────────────────────────────────────────

/** Event payload the dashboard consumes via `subscribeEvents`. */
export interface ReleaseEvent {
  event: string
  data: Record<string, unknown>
}

/**
 * Narrow an SSE event to "is this for the dashboard's session +
 * release".  Pure so the reducer stays test-friendly.  The
 * `release.queued` event is allowed to land *without* a prior
 * releaseId — it is the event that bootstraps the release id.
 */
export function matchReleaseEvent(
  event: ReleaseEvent,
  sessionId: string,
  releaseId: string | null,
): boolean {
  if (typeof event.event !== "string") return false
  if (!event.event.startsWith(RELEASE_EVENT_PREFIX)) return false
  const d = event.data ?? {}
  const sid =
    typeof d.session_id === "string"
      ? d.session_id
      : typeof d.sessionId === "string"
        ? d.sessionId
        : null
  if (sid !== sessionId) return false
  if (releaseId) {
    const rid =
      typeof d.release_id === "string"
        ? d.release_id
        : typeof d.releaseId === "string"
          ? d.releaseId
          : null
    if (rid && rid !== releaseId) return false
  }
  return true
}

/**
 * Apply one SSE event to the current release snapshot.  Pure reducer
 * (no side-effects).  Unknown sub-events degrade to a no-op rather
 * than throwing — the dashboard must never crash a workspace page.
 */
export function applyReleaseEvent(
  snapshot: ReleaseSnapshot,
  event: ReleaseEvent,
): ReleaseSnapshot {
  const d = event.data ?? {}
  const kind = event.event.slice(RELEASE_EVENT_PREFIX.length)

  if (kind === "queued") {
    const releaseId =
      typeof d.release_id === "string"
        ? d.release_id
        : typeof d.releaseId === "string"
          ? d.releaseId
          : snapshot.releaseId
    return {
      ...emptyReleaseSnapshot(snapshot.sessionId, releaseId),
      frameworkId: snapshot.frameworkId,
      frameworkLabel: snapshot.frameworkLabel,
      queuedAt:
        typeof d.queued_at === "string"
          ? d.queued_at
          : new Date().toISOString(),
    }
  }

  // Every other event must reference a target.
  const targetRaw = typeof d.target === "string" ? d.target : null
  if (!targetRaw) return snapshot
  const target = targetRaw as BuildTarget
  if (!snapshot.targets[target]) return snapshot

  switch (kind) {
    case "target_queued": {
      return updateTarget(snapshot, target, (prev) => ({
        ...prev,
        status: "queued",
        progress: 0,
        detail: typeof d.detail === "string" ? d.detail : null,
        queuedAt:
          typeof d.queued_at === "string"
            ? d.queued_at
            : new Date().toISOString(),
        startedAt: null,
        finishedAt: null,
        durationMs: null,
        failureReason: null,
      }))
    }
    case "target_started": {
      return updateTarget(snapshot, target, (prev) => ({
        ...prev,
        status: "building",
        startedAt:
          typeof d.started_at === "string"
            ? d.started_at
            : new Date().toISOString(),
        detail: typeof d.detail === "string" ? d.detail : prev.detail,
        progress: clampReleaseProgress(
          typeof d.progress === "number" ? d.progress : 0,
        ),
        finishedAt: null,
        durationMs: null,
        failureReason: null,
      }))
    }
    case "target_progress": {
      return updateTarget(snapshot, target, (prev) => ({
        ...prev,
        status: prev.status === "queued" ? "building" : prev.status,
        detail: typeof d.detail === "string" ? d.detail : prev.detail,
        progress: clampReleaseProgress(
          typeof d.progress === "number" ? d.progress : prev.progress,
        ),
      }))
    }
    case "target_succeeded": {
      const finishedAt =
        typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString()
      const artifactCandidate = coerceArtifact(d, target)
      return updateTarget(snapshot, target, (prev) => {
        const startedAt = prev.startedAt
        const durationMs =
          startedAt && Number.isFinite(new Date(startedAt).getTime())
            ? Math.max(0, new Date(finishedAt).getTime() - new Date(startedAt).getTime())
            : null
        return {
          ...prev,
          status: "passed",
          progress: 100,
          finishedAt,
          durationMs,
          failureReason: null,
          artifact: artifactCandidate ?? prev.artifact,
          detail: typeof d.detail === "string" ? d.detail : prev.detail,
        }
      })
    }
    case "target_failed": {
      const finishedAt =
        typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString()
      return updateTarget(snapshot, target, (prev) => {
        const startedAt = prev.startedAt
        const durationMs =
          startedAt && Number.isFinite(new Date(startedAt).getTime())
            ? Math.max(0, new Date(finishedAt).getTime() - new Date(startedAt).getTime())
            : null
        return {
          ...prev,
          status: "failed",
          finishedAt,
          durationMs,
          failureReason: typeof d.reason === "string" ? d.reason : "Build failed",
          detail: typeof d.detail === "string" ? d.detail : prev.detail,
        }
      })
    }
    case "target_cancelled": {
      const finishedAt =
        typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString()
      return updateTarget(snapshot, target, (prev) => {
        const startedAt = prev.startedAt
        const durationMs =
          startedAt && Number.isFinite(new Date(startedAt).getTime())
            ? Math.max(0, new Date(finishedAt).getTime() - new Date(startedAt).getTime())
            : null
        return {
          ...prev,
          status: "cancelled",
          finishedAt,
          durationMs,
          failureReason:
            typeof d.reason === "string" ? d.reason : "Cancelled by operator",
        }
      })
    }
    case "artifact_uploaded": {
      const artifact = coerceArtifact(d, target)
      if (!artifact) return snapshot
      return updateTarget(snapshot, target, (prev) => ({
        ...prev,
        artifact,
      }))
    }
    default:
      return snapshot
  }
}

function updateTarget(
  snapshot: ReleaseSnapshot,
  target: BuildTarget,
  fn: (prev: ReleaseTargetState) => ReleaseTargetState,
): ReleaseSnapshot {
  const prev = snapshot.targets[target]
  const next = fn(prev)
  if (next === prev) return snapshot
  return {
    ...snapshot,
    targets: { ...snapshot.targets, [target]: next },
  }
}

function coerceArtifact(
  d: Record<string, unknown>,
  target: BuildTarget,
): ReleaseArtifact | null {
  const filename =
    typeof d.filename === "string"
      ? d.filename
      : typeof d.name === "string"
        ? d.name
        : null
  const downloadUrl =
    typeof d.download_url === "string"
      ? d.download_url
      : typeof d.downloadUrl === "string"
        ? d.downloadUrl
        : typeof d.url === "string"
          ? d.url
          : null
  if (!filename || !downloadUrl) return null
  const byteSize =
    typeof d.byte_size === "number"
      ? d.byte_size
      : typeof d.byteSize === "number"
        ? d.byteSize
        : typeof d.size === "number"
          ? d.size
          : null
  const sha256 =
    typeof d.sha256 === "string"
      ? d.sha256
      : typeof d.checksum === "string"
        ? d.checksum
        : null
  const createdAt =
    typeof d.created_at === "string"
      ? d.created_at
      : typeof d.uploaded_at === "string"
        ? d.uploaded_at
        : new Date().toISOString()
  const contentType =
    typeof d.content_type === "string"
      ? d.content_type
      : typeof d.contentType === "string"
        ? d.contentType
        : null
  const id =
    typeof d.id === "string" ? d.id : `${target}:${filename}`
  return {
    id,
    target,
    filename,
    downloadUrl,
    byteSize,
    sha256,
    createdAt,
    contentType,
  }
}

// ─── Sub-components ────────────────────────────────────────────────────────

function TargetIcon({ target }: { target: BuildTarget }) {
  switch (target) {
    case "docker":
      return <Boxes className="size-4" aria-hidden="true" />
    case "helm":
      return <ShieldCheck className="size-4" aria-hidden="true" />
    case "deb":
    case "rpm":
      return <Package className="size-4" aria-hidden="true" />
    case "msi":
      return <FileBox className="size-4" aria-hidden="true" />
    case "dmg":
      return <Disc3 className="size-4" aria-hidden="true" />
    case "wheel":
      return <Coffee className="size-4" aria-hidden="true" />
    case "npm":
      return <PackageCheck className="size-4" aria-hidden="true" />
    case "jar":
      return <Server className="size-4" aria-hidden="true" />
    case "binary":
      return <Cpu className="size-4" aria-hidden="true" />
    default:
      return <Package className="size-4" aria-hidden="true" />
  }
}

function StatusIcon({ status }: { status: ReleaseTargetStatus }) {
  switch (status) {
    case "passed":
      return <CheckCircle2 className="size-3.5" aria-hidden="true" />
    case "failed":
      return <XCircle className="size-3.5" aria-hidden="true" />
    case "cancelled":
      return <CircleStop className="size-3.5" aria-hidden="true" />
    case "skipped":
      return <SkipForward className="size-3.5" aria-hidden="true" />
    case "not_applicable":
      return <SkipForward className="size-3.5 opacity-50" aria-hidden="true" />
    case "building":
      return <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
    case "queued":
      return <Hourglass className="size-3.5" aria-hidden="true" />
    case "pending":
    default:
      return <Clock className="size-3.5" aria-hidden="true" />
  }
}

interface ReleaseTargetRowProps {
  target: BuildTarget
  state: ReleaseTargetState
  option: BuildTargetOption
  onRetry?: (target: BuildTarget) => void
  onCancel?: (target: BuildTarget) => void
  onDownload?: (artifact: ReleaseArtifact) => void
  testId: string
  now: number
}

function ReleaseTargetRow({
  target,
  state,
  option,
  onRetry,
  onCancel,
  onDownload,
  testId,
  now,
}: ReleaseTargetRowProps) {
  const label = releaseTargetStatusLabel(state.status)
  const colour = releaseTargetStatusColorVar(state.status)
  const artifact = state.artifact
  const inFlight = state.status === "building" || state.status === "queued"
  const failed = state.status === "failed"
  const isNa = state.status === "not_applicable"

  const lastTs =
    state.finishedAt ?? state.startedAt ?? state.queuedAt ?? null
  const duration =
    state.durationMs != null
      ? formatReleaseDuration(state.durationMs)
      : state.startedAt && !state.finishedAt
        ? formatReleaseDuration(now - new Date(state.startedAt).getTime())
        : "—"

  return (
    <li
      data-testid={`${testId}-row-${target}`}
      data-target={target}
      data-status={state.status}
      data-has-artifact={artifact ? "true" : "false"}
      className={cn(
        "flex flex-col gap-1 rounded-md border px-2 py-1.5 text-xs",
        failed && "border-rose-500/40 bg-rose-500/5",
        state.status === "passed" && "border-emerald-500/40 bg-emerald-500/5",
        inFlight && "border-sky-500/40 bg-sky-500/5",
        (state.status === "pending" || isNa) && "border-border/50",
        isNa && "opacity-60",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <TargetIcon target={target} />
          <div className="flex min-w-0 flex-col">
            <span
              data-testid={`${testId}-row-${target}-label`}
              className="truncate text-foreground font-medium"
            >
              {option.label}
            </span>
            <span
              data-testid={`${testId}-row-${target}-description`}
              className="truncate text-[10px] text-muted-foreground"
            >
              {option.description}
            </span>
          </div>
        </div>
        <Badge
          data-testid={`${testId}-row-${target}-status`}
          variant="outline"
          className="h-5 gap-1 px-1.5 text-[11px]"
          style={{ color: colour }}
        >
          <StatusIcon status={state.status} />
          {label}
        </Badge>
      </div>

      {state.progress != null && inFlight && (
        <div className="flex items-center gap-2">
          <Progress
            data-testid={`${testId}-row-${target}-progress`}
            value={state.progress}
            className="h-1.5 flex-1"
          />
          <span className="font-mono text-[10px] text-muted-foreground">
            {Math.round(state.progress)}%
          </span>
        </div>
      )}

      {state.detail && (
        <p
          data-testid={`${testId}-row-${target}-detail`}
          className="truncate text-[10px] text-muted-foreground"
        >
          {state.detail}
        </p>
      )}

      {failed && state.failureReason && (
        <p
          data-testid={`${testId}-row-${target}-failure`}
          className="flex items-start gap-1 rounded-sm bg-rose-500/10 px-1.5 py-1 text-[11px] text-rose-300"
        >
          <AlertTriangle
            className="mt-0.5 size-3 shrink-0"
            aria-hidden="true"
          />
          <span className="break-words">{state.failureReason}</span>
        </p>
      )}

      {artifact && (
        <div
          data-testid={`${testId}-row-${target}-artifact`}
          className="flex items-center justify-between gap-2 rounded-sm border border-border/50 bg-background/40 px-1.5 py-1"
        >
          <div className="flex min-w-0 items-center gap-1.5">
            <HardDriveDownload
              className="size-3.5 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <div className="flex min-w-0 flex-col">
              <span
                data-testid={`${testId}-row-${target}-artifact-name`}
                className="truncate font-mono text-[11px] text-foreground"
              >
                {artifact.filename}
              </span>
              <span className="flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
                <span data-testid={`${testId}-row-${target}-artifact-size`}>
                  {formatReleaseByteSize(artifact.byteSize)}
                </span>
                {artifact.sha256 && (
                  <span
                    data-testid={`${testId}-row-${target}-artifact-sha`}
                    title={`sha256: ${artifact.sha256}`}
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
              data-testid={`${testId}-row-${target}-artifact-download`}
              onClick={() => onDownload(artifact)}
              className="h-7 gap-1 px-2 text-xs"
            >
              <Download className="size-3" aria-hidden="true" />
              Download
            </Button>
          ) : (
            <a
              data-testid={`${testId}-row-${target}-artifact-download`}
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
        </div>
      )}

      <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
        <span data-testid={`${testId}-row-${target}-duration`} className="font-mono">
          {duration}
        </span>
        <span data-testid={`${testId}-row-${target}-time`} className="font-mono">
          {formatReleaseRelativeTime(lastTs, now)}
        </span>
        <div className="ml-auto flex items-center gap-1">
          {failed && onRetry && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid={`${testId}-row-${target}-retry`}
              onClick={() => onRetry(target)}
              className="h-6 gap-1 px-1.5 text-[11px]"
            >
              <RefreshCw className="size-3" aria-hidden="true" />
              Retry
            </Button>
          )}
          {inFlight && onCancel && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid={`${testId}-row-${target}-cancel`}
              onClick={() => onCancel(target)}
              className="h-6 gap-1 px-1.5 text-[11px]"
            >
              <CircleStop className="size-3" aria-hidden="true" />
              Cancel
            </Button>
          )}
        </div>
      </div>
    </li>
  )
}

// ─── Main dashboard ───────────────────────────────────────────────────────

export interface SoftwareReleaseDashboardProps {
  /**
   * Workspace session this dashboard is bound to.  Events targeted at
   * a different session are dropped — matches the V0 #6 workspace-
   * scoped SSE routing contract.
   */
  sessionId: string
  /** Initial release id (semver / git sha) — optional. */
  releaseId?: string | null
  /**
   * Active framework — used to grey out targets the framework cannot
   * produce (e.g. `.jar` under Python).  When omitted, every target is
   * rendered.
   */
  framework?: FrameworkOption | null
  /**
   * Controlled initial snapshot — lets storybook / tests seed a
   * specific state without mocking the SSE transport.
   */
  initialSnapshot?: ReleaseSnapshot | null
  /**
   * Fully-controlled snapshot — when set, the dashboard becomes a pure
   * render surface and stops applying SSE events.  Useful when the
   * host page owns the reducer.
   */
  snapshot?: ReleaseSnapshot | null
  /** Fired when the operator clicks "Trigger release". */
  onTriggerRelease?: () => void
  /** Fired when the operator clicks "Retry" on a failed target row. */
  onRetryTarget?: (target: BuildTarget) => void
  /** Fired when the operator clicks "Cancel" on an in-flight target. */
  onCancelTarget?: (target: BuildTarget) => void
  /** Fired when the operator clicks the artifact Download button. */
  onDownloadArtifact?: (artifact: ReleaseArtifact) => void
  /**
   * Test seam — inject a custom event source.  The helper gets the
   * same interface as the reducer, not the raw `EventSource`, so tests
   * don't need to simulate the SSE wire format.
   */
  eventTransport?: (
    onEvent: (event: ReleaseEvent) => void,
  ) => { close: () => void }
  /** Test seam — pin "now" for the elapsed/relative-time display. */
  nowImpl?: () => number
  /** `data-testid` root (defaults to `software-release-dashboard`). */
  testId?: string
}

/**
 * `SoftwareReleaseDashboard` — the full panel.  See module-level
 * docstring for the contract.
 */
export function SoftwareReleaseDashboard(props: SoftwareReleaseDashboardProps) {
  const {
    sessionId,
    releaseId: releaseIdProp = null,
    framework = null,
    initialSnapshot = null,
    snapshot: controlledSnapshot,
    onTriggerRelease,
    onRetryTarget,
    onCancelTarget,
    onDownloadArtifact,
    eventTransport,
    nowImpl,
    testId = "software-release-dashboard",
  } = props

  const [internalSnapshot, setInternalSnapshot] = React.useState<ReleaseSnapshot>(
    () => {
      const seed =
        initialSnapshot ?? emptyReleaseSnapshot(sessionId, releaseIdProp ?? "")
      return framework ? applyFrameworkFilter(seed, framework) : seed
    },
  )
  const snapshot = controlledSnapshot ?? internalSnapshot

  // ── SSE wire-up ────────────────────────────────────────────────────────
  //
  // The dashboard attaches to the shared `EventSource` via
  // `subscribeEvents` when a transport is not injected; tests pass
  // `eventTransport` directly so they never touch the real network
  // layer.  The active `releaseId` rides on a ref so the subscription
  // is not torn down + rebuilt on every progress tick.
  const releaseIdRef = React.useRef<string>(snapshot.releaseId)
  React.useEffect(() => {
    releaseIdRef.current = snapshot.releaseId
  }, [snapshot.releaseId])

  React.useEffect(() => {
    if (controlledSnapshot != null) return
    const handler = (event: ReleaseEvent) => {
      if (
        !matchReleaseEvent(event, sessionId, releaseIdRef.current || null)
      )
        return
      setInternalSnapshot((prev) => applyReleaseEvent(prev, event))
    }
    if (eventTransport) {
      const h = eventTransport(handler)
      return () => h?.close?.()
    }
    const h = subscribeEvents((ev) =>
      handler({
        event: ev.event,
        data: (ev.data ?? {}) as Record<string, unknown>,
      }),
    )
    return () => h?.close?.()
  }, [controlledSnapshot, sessionId, eventTransport])

  // Re-seed when the bound session changes.
  const lastSessionRef = React.useRef(sessionId)
  React.useEffect(() => {
    if (lastSessionRef.current !== sessionId) {
      lastSessionRef.current = sessionId
      const seed = emptyReleaseSnapshot(sessionId, "")
      setInternalSnapshot(framework ? applyFrameworkFilter(seed, framework) : seed)
    }
  }, [sessionId, framework])

  // Re-apply framework filter when the active framework changes.
  const lastFrameworkRef = React.useRef<string | null>(framework?.id ?? null)
  React.useEffect(() => {
    const fid = framework?.id ?? null
    if (lastFrameworkRef.current === fid) return
    lastFrameworkRef.current = fid
    if (controlledSnapshot != null) return
    setInternalSnapshot((prev) => applyFrameworkFilter(prev, framework ?? null))
  }, [framework, controlledSnapshot])

  // ── Elapsed / relative-time ticker — increments 1× per minute ────────
  const [tick, setTick] = React.useState(0)
  React.useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 60_000)
    return () => window.clearInterval(id)
  }, [])
  // Reference the tick so it's not optimised away.
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  tick

  const now = nowImpl ? nowImpl() : Date.now()

  const rollupStatus = computeRollupStatus(snapshot)
  const counts = computeRollupCounts(snapshot)
  const totalBytes = totalArtifactBytes(snapshot)
  const lastFinishedAt = React.useMemo(() => {
    let latest: number | null = null
    for (const r of Object.values(snapshot.targets)) {
      if (!r.finishedAt) continue
      const t = new Date(r.finishedAt).getTime()
      if (!Number.isFinite(t)) continue
      if (latest == null || t > latest) latest = t
    }
    return latest
  }, [snapshot.targets])

  const rollupColour =
    rollupStatus === "passed"
      ? "var(--validation-emerald)"
      : rollupStatus === "partial" || rollupStatus === "failed"
        ? "var(--critical-red)"
        : rollupStatus === "in_progress"
          ? "var(--neural-blue)"
          : "var(--muted-foreground)"

  const canTrigger =
    onTriggerRelease != null &&
    rollupStatus !== "in_progress"

  const orderedTargets = RELEASE_TARGET_ORDER

  return (
    <section
      data-testid={testId}
      data-rollup={rollupStatus}
      data-release-id={snapshot.releaseId || ""}
      data-framework={snapshot.frameworkId ?? ""}
      data-passed={counts.passed}
      data-failed={counts.failed}
      data-in-flight={counts.inFlight}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-2"
    >
      {/* ─── Header ─────────────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-col gap-1.5"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            data-testid={`${testId}-rollup-badge`}
            variant="outline"
            className="h-5 gap-1 px-1.5 text-[11px]"
            style={{ color: rollupColour }}
          >
            <Rocket className="size-3.5" aria-hidden="true" />
            {RELEASE_ROLLUP_STATUS_LABELS[rollupStatus]}
          </Badge>
          {snapshot.releaseId && (
            <Badge
              data-testid={`${testId}-release-id`}
              variant="secondary"
              className="h-5 px-1.5 text-[11px] font-mono"
            >
              {snapshot.releaseId}
            </Badge>
          )}
          {snapshot.frameworkLabel && (
            <Badge
              data-testid={`${testId}-framework-badge`}
              variant="outline"
              className="h-5 px-1.5 text-[11px]"
            >
              {snapshot.frameworkLabel}
            </Badge>
          )}
          <span
            data-testid={`${testId}-counts`}
            className="ml-auto flex items-center gap-2 font-mono text-[10px] text-muted-foreground"
          >
            <span data-testid={`${testId}-counts-passed`} className="text-emerald-400">
              ✓ {counts.passed}
            </span>
            <span data-testid={`${testId}-counts-failed`} className="text-rose-400">
              ✗ {counts.failed}
            </span>
            <span
              data-testid={`${testId}-counts-in-flight`}
              className="text-sky-400"
            >
              ◐ {counts.inFlight}
            </span>
            <span data-testid={`${testId}-counts-pending`}>
              · {counts.pending} pending
            </span>
            <span data-testid={`${testId}-counts-na`}>
              · {counts.notApplicable} N/A
            </span>
          </span>
        </div>
        <div
          data-testid={`${testId}-summary`}
          className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground"
        >
          <span data-testid={`${testId}-total-bytes`} className="font-mono">
            {formatReleaseByteSize(totalBytes || null)} total
          </span>
          <span data-testid={`${testId}-last-build`} className="font-mono">
            last build {lastFinishedAt ? formatReleaseRelativeTime(new Date(lastFinishedAt).toISOString(), now) : "—"}
          </span>
          {onTriggerRelease && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              data-testid={`${testId}-trigger`}
              onClick={onTriggerRelease}
              disabled={!canTrigger}
              className="ml-auto h-7 gap-1 px-2 text-xs"
            >
              <Rocket className="size-3" aria-hidden="true" />
              {rollupStatus === "in_progress" ? "Building…" : "Trigger release"}
            </Button>
          )}
        </div>
      </header>

      <Separator />

      {/* ─── Status grid ───────────────────────────────────────────────── */}
      <ul
        data-testid={`${testId}-grid`}
        data-rollup={rollupStatus}
        className="grid grid-cols-1 gap-1.5 sm:grid-cols-2"
      >
        {orderedTargets.map((target) => {
          const opt = buildTargetOption(target)
          if (!opt) return null
          const state = snapshot.targets[target]
          if (!state) return null
          return (
            <ReleaseTargetRow
              key={target}
              target={target}
              state={state}
              option={opt}
              onRetry={onRetryTarget}
              onCancel={onCancelTarget}
              onDownload={onDownloadArtifact}
              testId={testId}
              now={now}
            />
          )
        })}
      </ul>

      {/* ─── Diagnostics footer ──────────────────────────────────────────── */}
      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span>
          Session
          <code className="ml-1 font-mono text-[10px]">
            {snapshot.sessionId}
          </code>
        </span>
        {snapshot.releaseId && (
          <span data-testid={`${testId}-footer-release`}>
            release
            <code className="ml-1 font-mono text-[10px]">
              {snapshot.releaseId}
            </code>
          </span>
        )}
      </footer>
    </section>
  )
}

export default SoftwareReleaseDashboard
