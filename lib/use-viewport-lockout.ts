"use client"

/**
 * OP-60 / MP.W6.8 - viewport lockout redirect helper.
 *
 * Module-global state audit: immutable default toast copy only. Redirect
 * dedupe is per hook instance.
 */

import { useEffect, useRef } from "react"
import { useRouter } from "next/navigation"
import { toast } from "sonner"

const DEFAULT_LOCKOUT_TOAST =
  "War Room is desktop-only — switching to Constellation view"

export interface ViewportLockoutOptions {
  minWidth: number
  redirectTo: string
  enabled?: boolean
  toastMessage?: string
}

export function useViewportLockout({
  minWidth,
  redirectTo,
  enabled = true,
  toastMessage = DEFAULT_LOCKOUT_TOAST,
}: ViewportLockoutOptions): void {
  const router = useRouter()
  const firedRef = useRef(false)

  useEffect(() => {
    if (
      !enabled ||
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return
    }

    const query = `(max-width: ${minWidth - 1}px)`
    const mediaQuery = window.matchMedia(query)

    const maybeRedirect = () => {
      if (firedRef.current || !mediaQuery.matches) return

      firedRef.current = true
      toast.info(toastMessage)
      router.push(redirectTo)
    }

    maybeRedirect()
    mediaQuery.addEventListener("change", maybeRedirect)

    return () => {
      mediaQuery.removeEventListener("change", maybeRedirect)
    }
  }, [enabled, minWidth, redirectTo, router, toastMessage])
}
