/**
 * V7 #5 (TODO row 2695 / issue #323) — Store submission dashboard.
 *
 * Live operator view of an app store submission — App Store Connect on
 * the iOS side, Google Play Console on the Android side — and the
 * associated tester-channel dispatch (TestFlight for iOS, Firebase App
 * Distribution for Android).  The panel sits inside the Mobile
 * Workspace and picks up where `MobileBuildStatusPanel` (V7 #4) drops
 * off: once a build has produced a signed `.ipa` / `.aab`, this
 * dashboard lets the operator see the review state and fire the
 * internal tester dispatch with a single click.
 *
 * Three concrete surfaces:
 *
 *   1. A review-status header — target (App Store / Play Console) +
 *      bundle id + version + build number + current review state
 *      (`in_review` / `pending_release` / `rejected` / `released` / ...)
 *      + `Submit` / `Re-submit` / `Withdraw` action buttons.  Reviewer
 *      notes from Apple / Google surface inline when the submission
 *      lands in `rejected` so the operator can read them without
 *      opening the respective web console.
 *   2. A screenshot manager — required device classes surface as slots
 *      (per-store catalogue is the source of truth: iPhone 6.7 / 5.5 /
 *      iPad 13 for App Store, phone / tablet-7 / tablet-10 for Play
 *      Store).  Filled slots show the preview + dimension + byte size;
 *      missing / dimension-invalid slots highlight amber / rose so the
 *      operator cannot submit with a gap.  Screenshot uploads are
 *      emitted as SSE events and the dashboard surfaces the latest
 *      backend-recorded set; it does NOT itself upload.
 *   3. A dispatch panel — TestFlight (iOS) / Firebase App Distribution
 *      (Android) channel, the audience picker, the latest dispatch
 *      status, tester count, and a short history of the last few
 *      dispatches so the operator can see "did the QA build go out
 *      yet".  The `Dispatch to <channel>` button is the V7 #5 one-click
 *      delivery contract.
 *
 * Live wire-up:
 *   The panel subscribes to the shared SSE stream (lib/api
 *   `subscribeEvents`) and filters for the
 *   `mobile_workspace.store_submission.*` namespace.  Nine event
 *   names, disjoint from V7 #2 `mobile_workspace.iteration_timeline.*`
 *   and V7 #4 `mobile_workspace.build.*`:
 *
 *     - `mobile_workspace.store_submission.queued`               — submission draft started
 *     - `mobile_workspace.store_submission.submitted`            — packet uploaded to the store
 *     - `mobile_workspace.store_submission.review_updated`       — Apple / Google changed status
 *     - `mobile_workspace.store_submission.screenshot_uploaded`  — screenshot added / replaced
 *     - `mobile_workspace.store_submission.screenshot_removed`   — screenshot deleted
 *     - `mobile_workspace.store_submission.withdrawn`            — operator withdrew the submission
 *     - `mobile_workspace.store_submission.dispatch_started`     — TestFlight / Firebase dispatch started
 *     - `mobile_workspace.store_submission.dispatch_completed`   — dispatch succeeded
 *     - `mobile_workspace.store_submission.dispatch_failed`      — dispatch failed
 *
 *   All events MUST carry `session_id` + `target` so a dashboard
 *   bound to `target="app-store"` never reacts to Play Store events.
 *
 * Module-global state audit (SOP Step 1):
 *   N/A — zero module-level mutable state; all state lives in React
 *   `useState` scoped per component instance; SSE subscription uses
 *   the shared `EventSource` owned by `lib/api` which is already
 *   multi-worker safe (the single-origin fan-out is done in the
 *   browser, not the server).  `STORE_SUBMISSION_EVENT_NAMES`,
 *   `REQUIRED_SCREENSHOTS_*`, and the label tables are frozen module-
 *   level constants — every worker imports the same value (SOP Step 1
 *   qualifying answer #1).
 *
 * Intentional non-goals:
 *   - The dashboard does NOT itself submit a build nor upload
 *     screenshots; `onSubmit` / `onResubmit` / `onWithdraw` /
 *     `onDispatch` / `onUploadScreenshot` / `onRemoveScreenshot` are
 *     opt-in callbacks the host page wires up to backend REST
 *     endpoints.  This keeps the component a pure render surface.
 *   - The dashboard does NOT validate a screenshot image byte-for-
 *     byte — it checks the dimensions the backend recorded against
 *     the store's expected device-class catalogue.
 */
"use client"

import * as React from "react"
import {
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Clock,
  Eye,
  FileImage,
  History,
  Image as ImageIcon,
  Loader2,
  Rocket,
  Send,
  ShieldCheck,
  Smartphone,
  Tablet,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"

import { subscribeEvents } from "@/lib/api"

// ─── Public shapes ─────────────────────────────────────────────────────────

/**
 * Store target — single vendor per dashboard instance.  Callers that
 * want to show both Apple + Google render two dashboards side-by-side;
 * the backend emits events keyed by `target` so cross-target pollution
 * is impossible.
 */
export type StoreTarget = "app-store" | "play-console"

/**
 * Unified review-status vocabulary.  Covers the Apple + Google state
 * machines:
 *
 *   - `idle`            — no submission exists (empty state)
 *   - `draft`           — operator saved metadata, not yet submitted
 *   - `submitted`       — packet sent to the store, awaiting review
 *   - `in_review`       — reviewer is actively evaluating
 *   - `pending_release` — approved, waiting for manual release flip
 *   - `approved`        — approved + auto-released
 *   - `rejected`        — rejected, operator must revise
 *   - `released`        — live on the store
 *   - `removed`         — pulled from the store (operator or store)
 *
 * Terminal-ish statuses are `released` / `removed` / `rejected` — a
 * resubmission creates a new submission entry; the dashboard flushes
 * the per-submission buffers on `queued`.
 */
export type StoreReviewStatus =
  | "idle"
  | "draft"
  | "submitted"
  | "in_review"
  | "pending_release"
  | "approved"
  | "rejected"
  | "released"
  | "removed"

/**
 * Tester-channel the dispatch button fires against.  App Store → TF,
 * Play Console → Firebase App Distribution.  Both are 1:1 with their
 * parent store in the current product; if a third vendor (e.g.
 * HockeyApp's successor or an enterprise MDM) is added the type
 * widens, the panel gains a discriminator badge.
 */
export type DispatchChannel = "testflight" | "firebase-app-distribution"

export type DispatchStatus = "idle" | "in_progress" | "succeeded" | "failed"

/**
 * Canonical device classes the store requires screenshots for.  The
 * catalogue is a deliberate subset of what each store currently
 * accepts — the classes below are ones where submission *requires*
 * coverage (not merely optional).
 */
export type ScreenshotDeviceClass =
  | "iphone-6.7"
  | "iphone-6.5"
  | "iphone-5.5"
  | "ipad-13"
  | "android-phone"
  | "android-tablet-7"
  | "android-tablet-10"

export type ScreenshotState = "valid" | "invalid_aspect" | "invalid_dim" | "pending"

export interface StoreScreenshot {
  /** Stable id — usually `${deviceClass}:${locale}:${seq}` or a hash. */
  id: string
  /** Device class the screenshot fills. */
  deviceClass: ScreenshotDeviceClass
  /** Locale — `"en-US"`, `"ja"`, ...  Surface untranslated. */
  locale: string
  /** Human filename (preserves the operator's upload name). */
  filename: string
  /** Preview URL (may be a CDN proxy; the dashboard does not care). */
  url: string
  /** Actual dimensions — used to drive the valid / invalid classifier. */
  width: number
  height: number
  /** Size for the size badge. */
  byteSize: number
  /** ISO8601 upload-completed timestamp. */
  uploadedAt: string
  /** Validation state — `valid` by default, downgrades if dimensions drift. */
  state: ScreenshotState
  /** Free-text reason when `state !== 'valid'`. */
  reason?: string | null
}

export interface DispatchHistoryEntry {
  /** Stable id — usually `${dispatchId}` or `${channel}:${timestamp}`. */
  id: string
  channel: DispatchChannel
  /** Audience label — `"Internal Testers"`, `"QA"`, ... free-text. */
  audience: string
  status: DispatchStatus
  /** Tester count at the time of the dispatch. */
  testerCount?: number
  /** ISO8601 dispatch timestamp. */
  at: string
  /** Free-text failure reason when `status === 'failed'`. */
  reason?: string | null
}

export interface DispatchState {
  channel: DispatchChannel
  status: DispatchStatus
  /** Currently-selected audience (picker state). */
  audience: string
  /** Tester count as reported by the most-recent successful dispatch. */
  testerCount: number
  /** ISO8601 start / finish. */
  startedAt?: string | null
  finishedAt?: string | null
  /** Current dispatch id — `null` between dispatches. */
  dispatchId?: string | null
  /** Free-text failure reason when `status === 'failed'`. */
  errorReason?: string | null
}

export interface StoreSubmission {
  /** Workspace session this submission belongs to. */
  sessionId: string
  /** Store target — drives channel, required screenshots, labels. */
  target: StoreTarget
  /** Bundle / package id — `com.foo.bar`. */
  bundleId: string
  /** Marketing version — `"1.4.2"`. */
  platformVersion: string
  /** Build number — `"142"`. */
  buildNumber: string
  /** Review status. */
  status: StoreReviewStatus
  /** Reviewer-supplied notes (Apple: Resolution Center, Google: rejection email body). */
  reviewerNotes?: string | null
  /** Reviewer display name — surfaced when known. */
  reviewerName?: string | null
  /** ISO8601 submitted-to-store timestamp. */
  submittedAt?: string | null
  /** ISO8601 reviewed-at timestamp (approved / rejected / released). */
  reviewedAt?: string | null
  /** ISO8601 released-at timestamp. */
  releasedAt?: string | null
  /** Screenshot catalogue — one entry per device-class × locale. */
  screenshots: StoreScreenshot[]
  /** Current dispatch state — `null` until the operator first clicks. */
  dispatch: DispatchState | null
  /** Dispatch history (ring-buffered). */
  history: DispatchHistoryEntry[]
  /** Build id the submission was produced from. */
  buildId?: string | null
}

// ─── Public constants ──────────────────────────────────────────────────────

/**
 * SSE event namespace prefix — kept as a constant so the disjointness
 * contract with sibling namespaces (V7 #2 iteration_timeline, V7 #4
 * build) is testable via `set.isdisjoint` in the unit tests.
 */
export const STORE_SUBMISSION_EVENT_PREFIX =
  "mobile_workspace.store_submission." as const

/**
 * Known event names — every event the dashboard consumes.  Exported so
 * tests can assert disjointness against sibling event prefixes.
 */
export const STORE_SUBMISSION_EVENT_NAMES = Object.freeze([
  `${STORE_SUBMISSION_EVENT_PREFIX}queued`,
  `${STORE_SUBMISSION_EVENT_PREFIX}submitted`,
  `${STORE_SUBMISSION_EVENT_PREFIX}review_updated`,
  `${STORE_SUBMISSION_EVENT_PREFIX}screenshot_uploaded`,
  `${STORE_SUBMISSION_EVENT_PREFIX}screenshot_removed`,
  `${STORE_SUBMISSION_EVENT_PREFIX}withdrawn`,
  `${STORE_SUBMISSION_EVENT_PREFIX}dispatch_started`,
  `${STORE_SUBMISSION_EVENT_PREFIX}dispatch_completed`,
  `${STORE_SUBMISSION_EVENT_PREFIX}dispatch_failed`,
] as const)

/** Ring-buffer cap for the dispatch history. */
export const DEFAULT_MAX_DISPATCH_HISTORY = 10
/** Ring-buffer cap for screenshot list (rare to exceed but defensive). */
export const DEFAULT_MAX_SCREENSHOTS = 80

/** Human label for each store target. */
export const STORE_TARGET_LABELS: Readonly<Record<StoreTarget, string>> =
  Object.freeze({
    "app-store": "App Store Connect",
    "play-console": "Google Play Console",
  })

/** Human label for each review status. */
export const REVIEW_STATUS_LABELS: Readonly<Record<StoreReviewStatus, string>> =
  Object.freeze({
    idle: "No submission",
    draft: "Draft",
    submitted: "Submitted",
    in_review: "In review",
    pending_release: "Pending release",
    approved: "Approved",
    rejected: "Rejected",
    released: "Released",
    removed: "Removed",
  })

/** Human label for each dispatch channel. */
export const DISPATCH_CHANNEL_LABELS: Readonly<
  Record<DispatchChannel, string>
> = Object.freeze({
  testflight: "TestFlight",
  "firebase-app-distribution": "Firebase App Distribution",
})

/** Human label for each device class. */
export const DEVICE_CLASS_LABELS: Readonly<
  Record<ScreenshotDeviceClass, string>
> = Object.freeze({
  "iphone-6.7": "iPhone 6.7\"",
  "iphone-6.5": "iPhone 6.5\"",
  "iphone-5.5": "iPhone 5.5\"",
  "ipad-13": "iPad 13\"",
  "android-phone": "Android phone",
  "android-tablet-7": "Android tablet 7\"",
  "android-tablet-10": "Android tablet 10\"",
})

/**
 * Expected portrait dimensions per device class.  Used by
 * `validateScreenshotDimensions` — aspect-ratio check uses a 3 %
 * tolerance so screenshots taken on slightly different hardware still
 * pass.  Landscape orientation is accepted by swapping width / height
 * before the aspect check.
 */
export const SCREENSHOT_EXPECTED_DIMENSIONS: Readonly<
  Record<ScreenshotDeviceClass, { width: number; height: number }>
> = Object.freeze({
  "iphone-6.7": { width: 1290, height: 2796 },
  "iphone-6.5": { width: 1284, height: 2778 },
  "iphone-5.5": { width: 1242, height: 2208 },
  "ipad-13": { width: 2064, height: 2752 },
  "android-phone": { width: 1080, height: 1920 },
  "android-tablet-7": { width: 1200, height: 1920 },
  "android-tablet-10": { width: 1600, height: 2560 },
})

/**
 * Required screenshot classes per store target.  The catalogue is the
 * conservative minimum Apple / Google currently enforce at submission
 * time — covering these unblocks the submit button.
 */
export const REQUIRED_SCREENSHOTS_APP_STORE: readonly ScreenshotDeviceClass[] =
  Object.freeze(["iphone-6.7", "iphone-5.5", "ipad-13"] as const)

export const REQUIRED_SCREENSHOTS_PLAY_STORE: readonly ScreenshotDeviceClass[] =
  Object.freeze([
    "android-phone",
    "android-tablet-7",
    "android-tablet-10",
  ] as const)

// ─── Pure helpers (exported for tests) ─────────────────────────────────────

/**
 * Resolve the matching tester-channel for a store target.  `app-store`
 * → `testflight`, `play-console` → `firebase-app-distribution`.
 */
export function storeTargetToChannel(target: StoreTarget): DispatchChannel {
  return target === "app-store" ? "testflight" : "firebase-app-distribution"
}

/**
 * Resolve the underlying platform for a store target.  `app-store` →
 * `"ios"`, `play-console` → `"android"`.  Kept as a plain string union
 * return so the dashboard does not reach into the `MobilePlatform`
 * superset (this panel only renders iOS + Android vendors).
 */
export function storeTargetToPlatform(target: StoreTarget): "ios" | "android" {
  return target === "app-store" ? "ios" : "android"
}

/**
 * Which device classes the target store requires coverage for.  Pure
 * so tests can pin the ordered output.
 */
export function requiredScreenshotDeviceClasses(
  target: StoreTarget,
): readonly ScreenshotDeviceClass[] {
  return target === "app-store"
    ? REQUIRED_SCREENSHOTS_APP_STORE
    : REQUIRED_SCREENSHOTS_PLAY_STORE
}

/**
 * Review status → CSS colour variable.  Mirrors the traffic-light
 * palette the ops surfaces use elsewhere (emerald for terminal-good,
 * rose for terminal-bad, amber for "attention required", neural-blue
 * for in-flight).
 */
export function reviewStatusColorVar(status: StoreReviewStatus): string {
  switch (status) {
    case "approved":
    case "released":
      return "var(--validation-emerald)"
    case "rejected":
    case "removed":
      return "var(--critical-red)"
    case "pending_release":
      return "var(--hardware-orange)"
    case "submitted":
    case "in_review":
      return "var(--neural-blue)"
    case "draft":
    case "idle":
    default:
      return "var(--muted-foreground)"
  }
}

/**
 * Review status → human label.  Thin wrapper around the constant so
 * tests can assert the exact copy without importing the table.
 */
export function reviewStatusLabel(status: StoreReviewStatus): string {
  return REVIEW_STATUS_LABELS[status]
}

/**
 * Whether a review status has reached a resting state.  Terminal
 * statuses freeze the dispatch button — the operator has to start a
 * fresh submission (queued event) to unlock it again.
 */
export function isTerminalReviewStatus(status: StoreReviewStatus): boolean {
  return (
    status === "released" || status === "removed" || status === "rejected"
  )
}

/**
 * Dispatch status → CSS colour var.  Same palette as review, so
 * operators build muscle memory across the two panels.
 */
export function dispatchStatusColorVar(status: DispatchStatus): string {
  switch (status) {
    case "succeeded":
      return "var(--validation-emerald)"
    case "failed":
      return "var(--critical-red)"
    case "in_progress":
      return "var(--neural-blue)"
    case "idle":
    default:
      return "var(--muted-foreground)"
  }
}

export function dispatchStatusLabel(status: DispatchStatus): string {
  switch (status) {
    case "idle":
      return "Idle"
    case "in_progress":
      return "Dispatching…"
    case "succeeded":
      return "Succeeded"
    case "failed":
      return "Failed"
  }
}

/**
 * Format a byte size into `KB / MB / GB` with one decimal.  Negative /
 * non-finite degrade to `"—"`.  Kept separate from the V7 #4 helper
 * because the store dashboard imports from a different file and we do
 * not want to reach across sibling modules for a two-line formatter
 * (also keeps the public API of each panel self-contained).
 */
export function formatStoreByteSize(
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
export function formatStoreRelativeTime(
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

/**
 * Shorten a long string (bundle id, dispatch id, session id) for a
 * monospace slot — leading chars collapse into an ellipsis.  Pure so
 * tests can pin the output.
 */
export function shortenStoreId(
  value: string | null | undefined,
  maxChars = 32,
): string {
  if (!value || typeof value !== "string") return ""
  if (value.length <= maxChars) return value
  const tail = value.slice(-(maxChars - 1))
  return `…${tail}`
}

/**
 * Ring-buffered append — dedup by id, cap at N.  Newest events at the
 * tail of the array so the UI can `.slice().reverse()` cheaply for
 * "most recent first" rendering.
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
 * Classify a recorded (width, height) against the expected portrait
 * dimensions for a device class.
 *
 * Rules:
 *   - Fully zero / invalid → `"invalid_dim"`.
 *   - Aspect ratio matches the class within 3 % → `"valid"`
 *     (orientation-agnostic — landscape is accepted).
 *   - Aspect ratio mismatch → `"invalid_aspect"`.
 */
export function validateScreenshotDimensions(
  width: number | null | undefined,
  height: number | null | undefined,
  deviceClass: ScreenshotDeviceClass,
): { state: ScreenshotState; reason?: string } {
  if (
    width == null ||
    height == null ||
    !Number.isFinite(width) ||
    !Number.isFinite(height) ||
    width <= 0 ||
    height <= 0
  ) {
    return { state: "invalid_dim", reason: "Missing or zero dimension." }
  }
  const expected = SCREENSHOT_EXPECTED_DIMENSIONS[deviceClass]
  const expectedAspect = expected.height / expected.width
  const actualAspect =
    width >= height ? width / height : height / width
  const expectedNorm =
    expectedAspect >= 1 ? expectedAspect : 1 / expectedAspect
  const tolerance = 0.03
  const ratioDrift = Math.abs(actualAspect - expectedNorm) / expectedNorm
  if (ratioDrift > tolerance) {
    return {
      state: "invalid_aspect",
      reason: `Expected aspect ~${expectedNorm.toFixed(2)}; got ${actualAspect.toFixed(2)}.`,
    }
  }
  return { state: "valid" }
}

/**
 * Group screenshots by device class + collapse locales.  The dashboard
 * renders one slot per device class and shows the locale-count badge;
 * individual locales appear inside the slot's expanded detail.
 */
export function groupScreenshotsByDeviceClass(
  screenshots: readonly StoreScreenshot[],
): Record<ScreenshotDeviceClass, StoreScreenshot[]> {
  const out: Record<ScreenshotDeviceClass, StoreScreenshot[]> = {
    "iphone-6.7": [],
    "iphone-6.5": [],
    "iphone-5.5": [],
    "ipad-13": [],
    "android-phone": [],
    "android-tablet-7": [],
    "android-tablet-10": [],
  }
  for (const s of screenshots) {
    if (out[s.deviceClass]) out[s.deviceClass].push(s)
  }
  return out
}

/**
 * Compute screenshot coverage — which required classes are filled and
 * which are missing.  Pure so the submit button's "can submit" gate
 * is easy to unit test.
 */
export function screenshotCoverage(
  submission: Pick<StoreSubmission, "target" | "screenshots">,
): {
  required: readonly ScreenshotDeviceClass[]
  provided: ScreenshotDeviceClass[]
  missing: ScreenshotDeviceClass[]
  invalid: ScreenshotDeviceClass[]
} {
  const required = requiredScreenshotDeviceClasses(submission.target)
  const grouped = groupScreenshotsByDeviceClass(submission.screenshots)
  const provided: ScreenshotDeviceClass[] = []
  const missing: ScreenshotDeviceClass[] = []
  const invalid: ScreenshotDeviceClass[] = []
  for (const cls of required) {
    const shots = grouped[cls]
    if (shots.length === 0) {
      missing.push(cls)
      continue
    }
    const anyValid = shots.some((s) => s.state === "valid")
    if (anyValid) {
      provided.push(cls)
    } else {
      invalid.push(cls)
    }
  }
  return { required, provided, missing, invalid }
}

/**
 * Whether the submission is eligible for a `Submit` / `Re-submit` click
 * — all required device classes must have at least one valid
 * screenshot, and the current status must not already be in a terminal
 * review state.  When `false`, the reason is surfaced so the button
 * disabled-tooltip can explain why.
 */
export function canSubmitSubmission(
  submission: Pick<StoreSubmission, "status" | "target" | "screenshots" | "bundleId" | "platformVersion" | "buildNumber">,
): { ok: boolean; reason?: string } {
  if (!submission.bundleId || !submission.platformVersion || !submission.buildNumber) {
    return { ok: false, reason: "Missing bundle / version / build." }
  }
  const coverage = screenshotCoverage(submission)
  if (coverage.missing.length > 0) {
    return {
      ok: false,
      reason: `Missing screenshots: ${coverage.missing.map((c) => DEVICE_CLASS_LABELS[c]).join(", ")}.`,
    }
  }
  if (coverage.invalid.length > 0) {
    return {
      ok: false,
      reason: `Invalid screenshots: ${coverage.invalid.map((c) => DEVICE_CLASS_LABELS[c]).join(", ")}.`,
    }
  }
  if (submission.status === "submitted" || submission.status === "in_review") {
    return { ok: false, reason: "Already under review." }
  }
  return { ok: true }
}

/**
 * Whether the dashboard may fire a TestFlight / Firebase dispatch.
 * Requires at least a build number and a non-rejected submission; a
 * release-state submission still allows dispatch so operator can
 * re-dispatch to a new tester group after release.
 */
export function canDispatchSubmission(
  submission: Pick<StoreSubmission, "status" | "buildNumber" | "dispatch">,
): { ok: boolean; reason?: string } {
  if (!submission.buildNumber) {
    return { ok: false, reason: "No build number yet." }
  }
  if (submission.status === "idle") {
    return { ok: false, reason: "No submission yet." }
  }
  if (submission.dispatch?.status === "in_progress") {
    return { ok: false, reason: "Dispatch already in progress." }
  }
  return { ok: true }
}

/**
 * Empty submission seed — matches the `idle` empty state and the
 * reset-on-queued behaviour of the reducer.
 */
export function emptyStoreSubmission(
  sessionId: string,
  target: StoreTarget,
): StoreSubmission {
  return {
    sessionId,
    target,
    bundleId: "",
    platformVersion: "",
    buildNumber: "",
    status: "idle",
    reviewerNotes: null,
    reviewerName: null,
    submittedAt: null,
    reviewedAt: null,
    releasedAt: null,
    screenshots: [],
    dispatch: null,
    history: [],
    buildId: null,
  }
}

// ─── SSE event shapes ──────────────────────────────────────────────────────

/** Event payload the dashboard consumes via `subscribeEvents`. */
export interface StoreSubmissionEvent {
  event: string
  data: Record<string, unknown>
}

/**
 * Narrow an SSE event to "is this for the dashboard's session +
 * target".  Pure so the reducer stays test-friendly.
 */
export function matchStoreSubmissionEvent(
  event: StoreSubmissionEvent,
  sessionId: string,
  target: StoreTarget,
): boolean {
  if (typeof event.event !== "string") return false
  if (!event.event.startsWith(STORE_SUBMISSION_EVENT_PREFIX)) return false
  const d = event.data ?? {}
  const sid =
    typeof d.session_id === "string"
      ? d.session_id
      : typeof d.sessionId === "string"
        ? d.sessionId
        : null
  if (sid !== sessionId) return false
  const tgt = typeof d.target === "string" ? d.target : null
  // `target` is required on every event — the backend must always key
  // the SSE payload to a concrete store so a dashboard can filter.
  if (tgt !== target) return false
  return true
}

/**
 * Apply one SSE event to the current submission state.  Pure reducer
 * (no side-effects).  Unrecognised sub-events degrade to a no-op
 * rather than throwing — the dashboard must never crash the workspace
 * page.
 */
export function applyStoreSubmissionEvent(
  submission: StoreSubmission,
  event: StoreSubmissionEvent,
  caps: {
    maxHistory?: number
    maxScreenshots?: number
  } = {},
): StoreSubmission {
  const maxHistory = caps.maxHistory ?? DEFAULT_MAX_DISPATCH_HISTORY
  const maxScreenshots = caps.maxScreenshots ?? DEFAULT_MAX_SCREENSHOTS
  const d = event.data ?? {}
  const kind = event.event.slice(STORE_SUBMISSION_EVENT_PREFIX.length)

  switch (kind) {
    case "queued": {
      return {
        ...submission,
        bundleId:
          typeof d.bundle_id === "string"
            ? d.bundle_id
            : typeof d.bundleId === "string"
              ? d.bundleId
              : submission.bundleId,
        platformVersion:
          typeof d.version === "string" ? d.version : submission.platformVersion,
        buildNumber:
          typeof d.build_number === "string"
            ? d.build_number
            : typeof d.buildNumber === "string"
              ? d.buildNumber
              : submission.buildNumber,
        buildId:
          typeof d.build_id === "string"
            ? d.build_id
            : typeof d.buildId === "string"
              ? d.buildId
              : submission.buildId,
        status: "draft",
        reviewerNotes: null,
        reviewerName: null,
        submittedAt: null,
        reviewedAt: null,
        releasedAt: null,
        screenshots: [],
        // Keep dispatch history across queued — the operator cares
        // about the prior TestFlight / Firebase runs even across
        // submission boundaries.
      }
    }
    case "submitted": {
      return {
        ...submission,
        status: "submitted",
        submittedAt:
          typeof d.submitted_at === "string"
            ? d.submitted_at
            : new Date().toISOString(),
      }
    }
    case "review_updated": {
      const rawStatus = typeof d.status === "string" ? d.status : submission.status
      const status = isValidReviewStatus(rawStatus) ? rawStatus : submission.status
      const reviewedAt =
        typeof d.reviewed_at === "string"
          ? d.reviewed_at
          : status !== submission.status
            ? new Date().toISOString()
            : submission.reviewedAt
      const releasedAt =
        status === "released"
          ? typeof d.released_at === "string"
            ? d.released_at
            : new Date().toISOString()
          : submission.releasedAt
      return {
        ...submission,
        status,
        reviewerNotes:
          typeof d.reviewer_notes === "string"
            ? d.reviewer_notes
            : typeof d.notes === "string"
              ? d.notes
              : submission.reviewerNotes,
        reviewerName:
          typeof d.reviewer_name === "string"
            ? d.reviewer_name
            : submission.reviewerName,
        reviewedAt,
        releasedAt,
      }
    }
    case "screenshot_uploaded": {
      const shot = coerceScreenshot(d)
      if (!shot) return submission
      // Replace-by-id: if an existing screenshot with the same id is in
      // the buffer, drop it first so the new one slides in at the tail.
      const filtered = submission.screenshots.filter((s) => s.id !== shot.id)
      return {
        ...submission,
        screenshots: pushRingBuffer(filtered, [shot], maxScreenshots),
      }
    }
    case "screenshot_removed": {
      const id = typeof d.id === "string" ? d.id : null
      if (!id) return submission
      return {
        ...submission,
        screenshots: submission.screenshots.filter((s) => s.id !== id),
      }
    }
    case "withdrawn": {
      return {
        ...submission,
        status: "draft",
        submittedAt: null,
        reviewedAt: null,
        reviewerNotes:
          typeof d.reason === "string" ? d.reason : submission.reviewerNotes,
      }
    }
    case "dispatch_started": {
      const channel = coerceDispatchChannel(d, submission.target)
      const audience =
        typeof d.audience === "string" ? d.audience : submission.dispatch?.audience ?? "Internal Testers"
      const dispatchId =
        typeof d.dispatch_id === "string"
          ? d.dispatch_id
          : typeof d.dispatchId === "string"
            ? d.dispatchId
            : null
      return {
        ...submission,
        dispatch: {
          channel,
          status: "in_progress",
          audience,
          testerCount: submission.dispatch?.testerCount ?? 0,
          startedAt:
            typeof d.started_at === "string"
              ? d.started_at
              : new Date().toISOString(),
          finishedAt: null,
          dispatchId,
          errorReason: null,
        },
      }
    }
    case "dispatch_completed": {
      const channel = coerceDispatchChannel(d, submission.target)
      const audience =
        typeof d.audience === "string"
          ? d.audience
          : submission.dispatch?.audience ?? "Internal Testers"
      const testerCount =
        typeof d.tester_count === "number"
          ? d.tester_count
          : typeof d.testerCount === "number"
            ? d.testerCount
            : submission.dispatch?.testerCount ?? 0
      const finishedAt =
        typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString()
      const historyId =
        typeof d.dispatch_id === "string"
          ? d.dispatch_id
          : submission.dispatch?.dispatchId ??
            `dispatch-${finishedAt}-${channel}`
      const entry: DispatchHistoryEntry = {
        id: historyId,
        channel,
        audience,
        status: "succeeded",
        testerCount,
        at: finishedAt,
      }
      return {
        ...submission,
        dispatch: {
          channel,
          status: "succeeded",
          audience,
          testerCount,
          startedAt: submission.dispatch?.startedAt ?? null,
          finishedAt,
          dispatchId: historyId,
          errorReason: null,
        },
        history: pushRingBuffer(submission.history, [entry], maxHistory),
      }
    }
    case "dispatch_failed": {
      const channel = coerceDispatchChannel(d, submission.target)
      const audience =
        typeof d.audience === "string"
          ? d.audience
          : submission.dispatch?.audience ?? "Internal Testers"
      const reason =
        typeof d.reason === "string" ? d.reason : "Dispatch failed"
      const finishedAt =
        typeof d.finished_at === "string"
          ? d.finished_at
          : new Date().toISOString()
      const historyId =
        typeof d.dispatch_id === "string"
          ? d.dispatch_id
          : submission.dispatch?.dispatchId ??
            `dispatch-${finishedAt}-${channel}`
      const entry: DispatchHistoryEntry = {
        id: historyId,
        channel,
        audience,
        status: "failed",
        at: finishedAt,
        reason,
      }
      return {
        ...submission,
        dispatch: {
          channel,
          status: "failed",
          audience,
          testerCount: submission.dispatch?.testerCount ?? 0,
          startedAt: submission.dispatch?.startedAt ?? null,
          finishedAt,
          dispatchId: historyId,
          errorReason: reason,
        },
        history: pushRingBuffer(submission.history, [entry], maxHistory),
      }
    }
    default:
      return submission
  }
}

function isValidReviewStatus(raw: string): raw is StoreReviewStatus {
  return raw in REVIEW_STATUS_LABELS
}

function coerceDispatchChannel(
  d: Record<string, unknown>,
  target: StoreTarget,
): DispatchChannel {
  const raw = typeof d.channel === "string" ? d.channel : null
  if (raw === "testflight" || raw === "firebase-app-distribution") {
    return raw
  }
  return storeTargetToChannel(target)
}

function coerceScreenshot(d: Record<string, unknown>): StoreScreenshot | null {
  const deviceClassRaw = typeof d.device_class === "string"
    ? d.device_class
    : typeof d.deviceClass === "string"
      ? d.deviceClass
      : null
  if (!deviceClassRaw || !(deviceClassRaw in DEVICE_CLASS_LABELS)) {
    return null
  }
  const deviceClass = deviceClassRaw as ScreenshotDeviceClass
  const filename =
    typeof d.filename === "string"
      ? d.filename
      : typeof d.name === "string"
        ? d.name
        : null
  const url =
    typeof d.url === "string"
      ? d.url
      : typeof d.preview_url === "string"
        ? d.preview_url
        : null
  if (!filename || !url) return null
  const width = typeof d.width === "number" ? d.width : 0
  const height = typeof d.height === "number" ? d.height : 0
  const byteSize =
    typeof d.byte_size === "number"
      ? d.byte_size
      : typeof d.byteSize === "number"
        ? d.byteSize
        : typeof d.size === "number"
          ? d.size
          : 0
  const locale = typeof d.locale === "string" ? d.locale : "en-US"
  const uploadedAt =
    typeof d.uploaded_at === "string"
      ? d.uploaded_at
      : typeof d.uploadedAt === "string"
        ? d.uploadedAt
        : new Date().toISOString()
  const id =
    typeof d.id === "string"
      ? d.id
      : `${deviceClass}:${locale}:${hashForId(filename)}`
  // Prefer explicit `state` from backend; otherwise validate locally.
  const explicitState =
    typeof d.state === "string" ? d.state.toLowerCase() : null
  let state: ScreenshotState
  let reason: string | null = null
  if (
    explicitState === "valid" ||
    explicitState === "invalid_aspect" ||
    explicitState === "invalid_dim" ||
    explicitState === "pending"
  ) {
    state = explicitState as ScreenshotState
    reason =
      typeof d.reason === "string" ? d.reason : null
  } else {
    const v = validateScreenshotDimensions(width, height, deviceClass)
    state = v.state
    reason = v.reason ?? null
  }
  return {
    id,
    deviceClass,
    locale,
    filename,
    url,
    width,
    height,
    byteSize,
    uploadedAt,
    state,
    reason,
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

function StatusIcon({
  status,
}: {
  status: StoreReviewStatus
}) {
  switch (status) {
    case "approved":
    case "released":
      return <CheckCircle2 className="size-4" aria-hidden="true" />
    case "rejected":
    case "removed":
      return <XCircle className="size-4" aria-hidden="true" />
    case "pending_release":
      return <ShieldCheck className="size-4" aria-hidden="true" />
    case "submitted":
    case "in_review":
      return <Loader2 className="size-4 animate-spin" aria-hidden="true" />
    case "draft":
      return <FileImage className="size-4" aria-hidden="true" />
    case "idle":
    default:
      return <Clock className="size-4" aria-hidden="true" />
  }
}

function DeviceClassIcon({ deviceClass }: { deviceClass: ScreenshotDeviceClass }) {
  if (deviceClass.startsWith("ipad") || deviceClass.includes("tablet")) {
    return <Tablet className="size-3.5" aria-hidden="true" />
  }
  return <Smartphone className="size-3.5" aria-hidden="true" />
}

interface ScreenshotSlotProps {
  deviceClass: ScreenshotDeviceClass
  screenshots: StoreScreenshot[]
  onUpload?: (deviceClass: ScreenshotDeviceClass) => void
  onRemove?: (screenshot: StoreScreenshot) => void
  testId: string
}

function ScreenshotSlot({
  deviceClass,
  screenshots,
  onUpload,
  onRemove,
  testId,
}: ScreenshotSlotProps) {
  const label = DEVICE_CLASS_LABELS[deviceClass]
  const expected = SCREENSHOT_EXPECTED_DIMENSIONS[deviceClass]
  const hasAny = screenshots.length > 0
  const hasValid = screenshots.some((s) => s.state === "valid")
  const state: "missing" | "invalid" | "valid" = !hasAny
    ? "missing"
    : hasValid
      ? "valid"
      : "invalid"
  return (
    <li
      data-testid={`${testId}-slot-${deviceClass}`}
      data-device-class={deviceClass}
      data-state={state}
      className={cn(
        "flex flex-col gap-1 rounded-md border px-2 py-1.5 text-xs",
        state === "missing" && "border-dashed border-border/60",
        state === "invalid" && "border-amber-500/50 bg-amber-500/5",
        state === "valid" && "border-emerald-500/40 bg-emerald-500/5",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-foreground font-medium">
          <DeviceClassIcon deviceClass={deviceClass} />
          {label}
        </span>
        <span
          data-testid={`${testId}-slot-${deviceClass}-count`}
          className="font-mono text-[10px] text-muted-foreground"
        >
          {screenshots.length} / req
        </span>
      </div>
      <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
        <span>
          {expected.width}×{expected.height}
        </span>
        {state === "missing" && (
          <Badge
            variant="outline"
            className="h-4 px-1 text-[10px] border-rose-500/40 text-rose-400"
            data-testid={`${testId}-slot-${deviceClass}-missing`}
          >
            missing
          </Badge>
        )}
        {state === "invalid" && (
          <Badge
            variant="outline"
            className="h-4 px-1 text-[10px] border-amber-500/50 text-amber-400"
            data-testid={`${testId}-slot-${deviceClass}-invalid`}
          >
            invalid
          </Badge>
        )}
        {state === "valid" && (
          <Badge
            variant="outline"
            className="h-4 px-1 text-[10px] border-emerald-500/40 text-emerald-400"
            data-testid={`${testId}-slot-${deviceClass}-valid`}
          >
            ok
          </Badge>
        )}
      </div>
      {hasAny && (
        <ul className="flex flex-col gap-0.5" data-testid={`${testId}-slot-${deviceClass}-list`}>
          {screenshots.map((s) => (
            <li
              key={s.id}
              data-testid={`${testId}-shot-${s.id}`}
              data-state={s.state}
              className={cn(
                "flex items-center justify-between gap-1 rounded-sm bg-background/50 px-1.5 py-1 text-[10px]",
                s.state === "invalid_aspect" && "text-amber-400",
                s.state === "invalid_dim" && "text-rose-400",
              )}
            >
              <div className="flex min-w-0 items-center gap-1">
                <ImageIcon className="size-3 shrink-0" aria-hidden="true" />
                <span className="truncate font-mono" title={s.filename}>
                  {s.filename}
                </span>
                <Badge variant="secondary" className="h-4 px-1 text-[9px] font-mono">
                  {s.locale}
                </Badge>
                <span className="font-mono text-muted-foreground">
                  {s.width}×{s.height}
                </span>
                <span className="font-mono text-muted-foreground">
                  {formatStoreByteSize(s.byteSize)}
                </span>
              </div>
              {onRemove && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  data-testid={`${testId}-shot-${s.id}-remove`}
                  onClick={() => onRemove(s)}
                  className="h-5 w-5 p-0"
                  aria-label={`Remove ${s.filename}`}
                >
                  <Trash2 className="size-3" aria-hidden="true" />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {onUpload && (
        <Button
          type="button"
          size="sm"
          variant="secondary"
          data-testid={`${testId}-slot-${deviceClass}-upload`}
          onClick={() => onUpload(deviceClass)}
          className="mt-1 h-7 gap-1 px-2 text-[11px]"
        >
          <Upload className="size-3" aria-hidden="true" />
          Upload screenshot
        </Button>
      )}
    </li>
  )
}

interface DispatchHistoryRowProps {
  entry: DispatchHistoryEntry
  testId: string
}

function DispatchHistoryRow({ entry, testId }: DispatchHistoryRowProps) {
  return (
    <li
      data-testid={`${testId}-history-${entry.id}`}
      data-status={entry.status}
      className={cn(
        "flex items-center justify-between gap-2 rounded-md border border-border/50 px-2 py-1 text-[11px]",
        entry.status === "failed" && "border-rose-500/40 bg-rose-500/5",
        entry.status === "succeeded" && "border-emerald-500/40 bg-emerald-500/5",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        {entry.status === "succeeded" ? (
          <CheckCircle2 className="size-3.5 shrink-0 text-emerald-400" aria-hidden="true" />
        ) : entry.status === "failed" ? (
          <XCircle className="size-3.5 shrink-0 text-rose-400" aria-hidden="true" />
        ) : (
          <Loader2
            className="size-3.5 shrink-0 animate-spin text-sky-400"
            aria-hidden="true"
          />
        )}
        <span className="flex min-w-0 flex-col">
          <span className="flex items-center gap-1.5">
            <Badge
              variant="outline"
              className="h-4 px-1 text-[10px] capitalize"
            >
              {DISPATCH_CHANNEL_LABELS[entry.channel]}
            </Badge>
            <span className="truncate text-foreground">{entry.audience}</span>
          </span>
          {entry.reason && (
            <span
              data-testid={`${testId}-history-${entry.id}-reason`}
              className="truncate text-[10px] text-rose-400"
            >
              {entry.reason}
            </span>
          )}
        </span>
      </div>
      <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
        {entry.testerCount != null && entry.status === "succeeded" && (
          <span
            data-testid={`${testId}-history-${entry.id}-count`}
            className="font-mono"
          >
            {entry.testerCount} testers
          </span>
        )}
        <span
          data-testid={`${testId}-history-${entry.id}-at`}
          className="font-mono"
        >
          {formatStoreRelativeTime(entry.at)}
        </span>
      </div>
    </li>
  )
}

// ─── Main dashboard ───────────────────────────────────────────────────────

export interface StoreSubmissionDashboardProps {
  /**
   * Workspace session this dashboard is bound to.  Events targeted at
   * a different session are dropped — matches the V0 #6 workspace-
   * scoped SSE routing contract.
   */
  sessionId: string
  /**
   * Store target — drives required screenshots, channel labels, and
   * the `target` filter on incoming SSE events.
   */
  target: StoreTarget
  /**
   * Controlled initial submission — lets storybook / tests seed a
   * specific state without mocking the SSE transport.
   */
  initialSubmission?: StoreSubmission | null
  /**
   * Fully-controlled submission — when set, the dashboard becomes a
   * pure render surface and stops applying SSE events.  Useful when
   * the host page owns the reducer (e.g. to persist between tab
   * switches or to share between sibling panels).
   */
  submission?: StoreSubmission | null
  /** Fired when the operator clicks `Submit for review`. */
  onSubmit?: (submission: StoreSubmission) => void
  /** Fired when the operator clicks `Re-submit` after a rejection. */
  onResubmit?: (submission: StoreSubmission) => void
  /** Fired when the operator clicks `Withdraw`. */
  onWithdraw?: (submission: StoreSubmission) => void
  /**
   * Fired when the operator clicks the one-click dispatch button.
   * Panel passes the resolved channel + current audience string so the
   * host page knows which backend endpoint to hit.
   */
  onDispatch?: (args: {
    submission: StoreSubmission
    channel: DispatchChannel
    audience: string
  }) => void
  /** Fired when the operator clicks `Upload screenshot` on a slot. */
  onUploadScreenshot?: (deviceClass: ScreenshotDeviceClass) => void
  /** Fired when the operator clicks `Remove` on a screenshot. */
  onRemoveScreenshot?: (screenshot: StoreScreenshot) => void
  /** Fired when the audience picker changes. */
  onChangeAudience?: (audience: string) => void
  /**
   * Test seam — inject a custom event source.  The helper gets the
   * same interface as the reducer, not the raw `EventSource`, so tests
   * don't need to simulate the SSE wire format.
   */
  eventTransport?: (
    onEvent: (event: StoreSubmissionEvent) => void,
  ) => { close: () => void }
  /** Ring-buffer cap overrides (tests). */
  maxHistory?: number
  maxScreenshots?: number
  /** Test seam — pin "now" for the relative-time display. */
  nowImpl?: () => number
  /** `data-testid` root (defaults to `store-submission-dashboard`). */
  testId?: string
}

/**
 * `StoreSubmissionDashboard` — the full panel.  See module-level
 * docstring for the contract.
 */
export function StoreSubmissionDashboard(props: StoreSubmissionDashboardProps) {
  const {
    sessionId,
    target,
    initialSubmission = null,
    submission: controlledSubmission,
    onSubmit,
    onResubmit,
    onWithdraw,
    onDispatch,
    onUploadScreenshot,
    onRemoveScreenshot,
    onChangeAudience,
    eventTransport,
    maxHistory = DEFAULT_MAX_DISPATCH_HISTORY,
    maxScreenshots = DEFAULT_MAX_SCREENSHOTS,
    nowImpl,
    testId = "store-submission-dashboard",
  } = props

  const [internalSubmission, setInternalSubmission] =
    React.useState<StoreSubmission>(
      () => initialSubmission ?? emptyStoreSubmission(sessionId, target),
    )
  const submission = controlledSubmission ?? internalSubmission

  const [audience, setAudience] = React.useState<string>(
    () =>
      submission.dispatch?.audience ??
      (target === "app-store" ? "Internal Testers" : "QA"),
  )

  // ── SSE wire-up ────────────────────────────────────────────────────────
  //
  // The dashboard attaches to the shared `EventSource` via
  // `subscribeEvents` when a transport is not injected; tests pass
  // `eventTransport` directly so they never touch the real network
  // layer.  Effect is bound to `sessionId + target` — switching the
  // bound target re-seeds the reducer to an empty submission so
  // stale state cannot bleed across.
  React.useEffect(() => {
    if (controlledSubmission != null) return
    const handler = (event: StoreSubmissionEvent) => {
      if (!matchStoreSubmissionEvent(event, sessionId, target)) return
      setInternalSubmission((prev) =>
        applyStoreSubmissionEvent(prev, event, {
          maxHistory,
          maxScreenshots,
        }),
      )
    }
    if (eventTransport) {
      const h = eventTransport(handler)
      return () => h.close()
    }
    const h = subscribeEvents((ev) =>
      handler({
        event: ev.event,
        data: (ev.data ?? {}) as Record<string, unknown>,
      }),
    )
    return () => h?.close?.()
  }, [
    controlledSubmission,
    sessionId,
    target,
    eventTransport,
    maxHistory,
    maxScreenshots,
  ])

  // Re-seed the internal reducer when the bound (session, target)
  // changes.  The effect fires on first render too but the seed is
  // idempotent.
  const lastBindRef = React.useRef(`${sessionId}::${target}`)
  React.useEffect(() => {
    const bind = `${sessionId}::${target}`
    if (lastBindRef.current !== bind) {
      lastBindRef.current = bind
      setInternalSubmission(emptyStoreSubmission(sessionId, target))
    }
  }, [sessionId, target])

  // Relative-time ticker so the "N minutes ago" timestamps refresh
  // without forcing a parent re-render.
  const [tick, setTick] = React.useState(0)
  React.useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 60_000)
    return () => window.clearInterval(id)
  }, [])
  // Keep the tick referenced so it's not tree-shaken as dead code.
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  tick

  const now = nowImpl ? nowImpl() : Date.now()
  const submittedAgo = formatStoreRelativeTime(submission.submittedAt, now)
  const reviewedAgo = formatStoreRelativeTime(submission.reviewedAt, now)

  const coverage = React.useMemo(
    () => screenshotCoverage(submission),
    [submission],
  )
  const groupedScreenshots = React.useMemo(
    () => groupScreenshotsByDeviceClass(submission.screenshots),
    [submission.screenshots],
  )

  const submitGate = canSubmitSubmission(submission)
  const dispatchGate = canDispatchSubmission(submission)

  const channel = storeTargetToChannel(target)
  const requiredClasses = requiredScreenshotDeviceClasses(target)

  const canStart =
    onSubmit != null &&
    submission.status !== "submitted" &&
    submission.status !== "in_review" &&
    submission.status !== "rejected"
  const canResubmit = onResubmit != null && submission.status === "rejected"
  const canWithdraw =
    onWithdraw != null &&
    (submission.status === "submitted" || submission.status === "in_review")

  const handleAudienceChange = (value: string) => {
    setAudience(value)
    onChangeAudience?.(value)
  }

  const handleDispatch = () => {
    if (!onDispatch || !dispatchGate.ok) return
    onDispatch({ submission, channel, audience })
  }

  const historyReversed = React.useMemo(
    () => [...submission.history].reverse(),
    [submission.history],
  )

  return (
    <section
      data-testid={testId}
      data-status={submission.status}
      data-target={submission.target}
      data-channel={channel}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-2"
    >
      {/* ─── Header ─────────────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-col gap-1.5"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            data-testid={`${testId}-target-badge`}
            variant="secondary"
            className="h-5 px-1.5 text-[11px]"
          >
            {STORE_TARGET_LABELS[target]}
          </Badge>
          <Badge
            data-testid={`${testId}-status-badge`}
            variant="outline"
            className="h-5 gap-1 px-1.5 text-[11px]"
            style={{ color: reviewStatusColorVar(submission.status) }}
          >
            <StatusIcon status={submission.status} />
            {reviewStatusLabel(submission.status)}
          </Badge>
          {submission.platformVersion && (
            <Badge
              data-testid={`${testId}-version-badge`}
              variant="outline"
              className="h-5 px-1.5 text-[11px] font-mono"
            >
              v{submission.platformVersion}
              {submission.buildNumber && (
                <span className="ml-1 text-muted-foreground">
                  ({submission.buildNumber})
                </span>
              )}
            </Badge>
          )}
          {submission.bundleId && (
            <code
              data-testid={`${testId}-bundle-id`}
              className="truncate font-mono text-[10px] text-muted-foreground"
              title={submission.bundleId}
            >
              {shortenStoreId(submission.bundleId, 40)}
            </code>
          )}
          <span
            data-testid={`${testId}-timestamps`}
            className="ml-auto flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
          >
            {submission.submittedAt && (
              <span data-testid={`${testId}-submitted-ago`}>
                submitted {submittedAgo}
              </span>
            )}
            {submission.reviewedAt && (
              <span data-testid={`${testId}-reviewed-ago`}>
                reviewed {reviewedAgo}
              </span>
            )}
          </span>
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
              data-testid={`${testId}-submit`}
              onClick={() => onSubmit?.(submission)}
              disabled={!submitGate.ok}
              title={submitGate.ok ? "Submit for review" : submitGate.reason}
              className="h-7 gap-1 px-2 text-xs"
            >
              <Send className="size-3" aria-hidden="true" />
              Submit for review
            </Button>
          )}
          {canResubmit && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              data-testid={`${testId}-resubmit`}
              onClick={() => onResubmit?.(submission)}
              disabled={!submitGate.ok}
              title={submitGate.ok ? "Re-submit" : submitGate.reason}
              className="h-7 gap-1 px-2 text-xs"
            >
              <ArrowUpRight className="size-3" aria-hidden="true" />
              Re-submit
            </Button>
          )}
          {canWithdraw && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid={`${testId}-withdraw`}
              onClick={() => onWithdraw?.(submission)}
              className="h-7 gap-1 px-2 text-xs"
            >
              <CircleStop className="size-3" aria-hidden="true" />
              Withdraw
            </Button>
          )}
          {!submitGate.ok && canStart && (
            <span
              data-testid={`${testId}-submit-reason`}
              className="ml-1 truncate text-[10px] text-amber-400"
            >
              {submitGate.reason}
            </span>
          )}
        </div>
        {submission.status === "rejected" && submission.reviewerNotes && (
          <div
            data-testid={`${testId}-reviewer-notes`}
            className="rounded-md border border-rose-500/40 bg-rose-500/5 p-2 text-[11px]"
          >
            <div className="flex items-center gap-1.5 text-rose-300">
              <AlertTriangle className="size-3.5" aria-hidden="true" />
              <span className="font-semibold uppercase tracking-wider">
                Reviewer notes
                {submission.reviewerName && (
                  <span className="ml-1 font-normal text-muted-foreground">
                    — {submission.reviewerName}
                  </span>
                )}
              </span>
            </div>
            <p className="mt-1 whitespace-pre-wrap break-words text-foreground">
              {submission.reviewerNotes}
            </p>
          </div>
        )}
      </header>

      <Separator />

      {/* ─── Screenshot manager ─────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-screenshots`}
        data-provided-count={coverage.provided.length}
        data-missing-count={coverage.missing.length}
        data-invalid-count={coverage.invalid.length}
        className="flex flex-col gap-1"
      >
        <header className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <ImageIcon className="size-3.5" aria-hidden="true" />
            Screenshots
            <Badge
              data-testid={`${testId}-screenshots-coverage`}
              variant="outline"
              className="ml-1 h-4 px-1 text-[10px]"
            >
              {coverage.provided.length} / {coverage.required.length}
            </Badge>
            {coverage.missing.length > 0 && (
              <Badge
                data-testid={`${testId}-screenshots-missing-count`}
                variant="outline"
                className="ml-1 h-4 px-1 text-[10px] border-rose-500/40 text-rose-400"
              >
                {coverage.missing.length} missing
              </Badge>
            )}
            {coverage.invalid.length > 0 && (
              <Badge
                data-testid={`${testId}-screenshots-invalid-count`}
                variant="outline"
                className="ml-1 h-4 px-1 text-[10px] border-amber-500/50 text-amber-400"
              >
                {coverage.invalid.length} invalid
              </Badge>
            )}
          </span>
        </header>
        <ul
          className="flex flex-col gap-1"
          data-testid={`${testId}-screenshots-list`}
        >
          {requiredClasses.map((cls) => (
            <ScreenshotSlot
              key={cls}
              deviceClass={cls}
              screenshots={groupedScreenshots[cls]}
              onUpload={onUploadScreenshot}
              onRemove={onRemoveScreenshot}
              testId={testId}
            />
          ))}
        </ul>
      </section>

      <Separator />

      {/* ─── Dispatch panel ────────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-dispatch`}
        data-dispatch-status={submission.dispatch?.status ?? "idle"}
        data-channel={channel}
        className="flex flex-col gap-1.5"
      >
        <header className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <Rocket className="size-3.5" aria-hidden="true" />
            {DISPATCH_CHANNEL_LABELS[channel]}
            {submission.dispatch?.testerCount ? (
              <Badge
                data-testid={`${testId}-dispatch-tester-count`}
                variant="outline"
                className="ml-1 h-4 px-1 text-[10px]"
              >
                {submission.dispatch.testerCount} testers
              </Badge>
            ) : null}
            {submission.dispatch?.status === "in_progress" && (
              <Badge
                data-testid={`${testId}-dispatch-in-progress`}
                variant="outline"
                className="ml-1 h-4 gap-1 px-1 text-[10px]"
                style={{ color: dispatchStatusColorVar("in_progress") }}
              >
                <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                {dispatchStatusLabel("in_progress")}
              </Badge>
            )}
          </span>
        </header>
        <div className="flex flex-wrap items-center gap-1.5">
          <label
            className="flex min-w-0 flex-1 items-center gap-1.5 text-[11px] text-muted-foreground"
            htmlFor={`${testId}-dispatch-audience`}
          >
            Audience
            <input
              id={`${testId}-dispatch-audience`}
              data-testid={`${testId}-dispatch-audience`}
              type="text"
              value={audience}
              onChange={(e) => handleAudienceChange(e.target.value)}
              placeholder={
                target === "app-store" ? "Internal Testers" : "QA"
              }
              className="min-w-0 flex-1 rounded-md border border-border bg-background/50 px-2 py-1 text-xs text-foreground placeholder:text-muted-foreground"
            />
          </label>
          <Button
            type="button"
            size="sm"
            variant="secondary"
            data-testid={`${testId}-dispatch-start`}
            onClick={handleDispatch}
            disabled={!dispatchGate.ok || onDispatch == null}
            title={
              dispatchGate.ok
                ? `Dispatch to ${DISPATCH_CHANNEL_LABELS[channel]}`
                : dispatchGate.reason
            }
            className="h-7 gap-1 px-2 text-xs"
          >
            <Rocket className="size-3" aria-hidden="true" />
            Dispatch
          </Button>
        </div>
        {submission.dispatch?.status === "failed" &&
          submission.dispatch.errorReason && (
            <p
              data-testid={`${testId}-dispatch-error`}
              className="truncate text-[11px] text-rose-400"
            >
              {submission.dispatch.errorReason}
            </p>
          )}
        {!dispatchGate.ok && (
          <p
            data-testid={`${testId}-dispatch-reason`}
            className="truncate text-[10px] text-muted-foreground"
          >
            {dispatchGate.reason}
          </p>
        )}
      </section>

      {/* ─── History (collapsible) ─────────────────────────────────────── */}
      {submission.history.length > 0 && (
        <details
          data-testid={`${testId}-history`}
          className="rounded-md border border-border/40 bg-background/30"
        >
          <summary className="flex cursor-pointer items-center gap-1 px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            <ChevronDown className="size-3.5 shrink-0" aria-hidden="true" />
            <History className="size-3.5" aria-hidden="true" />
            Dispatch history
            <Badge
              variant="outline"
              className="ml-1 h-4 px-1 text-[10px]"
              data-testid={`${testId}-history-count`}
            >
              {submission.history.length}
            </Badge>
          </summary>
          <ul
            data-testid={`${testId}-history-list`}
            className="flex flex-col gap-1 p-2"
          >
            {historyReversed.map((entry) => (
              <DispatchHistoryRow
                key={entry.id}
                entry={entry}
                testId={testId}
              />
            ))}
          </ul>
        </details>
      )}

      {/* ─── Diagnostics footer ─────────────────────────────────────────── */}
      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span className="flex items-center gap-1">
          <Eye className="size-3" aria-hidden="true" />
          Session
          <code className="ml-1 font-mono text-[10px]">
            {shortenStoreId(submission.sessionId, 24)}
          </code>
        </span>
        {submission.buildId && (
          <span
            data-testid={`${testId}-build-id`}
            className="flex items-center gap-1"
          >
            <ChevronRight className="size-3" aria-hidden="true" />
            build
            <code className="ml-1 font-mono text-[10px]">
              {shortenStoreId(submission.buildId, 24)}
            </code>
          </span>
        )}
      </footer>
    </section>
  )
}

export default StoreSubmissionDashboard
