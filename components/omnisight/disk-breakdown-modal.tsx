"use client"

/**
 * BS.8.3 — Disk-breakdown report modal.
 *
 * Treemap visualisation of how the installed entries on the current
 * tenant's host carve up the catalog/cache volume. Sister modal to
 * `<CleanupUnusedModal />` (BS.8.2): the cleanup view answers "what is
 * idle?", this view answers "where did my disk go?".
 *
 * Caller wiring contract
 * ──────────────────────
 * Pure presentation — the modal does not fetch / subscribe. The page
 * wrapper (`app/settings/platforms/page.tsx`) passes the same
 * `useInstalledEntries()` snapshot the BS.8.1 list view + BS.8.2 cleanup
 * modal already consume; the modal groups by `family`, computes the
 * family + entry shares, and renders a slice-and-dice treemap. Entries
 * with unknown disk usage (`diskUsageBytes === null/undefined`) are
 * surfaced in an "Unknown size" footer list rather than dropped, so the
 * operator can spot incomplete bookkeeping.
 *
 * Treemap layout choice — slice-and-dice
 * ──────────────────────────────────────
 * Top level groups entries by family (mobile / embedded / web / software
 * / custom — the BS.8.1 5-bucket palette). Each family becomes a column
 * with width proportional to the family's total bytes; within the
 * column the entries stack vertically with heights proportional to the
 * entry's byte count. The CSS implementation is one `flex-row` of
 * `flex-col`s — `flex-grow: <bytes>` on every node does the
 * proportional sizing without any external layout library, and the
 * resulting rectangles are immediately readable. Squarified treemaps
 * look prettier but the algorithm pulls in non-trivial layout maths
 * (and a hidden dependency on container measure) for marginal benefit
 * on a list of typically <50 toolchains; we ship the simple variant
 * now and leave the option open to upgrade if the operator backlog
 * complains.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * No module-level mutable state. The component is browser-only — the
 * uvicorn `--workers N` model does not apply. Every render derives
 * the family aggregation from the immutable `entries` prop via the pure
 * helpers `computeFamilyTotals` + `groupByFamilyForTreemap`; tests pin
 * those helpers without rendering.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * No API round-trip — pure derivation from the `entries` prop. The
 * page wrapper owns the `useInstalledEntries()` refresh path, so any
 * write→read race is owned by BS.8.2 / BS.8.4 (which already pass that
 * audit). Re-opening the modal after a successful uninstall sees the
 * post-commit snapshot via the same React render cycle.
 */

import { useMemo } from "react"
import { HardDrive, PieChart } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  CATALOG_FAMILIES,
  type CatalogFamily,
  coerceFamily,
} from "@/components/omnisight/catalog-tab"
import { formatInstallBytes } from "@/components/omnisight/install-progress-drawer"
import type { InstalledEntry } from "@/components/omnisight/installed-tab"

// ─────────────────────────────────────────────────────────────────────
// Family palette — kept local so the modal owns its own visual
// vocabulary. Mirrors the chip palette in <InstalledTab /> but uses
// solid background tints (rather than border-only) since the treemap
// cells need filled rectangles.
// ─────────────────────────────────────────────────────────────────────

const FAMILY_LABEL: Record<CatalogFamily, string> = {
  mobile: "Mobile",
  embedded: "Embedded",
  web: "Web",
  software: "Software",
  custom: "Custom",
}

const FAMILY_FILL: Record<CatalogFamily, string> = {
  mobile: "bg-emerald-500/35 hover:bg-emerald-500/50 border-emerald-500/60 text-emerald-100",
  embedded: "bg-amber-500/35 hover:bg-amber-500/50 border-amber-500/60 text-amber-100",
  web: "bg-sky-500/35 hover:bg-sky-500/50 border-sky-500/60 text-sky-100",
  software: "bg-violet-500/35 hover:bg-violet-500/50 border-violet-500/60 text-violet-100",
  custom: "bg-rose-500/35 hover:bg-rose-500/50 border-rose-500/60 text-rose-100",
}

const FAMILY_SWATCH: Record<CatalogFamily, string> = {
  mobile: "bg-emerald-500/55 border-emerald-500/70",
  embedded: "bg-amber-500/55 border-amber-500/70",
  web: "bg-sky-500/55 border-sky-500/70",
  software: "bg-violet-500/55 border-violet-500/70",
  custom: "bg-rose-500/55 border-rose-500/70",
}

// ─────────────────────────────────────────────────────────────────────
// Pure helpers — exported so BS.8.7 unit tests can lock the math
// contract without rendering.
// ─────────────────────────────────────────────────────────────────────

/** Returns the entry's disk usage in bytes, or `null` if the row did
 *  not surface a finite non-negative number. Used by every aggregator
 *  + filter so "size unknown" is handled uniformly. */
export function entryDiskBytes(entry: InstalledEntry): number | null {
  const v = entry.diskUsageBytes
  if (typeof v !== "number" || !Number.isFinite(v) || v < 0) return null
  return v
}

export interface FamilyTotal {
  family: CatalogFamily
  totalBytes: number
  entries: InstalledEntry[]
}

export interface DiskBreakdownTotals {
  /** Sum of every entry with a known disk usage. ``0`` when no entry
   *  reports a known size. */
  totalBytes: number
  /** Per-family bytes / entry list, only families that have at least
   *  one entry with known size. Sorted descending by `totalBytes` so
   *  the largest family comes first in the treemap. */
  byFamily: FamilyTotal[]
  /** Entries whose disk usage was null / undefined / negative. They
   *  cannot be sized in the treemap and are surfaced in the
   *  "Unknown size" footer instead. */
  unknownEntries: InstalledEntry[]
}

/** Aggregate the entries by family + compute totals. Pure — no side
 *  effects, deterministic given the same input. */
export function computeFamilyTotals(
  entries: ReadonlyArray<InstalledEntry>,
): DiskBreakdownTotals {
  // Seed every shipped family bucket so the legend always renders the
  // 5-bucket palette in a stable order — empty families are filtered
  // out at the end so the treemap only paints what has data.
  const buckets: Record<CatalogFamily, FamilyTotal> = {
    mobile: { family: "mobile", totalBytes: 0, entries: [] },
    embedded: { family: "embedded", totalBytes: 0, entries: [] },
    web: { family: "web", totalBytes: 0, entries: [] },
    software: { family: "software", totalBytes: 0, entries: [] },
    custom: { family: "custom", totalBytes: 0, entries: [] },
  }
  const unknown: InstalledEntry[] = []
  let total = 0
  for (const e of entries) {
    const fam = coerceFamily(e.family)
    const bytes = entryDiskBytes(e)
    if (bytes === null) {
      unknown.push(e)
      continue
    }
    buckets[fam].entries.push(e)
    buckets[fam].totalBytes += bytes
    total += bytes
  }
  // Sort each family's entry list descending so the treemap's vertical
  // slices put the heaviest entry at the top — operators glance from
  // top-left to bottom-right.
  for (const fam of CATALOG_FAMILIES) {
    buckets[fam].entries.sort(
      (a, b) => (entryDiskBytes(b) ?? 0) - (entryDiskBytes(a) ?? 0),
    )
  }
  const byFamily = CATALOG_FAMILIES
    .map((fam) => buckets[fam])
    .filter((b) => b.entries.length > 0)
    .sort((a, b) => b.totalBytes - a.totalBytes)
  return { totalBytes: total, byFamily, unknownEntries: unknown }
}

/** Convenience wrapper around `computeFamilyTotals` returning only the
 *  per-family list — exported because the treemap only needs that
 *  shape and the page-wrapper toolbar may want a cheap "N families"
 *  count without copying the totals object around. */
export function groupByFamilyForTreemap(
  entries: ReadonlyArray<InstalledEntry>,
): FamilyTotal[] {
  return computeFamilyTotals(entries).byFamily
}

/** Format a bytes-share as a percent string, ``"<1%"`` for non-zero
 *  shares that round to 0, and ``"0%"`` only for an actual zero. The
 *  surface rounds to integer percents — sub-percent precision is noise
 *  on a list-view glance. */
export function formatShare(bytes: number, totalBytes: number): string {
  if (totalBytes <= 0) return "0%"
  if (bytes <= 0) return "0%"
  const pct = (bytes / totalBytes) * 100
  if (pct < 1) return "<1%"
  return `${Math.round(pct)}%`
}

// ─────────────────────────────────────────────────────────────────────
// Public component props.
// ─────────────────────────────────────────────────────────────────────

export interface DiskBreakdownModalProps {
  /** Controlled-open. ``true`` mounts the dialog. */
  open: boolean
  /** Snapshot of installed entries — typically from
   *  `useInstalledEntries()`. The modal owns the family aggregation. */
  entries: ReadonlyArray<InstalledEntry>
  /** Fired on Esc / overlay click / Close button. */
  onClose: () => void
}

// ─────────────────────────────────────────────────────────────────────
// DiskBreakdownModal — main export.
// ─────────────────────────────────────────────────────────────────────

export function DiskBreakdownModal({
  open,
  entries,
  onClose,
}: DiskBreakdownModalProps) {
  const totals = useMemo(() => computeFamilyTotals(entries), [entries])
  const { totalBytes, byFamily, unknownEntries } = totals
  const knownEntryCount = useMemo(
    () => byFamily.reduce((acc, f) => acc + f.entries.length, 0),
    [byFamily],
  )
  const hasData = knownEntryCount > 0

  const handleOpenChange = (next: boolean) => {
    if (!next) onClose()
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="disk-breakdown-modal"
        data-entry-count={entries.length}
        data-known-entry-count={knownEntryCount}
        data-unknown-entry-count={unknownEntries.length}
        data-family-count={byFamily.length}
        data-total-bytes={totalBytes}
        className="max-w-3xl"
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="disk-breakdown-modal-title"
          >
            <PieChart
              size={14}
              className="text-[var(--muted-foreground)]"
              aria-hidden
            />
            <span>Disk breakdown</span>
            <span
              className="ml-auto inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--muted)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)]"
              data-testid="disk-breakdown-modal-total"
            >
              <HardDrive size={10} aria-hidden />
              total · {formatInstallBytes(totalBytes)}
            </span>
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-[var(--muted-foreground)]">
            How the {knownEntryCount} installed entr
            {knownEntryCount === 1 ? "y" : "ies"} carve up the catalog/cache
            volume. Cells are sized by reported disk usage; hover for the
            exact figure.
          </DialogDescription>
        </DialogHeader>

        {/* ── Empty state ─────────────────────────────────────────── */}
        {!hasData ? (
          <div
            className="flex min-h-[160px] items-center justify-center rounded-md border border-dashed border-[var(--border)] bg-[var(--card)]/30 p-6 font-mono text-xs text-[var(--muted-foreground)]"
            data-testid="disk-breakdown-modal-empty"
          >
            {entries.length === 0
              ? "No installed entries — install something from the Catalog tab to see it here."
              : "No installed entry reports a known disk usage yet — try again after the next sidecar bookkeeping pass."}
          </div>
        ) : (
          <>
            {/* ── Treemap ───────────────────────────────────────────── */}
            <div
              className="flex h-[260px] w-full overflow-hidden rounded-md border border-[var(--border)] bg-[var(--card)]"
              data-testid="disk-breakdown-modal-treemap"
              role="img"
              aria-label={`Disk breakdown across ${byFamily.length} famil${byFamily.length === 1 ? "y" : "ies"} totalling ${formatInstallBytes(totalBytes)}`}
            >
              {byFamily.map((bucket) => (
                <div
                  key={bucket.family}
                  data-testid={`disk-breakdown-modal-family-${bucket.family}`}
                  data-family-bytes={bucket.totalBytes}
                  data-family-share={formatShare(bucket.totalBytes, totalBytes)}
                  // ``flexGrow`` is the bytes share. CSS flex normalises
                  // the values so the rectangles share the row width
                  // proportionally.
                  style={{ flexGrow: bucket.totalBytes }}
                  className="flex min-w-0 flex-col border-r border-[var(--border)] last:border-r-0"
                  title={`${FAMILY_LABEL[bucket.family]} · ${formatInstallBytes(bucket.totalBytes)} (${formatShare(bucket.totalBytes, totalBytes)})`}
                >
                  {bucket.entries.map((entry) => {
                    const bytes = entryDiskBytes(entry) ?? 0
                    return (
                      <div
                        key={entry.id}
                        data-testid={`disk-breakdown-modal-cell-${entry.id}`}
                        data-entry-id={entry.id}
                        data-entry-family={bucket.family}
                        data-entry-bytes={bytes}
                        data-entry-share={formatShare(bytes, totalBytes)}
                        style={{ flexGrow: bytes }}
                        className={[
                          "flex min-h-0 flex-col justify-end overflow-hidden border-b border-[var(--background)]/40 px-2 py-1 transition-colors last:border-b-0",
                          FAMILY_FILL[bucket.family],
                        ].join(" ")}
                        title={`${entry.displayName} · ${formatInstallBytes(bytes)} (${formatShare(bytes, totalBytes)})`}
                      >
                        <span className="truncate font-orbitron text-[10px] tracking-wide">
                          {entry.displayName}
                        </span>
                        <span className="truncate font-mono text-[9px] opacity-80 tabular-nums">
                          {formatInstallBytes(bytes)} ·{" "}
                          {formatShare(bytes, totalBytes)}
                        </span>
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>

            {/* ── Family legend ─────────────────────────────────────── */}
            <ul
              className="grid grid-cols-1 gap-2 sm:grid-cols-2 md:grid-cols-3"
              data-testid="disk-breakdown-modal-legend"
            >
              {byFamily.map((bucket) => (
                <li
                  key={bucket.family}
                  data-testid={`disk-breakdown-modal-legend-item-${bucket.family}`}
                  data-family-share={formatShare(bucket.totalBytes, totalBytes)}
                  className="flex items-center gap-2 rounded border border-[var(--border)] bg-[var(--card)]/40 px-2 py-1.5 font-mono text-[10px] text-[var(--muted-foreground)]"
                >
                  <span
                    className={[
                      "inline-block h-3 w-3 shrink-0 rounded-sm border",
                      FAMILY_SWATCH[bucket.family],
                    ].join(" ")}
                    aria-hidden
                  />
                  <span className="font-orbitron text-[10px] uppercase tracking-wider text-[var(--foreground)]">
                    {FAMILY_LABEL[bucket.family]}
                  </span>
                  <span className="ml-auto inline-flex items-center gap-1 tabular-nums">
                    {formatInstallBytes(bucket.totalBytes)}
                    <span className="opacity-60">
                      ({formatShare(bucket.totalBytes, totalBytes)})
                    </span>
                  </span>
                  <span className="rounded border border-[var(--border)] bg-[var(--background)] px-1 py-0.5 text-[9px] tabular-nums">
                    {bucket.entries.length}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}

        {/* ── Unknown-size footer ──────────────────────────────────── */}
        {unknownEntries.length > 0 && (
          <div
            className="rounded border border-dashed border-[var(--border)] bg-[var(--card)]/30 px-3 py-2 font-mono text-[10px] text-[var(--muted-foreground)]"
            data-testid="disk-breakdown-modal-unknown"
          >
            <div className="mb-1 flex items-center gap-1 text-[var(--foreground)]">
              <span>
                {unknownEntries.length} entr
                {unknownEntries.length === 1 ? "y" : "ies"} without a known disk
                usage
              </span>
            </div>
            <ul
              className="flex flex-col gap-0.5"
              data-testid="disk-breakdown-modal-unknown-list"
            >
              {unknownEntries.map((entry) => (
                <li
                  key={entry.id}
                  data-testid={`disk-breakdown-modal-unknown-item-${entry.id}`}
                  className="truncate"
                  title={entry.displayName}
                >
                  · {entry.displayName}
                  {entry.version ? ` · v${entry.version}` : ""}
                </li>
              ))}
            </ul>
          </div>
        )}

        <DialogFooter>
          <button
            type="button"
            onClick={() => handleOpenChange(false)}
            className="inline-flex items-center justify-center rounded border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            data-testid="disk-breakdown-modal-close"
          >
            Close
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default DiskBreakdownModal
