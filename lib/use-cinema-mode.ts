"use client"

/**
 * Cinema-mode is an opt-in "extra sci-fi" toggle for the bootstrap /
 * setup-required pages. When ON, taste-dependent effects layer on top
 * of the base UI:
 *
 *   * typewriter boot sequence on /setup-required load
 *   * matrix-rain background behind the neural grid
 *   * "COMBAT DEPLOY" two-step confirm before /bootstrap smoke-run
 *
 * Default OFF. Persisted in plain localStorage (NOT the tenant/user-
 * scoped keys in lib/storage.ts) because these pages pre-date any
 * authenticated session — there is no tenant_id / user_id yet at the
 * moment the preference is read.
 *
 * SSR-safe: the hook returns `enabled=false` during server render and
 * the first client paint, then flips once the stored value has been
 * read. Consumers that must not render before hydration should gate
 * on the `hydrated` flag to avoid a brief cinematic-off → cinematic-
 * on flicker.
 */

import { useCallback, useEffect, useState } from "react"

const STORAGE_KEY = "omnisight:ui:cinema-mode"

export interface CinemaModeHook {
  /** Currently-enabled state. False before hydration even if the
   *  stored value is ON — see `hydrated` for the distinction. */
  enabled: boolean
  /** True once the effect has run and the localStorage value has
   *  been applied. Use to gate "render cinematic or not" decisions
   *  that should NOT flicker on first paint. */
  hydrated: boolean
  /** Flip the stored value + local state atomically. */
  toggle: () => void
}

export function useCinemaMode(): CinemaModeHook {
  const [enabled, setEnabled] = useState<boolean>(false)
  const [hydrated, setHydrated] = useState<boolean>(false)

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY)
      setEnabled(raw === "1")
    } catch {
      // localStorage can throw in sandboxed iframes / cookie-disabled
      // browsers; treat as "default off" and move on — the toggle
      // button will still work in-memory for this session.
    }
    setHydrated(true)
  }, [])

  const toggle = useCallback(() => {
    setEnabled((prev) => {
      const next = !prev
      try {
        window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0")
      } catch {
        /* ignore — in-memory state still updates */
      }
      return next
    })
  }, [])

  return { enabled, hydrated, toggle }
}
