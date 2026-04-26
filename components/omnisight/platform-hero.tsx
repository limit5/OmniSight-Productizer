"use client"

/**
 * BS.5.2 + BS.5.3 — Platforms hero panel.
 *
 * Three-column hero that anchors `/settings/platforms`:
 *   • Left   — `<OrbitalDiagram />` (3 concentric rings, 8/10/12 dot
 *              capacity, status-coloured per-entry dots, hover tooltip,
 *              click → catalog deep-link). Lives in
 *              `components/omnisight/orbital-diagram.tsx`.
 *   • Centre — live counter strip: 4 tiles for installed / available
 *              / installing / disk-used.
 *   • Right  — ENERGY CORE: vertical disk-usage bar with
 *              `entropy-scan-sweep` red sweep layered on top.
 *
 * Scope split inside the BS.5 epic:
 *   BS.5.2 — visual structure + counter wiring.
 *   BS.5.3 — per-entry orbital dots + tooltips + click-through
 *            (`<OrbitalDiagram />` swapped in here, replacing the
 *            previous file-private `<HeroOrbitalShell />` placeholder).
 *   BS.5.4 — BS.3 motion library (idle drift + cursor magnetic tilt +
 *            glass reflection) layered on top of this hero.
 *   BS.5.5 — vitest unit tests (~10 cases hero + 6 orbital).
 *
 * Data inputs: pure props (`PlatformCounters` + `entries`).
 *   Both default so the hero renders cleanly before BS.6 (catalog) /
 *   BS.7 (install pipeline) wire real numbers in. Disk values default
 *   to zero — caller plumbs live data from `useHostMetricsTick()` (or a
 *   future storage-quota endpoint) once those hooks land.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. The component is a pure render of
 * its props with `useMemo` for derived geometry. Disk-usage percent
 * is clamped 0..100 to defend against transient upstream bugs where
 * `disk_used_gb > disk_total_gb`.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A — no API calls inside this component. Visual updates flow from
 * prop changes; the live SSE tick that callers subscribe to is itself
 * commit-ordered upstream (host_metrics.tick publishes after sample
 * commit), so the hero re-renders consistently on each tick.
 */

import { useMemo } from "react"
import { Boxes, CheckCircle2, Download, HardDrive } from "lucide-react"

import {
  OrbitalDiagram,
  type InstalledPlatformEntry,
} from "@/components/omnisight/orbital-diagram"

// ─────────────────────────────────────────────────────────────────────
// Counter contract — exported so BS.5.5 tests + BS.6/BS.7 wiring share
// a single source of truth for the prop shape.
// ─────────────────────────────────────────────────────────────────────

export interface PlatformCounters {
  /** Catalog entries currently installed for this tenant. */
  installed: number
  /** Catalog entries available (catalog rows visible to operator). */
  available: number
  /** Install jobs currently in `running` or `queued` state. */
  installing: number
  /** Toolchain disk usage (live) — gigabytes used. */
  diskUsedGb: number
  /** Toolchain disk total — gigabytes (host disk total or quota). */
  diskTotalGb: number
}

export const PLATFORM_COUNTERS_ZERO: PlatformCounters = {
  installed: 0,
  available: 0,
  installing: 0,
  diskUsedGb: 0,
  diskTotalGb: 0,
}

export interface PlatformHeroProps {
  counters?: PlatformCounters
  /** Installed-platform entries that drive the orbital dots. Default
   *  empty so the hero renders cleanly before BS.6 / BS.7 wire real
   *  data; the orbital then shows placeholder dots only. */
  entries?: ReadonlyArray<InstalledPlatformEntry>
  className?: string
}

// ─────────────────────────────────────────────────────────────────────
// Disk-usage helpers — kept pure + exported so BS.5.5 tests can lock
// the threshold colour bands without touching JSX.
// ─────────────────────────────────────────────────────────────────────

export function computeDiskPercent(
  usedGb: number,
  totalGb: number,
): number {
  if (!Number.isFinite(usedGb) || !Number.isFinite(totalGb)) return 0
  if (totalGb <= 0) return 0
  const raw = (usedGb / totalGb) * 100
  if (raw < 0) return 0
  if (raw > 100) return 100
  return raw
}

export type DiskPressure = "nominal" | "elevated" | "critical"

/** Pressure bands chosen to align with the 80%/95% thresholds the
 *  H3 host-pressure coordinator already publishes (see
 *  `lib/api.ts::HostMetricsTickSample.disk_percent`). Kept narrow so
 *  the right-side bar stays calm at low usage and only flips to red
 *  once the operator should actually intervene. */
export function classifyDiskPressure(percent: number): DiskPressure {
  if (percent >= 95) return "critical"
  if (percent >= 80) return "elevated"
  return "nominal"
}

const PRESSURE_FILL: Record<DiskPressure, string> = {
  nominal: "bg-emerald-500/70",
  elevated: "bg-amber-500/80",
  critical: "bg-rose-500/85",
}

const PRESSURE_GLOW: Record<DiskPressure, string> = {
  nominal: "shadow-[0_0_18px_rgba(16,185,129,0.35)]",
  elevated: "shadow-[0_0_18px_rgba(245,158,11,0.45)]",
  critical: "shadow-[0_0_22px_rgba(244,63,94,0.55)]",
}

/** Stable empty array so `entries={EMPTY_ENTRIES}` does not invalidate
 *  the orbital's `useMemo` on every parent re-render. */
const EMPTY_ENTRIES: ReadonlyArray<InstalledPlatformEntry> = []

// ─────────────────────────────────────────────────────────────────────
// Counter tile — single stat with icon + value + label.
// ─────────────────────────────────────────────────────────────────────

interface CounterTileProps {
  testid: string
  icon: typeof Boxes
  label: string
  value: string
  accentClass: string
}

function CounterTile({ testid, icon: Icon, label, value, accentClass }: CounterTileProps) {
  return (
    <div
      data-testid={testid}
      className="rounded-md border border-[var(--border)] bg-[var(--card)]/60 px-3 py-3"
    >
      <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
        <Icon size={11} className={accentClass} />
        <span>{label}</span>
      </div>
      <div
        data-testid={`${testid}-value`}
        className={`mt-1 font-orbitron text-2xl tracking-wide ${accentClass}`}
      >
        {value}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// PlatformHero — main export.
// ─────────────────────────────────────────────────────────────────────

export function PlatformHero({
  counters = PLATFORM_COUNTERS_ZERO,
  entries = EMPTY_ENTRIES,
  className,
}: PlatformHeroProps) {
  const { installed, available, installing, diskUsedGb, diskTotalGb } = counters
  const diskPercent = useMemo(
    () => computeDiskPercent(diskUsedGb, diskTotalGb),
    [diskUsedGb, diskTotalGb],
  )
  const pressure = classifyDiskPressure(diskPercent)
  const fillClass = PRESSURE_FILL[pressure]
  const glowClass = PRESSURE_GLOW[pressure]

  // Display strings — `toLocaleString` keeps the counter strip readable
  // when an operator-tenant has many installed entries (commas / locale
  // grouping). Disk uses 1 fractional gigabyte to match
  // `host-device-panel`'s convention.
  const installedDisplay = installed.toLocaleString()
  const availableDisplay = available.toLocaleString()
  const installingDisplay = installing.toLocaleString()
  const diskDisplay = `${diskUsedGb.toFixed(1)} / ${diskTotalGb.toFixed(0)} GB`

  return (
    <section
      data-testid="platform-hero"
      data-disk-pressure={pressure}
      aria-label="Platforms hero panel"
      className={[
        "relative overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--card)] p-4 md:p-6",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="grid grid-cols-1 gap-4 md:grid-cols-[200px_1fr_120px] md:items-stretch">
        {/* ── Left: orbital diagram ─────────────────────────────── */}
        <div
          data-testid="platform-hero-orbital-frame"
          className="relative flex aspect-square items-center justify-center rounded-md border border-[var(--border)]/60 bg-black/20 p-2"
        >
          <OrbitalDiagram
            entries={entries}
            testIdPrefix="platform-hero-orbital"
            ariaLabel="Platforms orbital diagram"
          />
        </div>

        {/* ── Centre: live counters ──────────────────────────────── */}
        <div className="flex min-w-0 flex-col gap-3">
          <header
            data-testid="platform-hero-heading"
            className="flex items-center justify-between gap-2"
          >
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
                Platforms · live overview
              </div>
              <h2 className="mt-0.5 font-orbitron text-base tracking-wider text-[var(--foreground)]">
                Vertical / SDK / Runtime / BSP
              </h2>
            </div>
            <span
              data-testid="platform-hero-disk-pressure-badge"
              className={[
                "rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
                pressure === "critical"
                  ? "border-rose-500/60 text-rose-300"
                  : pressure === "elevated"
                    ? "border-amber-500/60 text-amber-300"
                    : "border-emerald-500/40 text-emerald-300",
              ].join(" ")}
            >
              {pressure}
            </span>
          </header>

          <div
            data-testid="platform-hero-counters"
            className="grid grid-cols-2 gap-2 sm:grid-cols-4"
          >
            <CounterTile
              testid="platform-hero-counter-installed"
              icon={CheckCircle2}
              label="installed"
              value={installedDisplay}
              accentClass="text-emerald-400"
            />
            <CounterTile
              testid="platform-hero-counter-available"
              icon={Boxes}
              label="available"
              value={availableDisplay}
              accentClass="text-[var(--neural-blue)]"
            />
            <CounterTile
              testid="platform-hero-counter-installing"
              icon={Download}
              label="installing"
              value={installingDisplay}
              accentClass="text-amber-400"
            />
            <CounterTile
              testid="platform-hero-counter-disk"
              icon={HardDrive}
              label="disk"
              value={diskDisplay}
              accentClass="text-[var(--foreground)]"
            />
          </div>
        </div>

        {/* ── Right: ENERGY CORE disk-usage bar ──────────────────── */}
        <div
          data-testid="platform-hero-energy-core"
          aria-label={`Disk usage ${diskPercent.toFixed(1)} percent`}
          className="flex flex-col items-center justify-between gap-2"
        >
          <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
            Energy core
          </div>

          <div
            data-testid="platform-hero-disk-bar"
            data-disk-percent={diskPercent.toFixed(2)}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(diskPercent)}
            className="entropy-scan relative w-10 flex-1 overflow-hidden rounded-md border border-[var(--border)] bg-black/40"
          >
            {/* Fill grows from the bottom — height is `diskPercent`%
             *  of the bar's content area. Layer order: fill at z-0,
             *  the .entropy-scan ::after sweep sits above (no z-index
             *  needed; absolute ::after wins by default). */}
            <div
              data-testid="platform-hero-disk-bar-fill"
              className={[
                "absolute inset-x-0 bottom-0 transition-[height] duration-500 ease-out",
                fillClass,
                glowClass,
              ].join(" ")}
              style={{ height: `${diskPercent}%` }}
            />
          </div>

          <div className="text-center font-mono text-[11px] tracking-wider text-[var(--foreground)]">
            <div data-testid="platform-hero-disk-percent">
              {diskPercent.toFixed(1)}%
            </div>
            <div className="text-[9px] text-[var(--muted-foreground)]">
              disk used
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export default PlatformHero
