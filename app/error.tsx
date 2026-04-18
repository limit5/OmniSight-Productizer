"use client"

/**
 * FUI client error boundary — rendered by Next.js when a component throws
 * during render / effect. B13 Part B (#339). Replaces the Next.js default
 * so unhandled errors stay inside the OmniSight dark scan-line aesthetic.
 *
 * UX contract:
 * - Friendly headline ("發生錯誤") + retry button that calls ``reset()``.
 * - "技術詳情" disclosure always exposes error.message + digest so operators
 *   can paste them into a bug report without opening DevTools.
 * - ``error.stack`` is surfaced ONLY outside production builds. In prod we
 *   hide the stack to avoid leaking internal paths / code layout. The stack
 *   is also often scrubbed by Next.js itself in prod, but we gate on
 *   NODE_ENV defensively.
 */

import { useEffect, useState } from "react"
import Link from "next/link"
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Home,
  RefreshCw,
} from "lucide-react"
import { NeuralGrid } from "@/components/omnisight/neural-grid"

interface ErrorPageProps {
  error: Error & { digest?: string }
  reset: () => void
}

export default function Error({ error, reset }: ErrorPageProps) {
  const [expanded, setExpanded] = useState(false)
  const [pathname, setPathname] = useState<string | null>(null)

  useEffect(() => {
    // eslint-disable-next-line no-console
    console.error("[OmniSight] Unhandled error:", error)
    setPathname(window.location.pathname + window.location.search)
  }, [error])

  const isProd = process.env.NODE_ENV === "production"
  const stack = !isProd ? error.stack : null

  return (
    <div className="relative min-h-screen flex items-center justify-center overflow-hidden bg-[var(--deep-space-start)]">
      <NeuralGrid />

      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-0 opacity-40"
        style={{
          background:
            "repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(56,189,248,0.05) 2px, rgba(56,189,248,0.05) 3px)",
        }}
      />

      <div aria-hidden="true" className="fui-scan-sweep" />

      <main className="relative z-10 w-full max-w-2xl px-6 py-10">
        <div className="holo-glass-simple corner-brackets rounded-lg p-8 md:p-10">
          <div className="mb-4 flex items-center justify-center gap-3">
            <div className="h-10 w-10 rounded-full border-2 border-[var(--critical-red)] flex items-center justify-center animate-pulse">
              <AlertTriangle size={18} className="text-[var(--critical-red)]" />
            </div>
            <span
              className="text-[10px] uppercase tracking-[0.3em] text-[var(--critical-red)]"
              style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
            >
              SYS.EXCEPTION · UNHANDLED
            </span>
          </div>

          <div
            className="mb-2 text-center text-6xl md:text-7xl font-bold uppercase tracking-[0.2em] text-[var(--critical-red)]"
            style={{
              fontFamily: "var(--font-orbitron), sans-serif",
              textShadow:
                "0 0 10px var(--critical-red), 0 0 20px var(--critical-red-dim)",
            }}
          >
            ERR
          </div>

          <h1
            className="text-2xl md:text-3xl font-semibold tracking-fui text-[var(--foreground)] text-center"
            style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
          >
            發生錯誤
          </h1>
          <p className="mt-3 font-mono text-sm text-[var(--muted-foreground)] text-center leading-relaxed">
            系統執行時發生未預期的錯誤，
            <br />
            您可以點擊下方按鈕重試，或回首頁繼續操作。
          </p>

          <div className="mt-8 flex flex-col sm:flex-row items-center justify-center gap-3">
            <button
              type="button"
              onClick={() => reset()}
              className="group inline-flex items-center gap-2 rounded-md border border-[var(--neural-blue)] bg-[var(--neural-blue-dim)] px-6 py-3 font-mono text-sm font-semibold tracking-widest text-[var(--foreground)] transition-all hover:bg-[var(--neural-blue)] hover:text-black hover:shadow-[0_0_20px_var(--neural-blue-glow)]"
            >
              <RefreshCw size={14} aria-hidden="true" />
              <span>重試</span>
            </button>
            <Link
              href="/"
              className="group inline-flex items-center gap-2 rounded-md border border-[var(--holo-glass-border)] px-6 py-3 font-mono text-sm font-semibold tracking-widest text-[var(--muted-foreground)] transition-all hover:border-[var(--neural-blue)] hover:text-[var(--neural-blue)]"
            >
              <Home size={14} aria-hidden="true" />
              <span>回首頁</span>
              <ChevronRight
                size={14}
                className="opacity-60 transition-transform group-hover:translate-x-0.5"
              />
            </Link>
          </div>

          <div className="mt-8">
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="flex w-full items-center justify-between rounded border border-[var(--holo-glass-border)] bg-black/20 px-3 py-2 font-mono text-xs text-[var(--muted-foreground)] transition-colors hover:text-[var(--neural-blue)]"
              aria-expanded={expanded}
            >
              <span className="flex items-center gap-2">
                <AlertTriangle size={12} />
                <span>技術詳情 (for engineers)</span>
              </span>
              <ChevronDown
                size={14}
                className={`transition-transform ${expanded ? "rotate-180" : ""}`}
              />
            </button>

            {expanded && (
              <div className="mt-3 space-y-3 rounded border border-[var(--holo-glass-border)] bg-black/40 p-3">
                <div className="grid grid-cols-2 gap-3 font-mono text-[11px]">
                  <div>
                    <div className="text-[var(--muted-foreground)]">
                      Error Type
                    </div>
                    <div className="text-[var(--critical-red)]">
                      {error.name || "Error"}
                    </div>
                  </div>
                  <div>
                    <div className="text-[var(--muted-foreground)]">
                      Digest
                    </div>
                    <div className="text-[var(--foreground)] break-all">
                      {error.digest ?? "—"}
                    </div>
                  </div>
                  <div className="col-span-2">
                    <div className="text-[var(--muted-foreground)]">
                      Path
                    </div>
                    <div className="text-[var(--foreground)] break-all">
                      {pathname ?? "—"}
                    </div>
                  </div>
                </div>

                <div>
                  <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                    Message
                  </div>
                  <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all rounded border border-[var(--holo-glass-border)] bg-black/60 p-3 font-mono text-[11px] leading-relaxed text-[var(--critical-red)]">
                    {error.message || "// no message"}
                  </pre>
                </div>

                {stack ? (
                  <div>
                    <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                      Stack Trace
                    </div>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded border border-[var(--holo-glass-border)] bg-black/60 p-3 font-mono text-[11px] leading-relaxed text-[var(--neural-blue)]">
                      {stack}
                    </pre>
                  </div>
                ) : (
                  <div className="rounded border border-[var(--holo-glass-border)] bg-black/60 p-3 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                    // stack hidden in production
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <p className="mt-6 text-center font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
          OmniSight Productizer · neural command center
        </p>
      </main>
    </div>
  )
}
