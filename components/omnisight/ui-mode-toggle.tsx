"use client"

/**
 * RPG-UI — the Focus ↔ Immersive intensity dial control.
 *
 * A compact segmented toggle for the global header. Flipping it flips the
 * whole UI's game intensity (via the `omnisight:ui-mode-changed` bus that
 * `useUiMode` subscribes to): Focus = clean/professional/dense (enterprise),
 * Immersive = the full RPG showcase (enthusiast). The character brand is
 * present in both — only the juice changes.
 */

import { LayoutGrid, Sparkles } from "lucide-react"

import { useUiMode } from "@/hooks/use-ui-mode"

export function UiModeToggle({ className }: { className?: string }) {
  const { immersive, setMode } = useUiMode()
  return (
    <span
      role="group"
      aria-label="UI intensity"
      className={`hidden sm:inline-flex items-center overflow-hidden rounded-md border border-[var(--border)] font-mono text-[10px] ${className ?? ""}`}
      data-ui-mode={immersive ? "immersive" : "focus"}
    >
      {/* Icon-only to stay compact in a crowded header; the active segment
          reveals its label so the current mode is still readable at a glance. */}
      <button
        type="button"
        onClick={() => setMode("focus")}
        aria-pressed={!immersive}
        aria-label="Focus mode"
        title="Focus — clean, professional, dense"
        className={`flex items-center gap-1 px-1.5 py-1 transition-colors ${
          !immersive
            ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
            : "text-[var(--muted-foreground)] hover:bg-[var(--secondary)]"
        }`}
      >
        <LayoutGrid size={12} />
        {!immersive && <span>Focus</span>}
      </button>
      <button
        type="button"
        onClick={() => setMode("immersive")}
        aria-pressed={immersive}
        aria-label="Immersive mode"
        title="Immersive — the full RPG experience"
        className={`flex items-center gap-1 px-1.5 py-1 transition-colors ${
          immersive
            ? "bg-[var(--artifact-purple)]/25 text-[var(--artifact-purple)]"
            : "text-[var(--muted-foreground)] hover:bg-[var(--secondary)]"
        }`}
      >
        <Sparkles size={12} />
        {immersive && <span>Immersive</span>}
      </button>
    </span>
  )
}
