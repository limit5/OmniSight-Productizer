"use client"

/**
 * WP.6 (OP-1500) — Sync-scope badge for the settings UI.
 *
 * Renders a small pill next to a setting's label that tells the user
 * how the value will sync across their devices:
 *
 *   - Globally synced → ``Synced everywhere`` (neural-green)
 *   - Per-platform   → ``This platform only`` (neural-blue)
 *   - Never synced   → ``This device only`` (neural-orange)
 *
 * The visual vocabulary intentionally mirrors the four-state catalog
 * card semantics (available / installing / installed / failed) used
 * elsewhere on the platforms surface — keeps the operator's mental
 * model coherent across pages.
 *
 * The badge is a pure presentational component; consumers pass in
 * the registry ``SettingMetadata`` directly. The settings UI calls
 * ``findSetting(registry.settings, MOTION_PREFERENCE_KEY)`` and
 * passes the result here. Unknown / unregistered settings render
 * nothing (no badge) so legacy surfaces opt-in gradually.
 */

import { Cloud, Globe2, Laptop } from "lucide-react"

import {
  SYNC_MODE_HINT,
  SYNC_MODE_LABEL,
  type SettingMetadata,
  type SyncMode,
} from "@/lib/settings-registry"

interface SyncScopeBadgeProps {
  /** Registry entry — render nothing when missing (legacy / unknown). */
  meta?: SettingMetadata
  /** Extra Tailwind classes (consumers control margin / size). */
  className?: string
  /** When true, render a longer copy line under the pill — used on
   *  detail panels; the default compact pill renders icon + label. */
  showHint?: boolean
}

const SYNC_MODE_ICON: Record<SyncMode, typeof Cloud> = {
  globally: Cloud,
  per_platform: Globe2,
  never: Laptop,
}

const SYNC_MODE_TONE: Record<SyncMode, string> = {
  globally:
    "border-[var(--neural-green)]/40 bg-[var(--neural-green)]/10 text-[var(--neural-green)]",
  per_platform:
    "border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]",
  never:
    "border-[var(--neural-orange)]/40 bg-[var(--neural-orange)]/10 text-[var(--neural-orange)]",
}

export function SyncScopeBadge({
  meta,
  className,
  showHint,
}: SyncScopeBadgeProps) {
  if (!meta) return null
  const Icon = SYNC_MODE_ICON[meta.sync]
  return (
    <span
      data-testid="sync-scope-badge"
      data-sync-mode={meta.sync}
      data-pref-key={meta.pref_key}
      className={["inline-flex flex-col items-start gap-1", className]
        .filter(Boolean)
        .join(" ")}
    >
      <span
        className={[
          "inline-flex items-center gap-1 rounded border px-1.5 py-0.5",
          "font-mono text-[10px] uppercase tracking-wider",
          SYNC_MODE_TONE[meta.sync],
        ].join(" ")}
        aria-label={`Sync mode: ${SYNC_MODE_LABEL[meta.sync]}`}
      >
        <Icon size={10} aria-hidden />
        {SYNC_MODE_LABEL[meta.sync]}
      </span>
      {showHint && (
        <span
          data-testid="sync-scope-hint"
          className="text-[10px] text-[var(--muted-foreground)]"
        >
          {SYNC_MODE_HINT[meta.sync]}
        </span>
      )}
    </span>
  )
}
