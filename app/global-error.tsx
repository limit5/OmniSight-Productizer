"use client"

/**
 * Global error boundary — rendered by Next.js when the ROOT layout itself
 * throws. B13 Part B (#339). Unlike app/error.tsx (which only wraps page
 * segments and relies on the root layout still being alive), this file
 * REPLACES the root layout including <html>/<body>. That means:
 *
 *   - globals.css, Providers, Orbitron font, and every CSS var from
 *     layout.tsx are NOT guaranteed to be loaded. We therefore ship the
 *     styling inline so the page stays legible even if the whole layout
 *     bundle is broken.
 *   - Only browser APIs + a bare <html>/<body> are safe to rely on.
 *
 * UX contract:
 *   - Dark scan-line background, critical red "ERR" badge, minimal copy.
 *   - Retry button calls ``reset()`` (Next.js will remount the tree).
 *   - "Back to home" triggers a full page reload via window.location so
 *     it works even if the Next.js router is wedged.
 *   - Tech details are always available; stack trace hidden in prod.
 */

import { useEffect, useState } from "react"

interface GlobalErrorProps {
  error: Error & { digest?: string }
  reset: () => void
}

export default function GlobalError({ error, reset }: GlobalErrorProps) {
  const [expanded, setExpanded] = useState(false)
  const [href, setHref] = useState<string>("")

  useEffect(() => {
    // eslint-disable-next-line no-console
    console.error("[OmniSight] Root layout crashed:", error)
    setHref(window.location.href)
  }, [error])

  const isProd = process.env.NODE_ENV === "production"
  const stack = !isProd ? error.stack : null

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          background:
            "radial-gradient(ellipse at center, #0a1628 0%, #040812 70%, #000 100%)",
          color: "#e5e7eb",
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "24px",
          position: "relative",
          overflow: "hidden",
        }}
      >
        <div
          aria-hidden="true"
          style={{
            position: "fixed",
            inset: 0,
            pointerEvents: "none",
            background:
              "repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(56,189,248,0.06) 2px, rgba(56,189,248,0.06) 3px)",
            zIndex: 0,
          }}
        />

        <main
          style={{
            position: "relative",
            zIndex: 1,
            maxWidth: "640px",
            width: "100%",
            border: "1px solid rgba(56,189,248,0.25)",
            background: "rgba(4, 10, 20, 0.75)",
            borderRadius: "6px",
            padding: "32px",
            boxShadow:
              "0 0 0 1px rgba(56,189,248,0.08) inset, 0 0 40px rgba(56,189,248,0.08)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "10px",
              marginBottom: "18px",
            }}
          >
            <span
              style={{
                display: "inline-block",
                width: "10px",
                height: "10px",
                borderRadius: "50%",
                background: "#ef4444",
                boxShadow: "0 0 8px #ef4444",
                animation: "omni-pulse 1.4s ease-in-out infinite",
              }}
            />
            <span
              style={{
                fontSize: "10px",
                letterSpacing: "0.3em",
                color: "#ef4444",
                textTransform: "uppercase",
              }}
            >
              SYS.ROOT · LAYOUT FAULT
            </span>
          </div>

          <div
            style={{
              textAlign: "center",
              fontSize: "72px",
              fontWeight: 700,
              letterSpacing: "0.2em",
              color: "#ef4444",
              textShadow: "0 0 10px #ef4444, 0 0 24px rgba(239,68,68,0.4)",
              lineHeight: 1,
              marginBottom: "12px",
            }}
          >
            ERR
          </div>

          <h1
            style={{
              textAlign: "center",
              fontSize: "22px",
              fontWeight: 600,
              color: "#e5e7eb",
              margin: "0 0 10px 0",
              letterSpacing: "0.1em",
            }}
          >
            系統嚴重錯誤
          </h1>
          <p
            style={{
              textAlign: "center",
              color: "#94a3b8",
              fontSize: "13px",
              lineHeight: 1.7,
              margin: "0 0 28px 0",
            }}
          >
            核心框架發生未預期的錯誤，
            <br />
            請嘗試重新載入；若問題持續請聯繫系統管理員。
          </p>

          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "10px",
              justifyContent: "center",
              marginBottom: "24px",
            }}
          >
            <button
              type="button"
              onClick={() => reset()}
              style={{
                cursor: "pointer",
                padding: "12px 22px",
                fontSize: "13px",
                fontWeight: 600,
                letterSpacing: "0.2em",
                textTransform: "uppercase",
                color: "#e5e7eb",
                background: "rgba(56,189,248,0.15)",
                border: "1px solid #38bdf8",
                borderRadius: "4px",
                fontFamily: "inherit",
              }}
            >
              ▶ 重試
            </button>
            <button
              type="button"
              onClick={() => {
                window.location.href = "/"
              }}
              style={{
                cursor: "pointer",
                padding: "12px 22px",
                fontSize: "13px",
                fontWeight: 600,
                letterSpacing: "0.2em",
                textTransform: "uppercase",
                color: "#94a3b8",
                background: "transparent",
                border: "1px solid rgba(148,163,184,0.4)",
                borderRadius: "4px",
                fontFamily: "inherit",
              }}
            >
              ◀ 回首頁
            </button>
          </div>

          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            style={{
              width: "100%",
              cursor: "pointer",
              padding: "8px 12px",
              fontSize: "11px",
              color: "#94a3b8",
              background: "rgba(0,0,0,0.3)",
              border: "1px solid rgba(148,163,184,0.25)",
              borderRadius: "3px",
              textAlign: "left",
              fontFamily: "inherit",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <span>⚠ 技術詳情 (for engineers)</span>
            <span aria-hidden="true">{expanded ? "▲" : "▼"}</span>
          </button>

          {expanded && (
            <div
              style={{
                marginTop: "10px",
                padding: "12px",
                background: "rgba(0,0,0,0.5)",
                border: "1px solid rgba(148,163,184,0.2)",
                borderRadius: "3px",
                fontSize: "11px",
                lineHeight: 1.6,
              }}
            >
              <div style={{ marginBottom: "10px" }}>
                <div
                  style={{
                    color: "#64748b",
                    fontSize: "10px",
                    textTransform: "uppercase",
                    letterSpacing: "0.15em",
                    marginBottom: "4px",
                  }}
                >
                  Error Type
                </div>
                <div style={{ color: "#ef4444" }}>{error.name || "Error"}</div>
              </div>
              <div style={{ marginBottom: "10px" }}>
                <div
                  style={{
                    color: "#64748b",
                    fontSize: "10px",
                    textTransform: "uppercase",
                    letterSpacing: "0.15em",
                    marginBottom: "4px",
                  }}
                >
                  Digest
                </div>
                <div style={{ color: "#e5e7eb", wordBreak: "break-all" }}>
                  {error.digest ?? "—"}
                </div>
              </div>
              <div style={{ marginBottom: "10px" }}>
                <div
                  style={{
                    color: "#64748b",
                    fontSize: "10px",
                    textTransform: "uppercase",
                    letterSpacing: "0.15em",
                    marginBottom: "4px",
                  }}
                >
                  URL
                </div>
                <div style={{ color: "#e5e7eb", wordBreak: "break-all" }}>
                  {href || "—"}
                </div>
              </div>
              <div style={{ marginBottom: stack ? "10px" : 0 }}>
                <div
                  style={{
                    color: "#64748b",
                    fontSize: "10px",
                    textTransform: "uppercase",
                    letterSpacing: "0.15em",
                    marginBottom: "4px",
                  }}
                >
                  Message
                </div>
                <pre
                  style={{
                    margin: 0,
                    padding: "10px",
                    maxHeight: "140px",
                    overflow: "auto",
                    background: "rgba(0,0,0,0.6)",
                    border: "1px solid rgba(148,163,184,0.2)",
                    borderRadius: "3px",
                    color: "#ef4444",
                    fontSize: "11px",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                    fontFamily: "inherit",
                  }}
                >
                  {error.message || "// no message"}
                </pre>
              </div>
              {stack ? (
                <div>
                  <div
                    style={{
                      color: "#64748b",
                      fontSize: "10px",
                      textTransform: "uppercase",
                      letterSpacing: "0.15em",
                      marginBottom: "4px",
                    }}
                  >
                    Stack Trace
                  </div>
                  <pre
                    style={{
                      margin: 0,
                      padding: "10px",
                      maxHeight: "260px",
                      overflow: "auto",
                      background: "rgba(0,0,0,0.6)",
                      border: "1px solid rgba(148,163,184,0.2)",
                      borderRadius: "3px",
                      color: "#38bdf8",
                      fontSize: "11px",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-all",
                      fontFamily: "inherit",
                    }}
                  >
                    {stack}
                  </pre>
                </div>
              ) : (
                <div
                  style={{
                    padding: "10px",
                    background: "rgba(0,0,0,0.6)",
                    border: "1px solid rgba(148,163,184,0.2)",
                    borderRadius: "3px",
                    color: "#64748b",
                    fontSize: "10px",
                    textTransform: "uppercase",
                    letterSpacing: "0.15em",
                  }}
                >
                  // stack hidden in production
                </div>
              )}
            </div>
          )}

          <p
            style={{
              marginTop: "24px",
              marginBottom: 0,
              textAlign: "center",
              fontSize: "10px",
              letterSpacing: "0.25em",
              textTransform: "uppercase",
              color: "#64748b",
            }}
          >
            OmniSight Productizer · neural command center
          </p>
        </main>

        <style>{`
          @keyframes omni-pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.4; transform: scale(0.85); }
          }
        `}</style>
      </body>
    </html>
  )
}
