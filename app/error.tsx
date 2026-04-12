"use client"

import { useEffect } from "react"

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  useEffect(() => {
    console.error("[OmniSight] Unhandled error:", error)
  }, [error])

  return (
    <div className="h-screen flex flex-col items-center justify-center bg-[var(--deep-space-start)] p-6">
      <div className="holo-glass-simple p-8 rounded-lg max-w-md text-center corner-brackets">
        <div className="w-16 h-16 mx-auto mb-4 rounded-full border-2 border-[var(--critical-red)] flex items-center justify-center">
          <span className="font-mono text-2xl text-[var(--critical-red)]">!</span>
        </div>
        <h2 className="font-sans text-lg font-semibold tracking-fui text-[var(--critical-red)] mb-2">
          SYSTEM ERROR
        </h2>
        <p className="font-mono text-xs text-[var(--muted-foreground)] mb-6 break-all">
          {error.message || "An unexpected error occurred"}
        </p>
        <button
          onClick={reset}
          className="px-6 py-2 rounded bg-[var(--neural-blue)] text-black font-mono text-sm font-semibold hover:opacity-90 transition-opacity"
        >
          RELOAD SYSTEM
        </button>
      </div>
    </div>
  )
}
