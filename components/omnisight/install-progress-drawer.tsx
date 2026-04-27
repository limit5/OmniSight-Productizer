"use client"

/**
 * BS.7.3 — Install Progress Drawer.
 *
 * Bottom-right permanent floating drawer (same surface pattern as
 * ``ToastCenter``) that surfaces in-flight install jobs while the
 * sidecar is downloading / installing them.
 *
 *   Collapsed (default):
 *     small chip ``⟳ N installing`` — lights up when at least one
 *     job is queued/running, fades out when the queue drains.
 *
 *   Expanded (operator clicked the chip):
 *     per-job row showing
 *       · entry display name (or entry_id fallback) + state pill
 *       · progress bar with conic-style accent + percentage
 *       · live speed (KB/s ↦ MB/s ↦ GB/s)
 *       · ETA — prefer backend-supplied ``eta_seconds`` (sidecar's
 *         own estimate), fall back to derived ``remaining_bytes /
 *         speed`` so the chip never says "—" while bytes are still
 *         flowing.
 *
 * Caller wiring contract
 * ──────────────────────
 * The drawer is **purely presentational** — it does not subscribe to
 * SSE itself. BS.7.4 (``hooks/use-install-jobs.ts``) handles the
 * subscription and feeds an array of in-flight ``InstallJob`` rows in
 * via the ``jobs`` prop. This separation keeps the drawer trivially
 * unit-testable (just pass a static array) and matches the BS.7
 * row-by-row split.
 *
 * The drawer accepts the full :type:`InstallJob` shape from
 * ``lib/api.ts`` and **internally filters** to the in-flight states
 * (``queued`` / ``running``). Callers can pass the unfiltered list
 * straight from the hook — when SSE fires ``installer_progress`` with
 * ``state="completed"`` the row simply drops off the drawer (the
 * catalog card lights up green via BS.7.5 / SSE state-3 transition).
 *
 * If ``onCancel`` is wired the drawer renders a small cancel button
 * per row that fires ``onCancel(job.id)`` — BS.7.7 will hand back a
 * handler that POSTs ``/installer/jobs/{id}/cancel``. Until then the
 * prop is omitted and no cancel UI renders (no dead button).
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. Speed is derived per-component-instance
 * by storing the previous (bytes_done, t_ms) pair per job-id in a React
 * ``useRef`` map; the ref resets when the component unmounts. There is
 * no in-memory cache, no singleton, no thread-locals. Each test renders
 * a fresh drawer with a fresh ref — no cross-test pollution.
 *
 * Read-after-write timing
 * ───────────────────────
 * N/A — pure React. No SQL, no async race, no asyncio.gather. Speed
 * derivation reads the just-written ref then writes the new sample;
 * single render-thread serialisation is sufficient.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { ChevronDown, ChevronUp, Download, Loader2, X } from "lucide-react"
import type { InstallJob, InstallJobState } from "@/lib/api"

const IN_FLIGHT_STATES: ReadonlySet<InstallJobState> = new Set([
  "queued",
  "running",
])

// Min ms between speed samples for the same job. Defends against rapid
// SSE bursts where two ticks land in the same animation frame and would
// otherwise produce a divide-by-near-zero speed reading.
const MIN_SAMPLE_INTERVAL_MS = 250

/**
 * BS.7.3 helper — format a non-negative byte count as a short
 * human-readable string. 1 decimal under 100 of a unit, drop decimal
 * at >= 100 (matches ``backend/pep_gateway._format_size_bytes`` 階梯
 * so install coaching card and drawer use the same vocabulary).
 *
 * Returns ``"—"`` for null / NaN / negative — caller surfaces this
 * placeholder rather than a fake "0 B" so operators tell apart "no
 * data" from "actual zero".
 */
export function formatInstallBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—"
  if (typeof n !== "number" || !Number.isFinite(n) || n < 0) return "—"
  if (n === 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  const exp = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1)
  const v = n / Math.pow(1024, exp)
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[exp]}`
}

/**
 * BS.7.3 helper — format a bytes-per-second rate. Uses the same
 * unit cascade as :func:`formatInstallBytes` and appends ``/s``.
 * Returns ``"—"`` when the rate is null / 0 / unknown so the UI
 * shows "—/s" rather than an alarming "0 KB/s" while data is
 * actually flowing (the first SSE tick has no prior sample yet).
 */
export function formatInstallSpeed(bytesPerSec: number | null | undefined): string {
  if (bytesPerSec === null || bytesPerSec === undefined) return "—"
  if (typeof bytesPerSec !== "number" || !Number.isFinite(bytesPerSec) || bytesPerSec <= 0) {
    return "—"
  }
  return `${formatInstallBytes(bytesPerSec)}/s`
}

/**
 * BS.7.3 helper — format an ETA in seconds as ``mm:ss`` (< 1 h) or
 * ``h:mm:ss`` (>= 1 h). Returns ``"—"`` for null / negative / NaN.
 * Capped at 99:59:59 so absurd values from a very small first
 * speed sample don't blow up the chip width.
 */
export function formatInstallEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—"
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—"
  const total = Math.min(Math.floor(seconds), 99 * 3600 + 59 * 60 + 59)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => n.toString().padStart(2, "0")
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`
  return `${pad(m)}:${pad(s)}`
}

/**
 * BS.7.3 helper — derive the percentage 0..100 from
 * ``bytes_done / bytes_total``. Returns ``null`` when total is
 * unknown / zero so the UI can fall back to an indeterminate
 * (striped) progress bar instead of pretending we know the size.
 */
export function deriveInstallPercent(job: InstallJob): number | null {
  const total = job.bytes_total
  if (total === null || total === undefined) return null
  if (typeof total !== "number" || !Number.isFinite(total) || total <= 0) return null
  const done = typeof job.bytes_done === "number" && Number.isFinite(job.bytes_done)
    ? Math.max(0, job.bytes_done)
    : 0
  return Math.max(0, Math.min(100, (done / total) * 100))
}

interface SpeedSample {
  bytes: number
  t: number  // ms
  speed: number  // bytes/sec, last derived value
}

export interface InstallProgressDrawerProps {
  /** Full ``InstallJob`` rows from BS.7.4's SSE hook. The drawer
   *  filters to in-flight (queued / running) internally. Defaults to
   *  the empty list so the component is mount-safe before BS.7.4
   *  wires the hook (will simply render nothing). */
  jobs?: InstallJob[]
  /** When wired, renders a per-row cancel button. BS.7.7 will hand
   *  back a handler that POSTs ``/installer/jobs/{id}/cancel``. */
  onCancel?: (jobId: string) => void
  /** Initial open state — exposed for tests; production mounts it
   *  collapsed so the drawer doesn't steal screen real-estate by
   *  default. Operators click the chip to expand. */
  initialOpen?: boolean
  /** Optional override for ``Date.now`` used to derive speed
   *  samples. Tests pass a deterministic clock so derived speed is
   *  reproducible without relying on ``vi.useFakeTimers`` reaching
   *  the ref-update inside ``useEffect``. */
  nowMs?: () => number
}

export function InstallProgressDrawer({
  jobs = [],
  onCancel,
  initialOpen = false,
  nowMs,
}: InstallProgressDrawerProps) {
  const [open, setOpen] = useState<boolean>(initialOpen)
  // Per-job speed sample ring. Keyed by job.id so a job that completes
  // and another spawns with the same numeric prefix don't cross-pollute.
  // Stored in a ref (not state) because we don't want speed-sample
  // bookkeeping to trigger a re-render — the next jobs prop change
  // already triggers one.
  const samplesRef = useRef<Map<string, SpeedSample>>(new Map())
  // Force-render bumper after speed updates so the rendered speed
  // value comes from the just-updated ref (not a stale closure).
  const [, setSampleTick] = useState(0)

  const inFlight = jobs.filter((j) => IN_FLIGHT_STATES.has(j.state))

  // Update speed samples from the latest jobs list. Runs after every
  // render whose props changed — equivalent to "subscribe to incoming
  // SSE batched into a render". We deliberately do NOT reset samples
  // for jobs that disappeared from `jobs` (the user may collapse the
  // drawer briefly and re-expand; if SSE fed a stale snapshot that
  // omitted them temporarily we don't want to lose the speed reading).
  // Instead, completed/failed/cancelled jobs naturally fall out of
  // `inFlight` and stop being read — their stale samples cost ~32 B
  // until unmount.
  useEffect(() => {
    const clock = nowMs ?? Date.now
    const now = clock()
    let dirty = false
    for (const job of inFlight) {
      const prev = samplesRef.current.get(job.id)
      const bytes = typeof job.bytes_done === "number" && Number.isFinite(job.bytes_done)
        ? Math.max(0, job.bytes_done)
        : 0
      if (!prev) {
        samplesRef.current.set(job.id, { bytes, t: now, speed: 0 })
        dirty = true
        continue
      }
      const dt = now - prev.t
      if (dt < MIN_SAMPLE_INTERVAL_MS) continue  // rate-limit
      const db = bytes - prev.bytes
      // bytes_done is monotonic (sidecar never decreases it), so a
      // negative delta would mean the sidecar restarted from 0 (e.g.
      // method retry). Treat as a fresh sample with speed=0 instead
      // of a negative speed.
      const speed = db > 0 ? (db / dt) * 1000 : 0
      samplesRef.current.set(job.id, { bytes, t: now, speed })
      dirty = true
    }
    if (dirty) setSampleTick((n) => n + 1)
  }, [inFlight, nowMs])

  const handleToggle = useCallback(() => {
    setOpen((cur) => !cur)
  }, [])

  const handleCancel = useCallback((jobId: string) => {
    if (onCancel) onCancel(jobId)
  }, [onCancel])

  if (inFlight.length === 0) return null

  if (!open) {
    return (
      <div
        className="fixed bottom-4 right-4 z-[55] pointer-events-none"
        aria-label="install progress drawer"
      >
        <button
          type="button"
          onClick={handleToggle}
          data-testid="install-drawer-chip"
          aria-label={`${inFlight.length} install${inFlight.length === 1 ? "" : "s"} in progress — open drawer`}
          aria-expanded={false}
          className="pointer-events-auto holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-cyan,#67e8f9)] backdrop-blur-sm px-3 py-1.5 flex items-center gap-2 font-mono text-[11px] tracking-wider text-[var(--neural-cyan,#67e8f9)] shadow-lg hover:bg-[var(--neural-cyan,#67e8f9)]/10"
          style={{
            boxShadow:
              "0 8px 28px -10px var(--neural-cyan,#67e8f9), 0 0 0 1px var(--neural-cyan,#67e8f9)",
          }}
        >
          <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
          <span data-testid="install-drawer-chip-count">{inFlight.length}</span>
          <span>installing</span>
        </button>
      </div>
    )
  }

  return (
    <div
      className="fixed bottom-4 right-4 z-[55] w-[min(380px,calc(100vw-2rem))] pointer-events-none"
      aria-label="install progress drawer"
    >
      <div
        data-testid="install-drawer-panel"
        className="pointer-events-auto holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-cyan,#67e8f9)]/70 backdrop-blur-sm shadow-lg"
        style={{
          boxShadow:
            "0 8px 28px -10px var(--neural-cyan,#67e8f9), 0 0 0 1px var(--neural-cyan,#67e8f9), inset 0 0 28px -18px var(--neural-cyan,#67e8f9)",
        }}
        role="region"
        aria-live="polite"
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-[var(--neural-cyan,#67e8f9)]/20">
          <Download className="w-3.5 h-3.5 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <span className="font-mono text-[10px] tracking-[0.25em] font-bold text-[var(--neural-cyan,#67e8f9)]">
            INSTALLS
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] tabular-nums">
            ({inFlight.length})
          </span>
          <button
            type="button"
            onClick={handleToggle}
            data-testid="install-drawer-collapse"
            aria-label="collapse install drawer"
            aria-expanded={true}
            className="ml-auto p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] hover:bg-white/5"
          >
            <ChevronDown className="w-3.5 h-3.5" aria-hidden />
          </button>
        </div>

        <ul className="max-h-[60vh] overflow-y-auto divide-y divide-white/5">
          {inFlight.map((job) => {
            const sample = samplesRef.current.get(job.id)
            const speed = sample?.speed
            const pct = deriveInstallPercent(job)
            // Prefer backend ETA (sidecar knows method-specific signals
            // like docker pull layer count); fall back to derived ETA
            // from remaining bytes / speed so the chip stays useful
            // before the first sidecar ETA tick.
            let etaSec: number | null = job.eta_seconds
            if (etaSec === null || etaSec === undefined) {
              if (
                typeof speed === "number" && speed > 0 &&
                typeof job.bytes_total === "number" && job.bytes_total > 0
              ) {
                const remaining = Math.max(0, job.bytes_total - (job.bytes_done ?? 0))
                etaSec = remaining / speed
              } else {
                etaSec = null
              }
            }
            const displayName = (() => {
              const meta = job.result_json
              if (meta && typeof meta === "object") {
                const hint = (meta as { display_name?: unknown }).display_name
                if (typeof hint === "string" && hint.length > 0) return hint
              }
              return job.entry_id
            })()
            return (
              <li
                key={job.id}
                data-testid={`install-drawer-row-${job.id}`}
                data-job-state={job.state}
                className="px-3 py-2"
              >
                <div className="flex items-center gap-2 mb-1">
                  <Loader2
                    className={`w-3 h-3 ${job.state === "running" ? "animate-spin text-[var(--neural-cyan,#67e8f9)]" : "text-[var(--muted-foreground,#94a3b8)]"}`}
                    aria-hidden
                  />
                  <span
                    className="font-mono text-[11px] font-bold text-[var(--foreground,#e2e8f0)] truncate flex-1 min-w-0"
                    title={displayName}
                  >
                    {displayName}
                  </span>
                  <span
                    className="font-mono text-[9px] tracking-wider px-1 py-[1px] rounded-sm border border-[var(--muted-foreground,#94a3b8)]/40 text-[var(--muted-foreground,#94a3b8)] uppercase shrink-0"
                    aria-label={`state ${job.state}`}
                  >
                    {job.state}
                  </span>
                  {onCancel && (
                    <button
                      type="button"
                      onClick={() => handleCancel(job.id)}
                      data-testid={`install-drawer-cancel-${job.id}`}
                      aria-label={`cancel install ${displayName}`}
                      className="p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--critical-red,#ef4444)] hover:bg-white/5 shrink-0"
                    >
                      <X className="w-3 h-3" aria-hidden />
                    </button>
                  )}
                </div>

                <div
                  className="h-[4px] w-full bg-white/5 rounded-sm overflow-hidden mb-1"
                  data-testid={`install-drawer-bar-${job.id}`}
                  data-progress-known={pct !== null ? "true" : "false"}
                >
                  {pct !== null ? (
                    <div
                      className="h-full transition-[width] duration-150"
                      style={{
                        width: `${pct}%`,
                        background: "var(--neural-cyan,#67e8f9)",
                      }}
                    />
                  ) : (
                    <div
                      className="h-full w-1/3 animate-pulse"
                      style={{ background: "var(--neural-cyan,#67e8f9)" }}
                    />
                  )}
                </div>

                <div className="flex items-center gap-2 font-mono text-[10px] tabular-nums text-[var(--muted-foreground,#94a3b8)]">
                  <span data-testid={`install-drawer-percent-${job.id}`}>
                    {pct !== null ? `${pct.toFixed(0)}%` : "—%"}
                  </span>
                  <span aria-hidden>·</span>
                  <span data-testid={`install-drawer-bytes-${job.id}`}>
                    {formatInstallBytes(job.bytes_done)} / {formatInstallBytes(job.bytes_total)}
                  </span>
                  <span aria-hidden>·</span>
                  <span data-testid={`install-drawer-speed-${job.id}`}>
                    {formatInstallSpeed(speed)}
                  </span>
                  <span className="ml-auto" data-testid={`install-drawer-eta-${job.id}`}>
                    ETA {formatInstallEta(etaSec)}
                  </span>
                </div>

                {job.log_tail && (
                  <div
                    className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]/80 mt-1 truncate"
                    title={job.log_tail}
                    data-testid={`install-drawer-log-${job.id}`}
                  >
                    {job.log_tail.split("\n").pop()}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
