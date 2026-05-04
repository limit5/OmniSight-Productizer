"use client"

/**
 * FUI 404 page — rendered by Next.js whenever no route segment matches
 * the request or a Server Component calls ``notFound()``.
 *
 * B13 Part B (#339). Replaces the shipping Next.js default (white paper
 * background) with the OmniSight dark scan-line aesthetic so the
 * command-center look stays consistent even when an operator fat-fingers
 * a URL. The technical disclosure surfaces the requested URL so they can
 * screenshot/paste it into a bug report without opening DevTools.
 */

import { useEffect, useState } from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { useTranslations } from "next-intl"
import { AlertTriangle, ChevronDown, ChevronRight, Home } from "lucide-react"
import { NeuralGrid } from "@/components/omnisight/neural-grid"

export default function NotFound() {
  const tNotFound = useTranslations("notFound")
  const tCommon = useTranslations("common")
  const pathname = usePathname()
  const [expanded, setExpanded] = useState(false)
  // ``window`` is undefined during SSR — hydrate the full URL on mount so
  // the disclosure shows the exact URL the browser asked for (host +
  // query string), not just the pathname.
  const [fullUrl, setFullUrl] = useState<string | null>(null)
  const [referrer, setReferrer] = useState<string | null>(null)

  useEffect(() => {
    setFullUrl(window.location.href)
    setReferrer(document.referrer || null)
  }, [])

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
              {tNotFound("badge")}
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
            404
          </div>

          <h1
            className="text-2xl md:text-3xl font-semibold tracking-fui text-[var(--foreground)] text-center"
            style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
          >
            {tNotFound("title")}
          </h1>
          <p className="mt-3 font-mono text-sm text-[var(--muted-foreground)] text-center leading-relaxed">
            {tNotFound("introLine1")}
            <br />
            {tNotFound("introLine2")}
          </p>

          <div className="mt-8 flex flex-col items-center gap-3">
            <Link
              href="/"
              className="group inline-flex items-center gap-2 rounded-md border border-[var(--neural-blue)] bg-[var(--neural-blue-dim)] px-6 py-3 font-mono text-sm font-semibold tracking-widest text-[var(--foreground)] transition-all hover:bg-[var(--neural-blue)] hover:text-black hover:shadow-[0_0_20px_var(--neural-blue-glow)]"
            >
              <Home size={14} aria-hidden="true" />
              <span>{tNotFound("home")}</span>
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
                <span>{tNotFound("techDetails")}</span>
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
                      HTTP Status
                    </div>
                    <div className="text-[var(--critical-red)]">
                      404 Not Found
                    </div>
                  </div>
                  <div>
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
                    Requested URL
                  </div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded border border-[var(--holo-glass-border)] bg-black/60 p-3 font-mono text-[11px] leading-relaxed text-[var(--neural-blue)]">
                    {fullUrl ?? "// resolving…"}
                  </pre>
                </div>

                {referrer && (
                  <div>
                    <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                      Referrer
                    </div>
                    <pre className="max-h-20 overflow-auto whitespace-pre-wrap break-all rounded border border-[var(--holo-glass-border)] bg-black/60 p-3 font-mono text-[11px] leading-relaxed text-[var(--foreground)]">
                      {referrer}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <p className="mt-6 text-center font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
          {tCommon("footer")}
        </p>
      </main>
    </div>
  )
}
