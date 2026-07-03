"use client"

/**
 * RPG-UI — Focus ↔ Immersive intensity dial (client-side).
 *
 * The product serves three audiences from ONE UI (operator direction):
 *   - enterprise → legibility, professional restraint  → "focus"
 *   - AI enthusiasts → game feel, immersion            → "immersive"
 *   - newcomers → brand recall (characters present in both)
 *
 * This hook is the dial: a persisted mode that surfaces can read to dial the
 * game juice up (bigger portraits, XP bars, glow) or down (compact, calm).
 * Client-only + localStorage-backed — deliberately NOT wired to the J4
 * cross-device preference backend yet (that's a follow-up); a same-tab
 * CustomEvent keeps every consumer in sync so one toggle flips the whole UI.
 *
 * Default is "focus": the professional read is the safe default for the
 * widest/riskiest audience (enterprise); immersion is one click away and
 * persists per browser.
 */

import { useCallback, useEffect, useState } from "react"

export type UiMode = "focus" | "immersive"

const STORAGE_KEY = "omnisight:ui-mode"
const EVENT = "omnisight:ui-mode-changed"
export const DEFAULT_UI_MODE: UiMode = "focus"

function isMode(v: unknown): v is UiMode {
  return v === "focus" || v === "immersive"
}

export function getUiMode(): UiMode {
  if (typeof window === "undefined") return DEFAULT_UI_MODE
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    return isMode(raw) ? raw : DEFAULT_UI_MODE
  } catch {
    return DEFAULT_UI_MODE
  }
}

export function setUiMode(mode: UiMode): void {
  if (typeof window === "undefined") return
  try {
    window.localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    /* private-mode / quota — the event still flips the live UI */
  }
  window.dispatchEvent(new CustomEvent<UiMode>(EVENT, { detail: mode }))
}

export function subscribeUiMode(cb: (mode: UiMode) => void): () => void {
  if (typeof window === "undefined") return () => {}
  const onEvent = (e: Event) => {
    const detail = (e as CustomEvent<UiMode>).detail
    if (isMode(detail)) cb(detail)
  }
  // Same-tab (our dispatch) + cross-tab (native storage event).
  const onStorage = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY && isMode(e.newValue)) cb(e.newValue)
  }
  window.addEventListener(EVENT, onEvent)
  window.addEventListener("storage", onStorage)
  return () => {
    window.removeEventListener(EVENT, onEvent)
    window.removeEventListener("storage", onStorage)
  }
}

export interface UseUiModeResult {
  mode: UiMode
  setMode: (next: UiMode) => void
  toggle: () => void
  immersive: boolean
}

export function useUiMode(): UseUiModeResult {
  // SSR/first paint uses the default; the real persisted value is read after
  // mount to avoid a hydration mismatch.
  const [mode, setLocal] = useState<UiMode>(DEFAULT_UI_MODE)

  useEffect(() => {
    setLocal(getUiMode())
    return subscribeUiMode(setLocal)
  }, [])

  const setMode = useCallback((next: UiMode) => {
    setLocal(next) // optimistic; the event echo is a no-op (same value)
    setUiMode(next)
  }, [])

  const toggle = useCallback(() => {
    setMode(getUiMode() === "immersive" ? "focus" : "immersive")
  }, [setMode])

  return { mode, setMode, toggle, immersive: mode === "immersive" }
}
