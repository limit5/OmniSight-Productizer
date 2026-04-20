"use client"

/**
 * Host vs Target architecture indicator (H1 / 2026-04-14).
 *
 * Lives in the global header next to MODE so the operator can see at
 * a glance whether the active hardware_manifest.target_platform matches
 * the host arch — and whether the cross-compile toolchain is installed
 * — *before* a build wastes their time.
 *
 * Status colour map:
 *   native             — green   (host arch == target arch, fast path)
 *   cross_ready        — cyan    (cross-compile, toolchain on PATH)
 *   toolchain_missing  — red     (cross-compile, toolchain NOT installed)
 *   unknown_target     — amber   (target_platform set, profile yaml missing)
 *   no_target          — grey    (manifest empty, no target picked)
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { Cpu, AlertOctagon, AlertTriangle, CheckCircle2, ArrowRight, HelpCircle } from "lucide-react"

interface PlatformStatus {
  host: { arch: string; raw: string; os: string } | null
  target: {
    profile_id: string
    arch?: string
    label?: string
    toolchain?: string
    toolchain_present?: boolean
    qemu?: string
    qemu_present?: boolean
    sysroot?: string | null
    cmake_toolchain_file?: string | null
    vendor_id?: string
    sdk_version?: string
  } | null
  match: boolean | null
  status: "native" | "cross_ready" | "toolchain_missing" | "unknown_target" | "no_target"
  advice: string
}

const STATUS_META: Record<PlatformStatus["status"], {
  color: string
  bg: string
  Icon: typeof Cpu
  short: string
}> = {
  native:            { color: "var(--validation-emerald,#10b981)", bg: "rgba(16,185,129,0.12)",  Icon: CheckCircle2,  short: "NATIVE" },
  cross_ready:       { color: "var(--neural-cyan,#67e8f9)",        bg: "rgba(103,232,249,0.12)", Icon: ArrowRight,    short: "CROSS" },
  toolchain_missing: { color: "var(--critical-red,#ef4444)",       bg: "rgba(239,68,68,0.14)",   Icon: AlertOctagon,  short: "MISSING" },
  unknown_target:    { color: "var(--fui-orange,#f59e0b)",         bg: "rgba(245,158,11,0.14)",  Icon: AlertTriangle, short: "UNKNOWN" },
  no_target:         { color: "var(--muted-foreground,#94a3b8)",   bg: "rgba(148,163,184,0.10)", Icon: HelpCircle,    short: "NO TARGET" },
}

const POLL_MS = 15_000

export function ArchIndicator({ compact = false }: { compact?: boolean }) {
  const [status, setStatus] = useState<PlatformStatus | null>(null)
  const [open, setOpen] = useState(false)
  const popRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/v1/runtime/platform-status", { cache: "no-store" })
      if (!res.ok) return
      setStatus(await res.json())
    } catch {
      // network / backend down — leave previous status, ArchIndicator
      // is read-only and a stale chip is better than no chip.
    }
  }, [])

  useEffect(() => {
    void refresh() // eslint-disable-line react-hooks/set-state-in-effect -- fetch-on-mount populates state from network
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  // Outside-click + Esc close
  useEffect(() => {
    if (!open) return
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node
      if (popRef.current?.contains(t) || triggerRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false)
    }
    document.addEventListener("mousedown", onDocClick)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDocClick)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  // ── Layout safety ──────────────────────────────────────────────
  // Cap each arch label to 8 chars + ellipsis so a malformed backend
  // payload (e.g. `riscv64-unknown-linux-gnu`) cannot blow up the
  // header. Full string is still shown in the popover and as the
  // button's title attribute.
  const truncateArch = (s: string): string =>
    s.length > 8 ? s.slice(0, 7) + "…" : s

  // Fixed dimensions so the chip never expands or contracts
  // regardless of payload, and the loading placeholder occupies the
  // same box (no layout shift on mount). On compact (mobile) headers
  // the chip is a hair narrower.
  const dims = compact
    ? { width: 124, height: 18, fontSize: 10 }
    : { width: 142, height: 20, fontSize: 11 }
  const containerStyle: React.CSSProperties = {
    width: dims.width,
    height: dims.height,
    fontSize: dims.fontSize,
    flexShrink: 0,            // header flex never squashes the chip
  }

  if (!status) {
    return (
      <div
        className="inline-flex items-center justify-center font-mono text-[var(--muted-foreground,#94a3b8)] opacity-50 rounded-sm border border-dashed border-[var(--muted-foreground,#94a3b8)]/40"
        style={containerStyle}
        title="Loading platform status…"
        aria-label="loading platform status"
      >
        — / —
      </div>
    )
  }

  const meta = STATUS_META[status.status]
  const { Icon } = meta
  const hostArchFull = status.host?.arch ?? "?"
  const targetArchFull = status.target?.arch ?? "—"
  const hostArch = truncateArch(hostArchFull)
  const targetArch = truncateArch(targetArchFull)

  return (
    <div className="relative inline-flex" style={{ flexShrink: 0 }}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Host ${hostArchFull} → Target ${targetArchFull}: ${meta.short}`}
        title={`${hostArchFull} → ${targetArchFull}\n${status.advice}`}
        className="flex items-center justify-center gap-1 px-1.5 rounded-sm font-mono tabular-nums tracking-wider transition-colors hover:brightness-125 whitespace-nowrap overflow-hidden"
        style={{
          ...containerStyle,
          color: meta.color,
          background: meta.bg,
          border: `1px solid ${meta.color}`,
        }}
      >
        <Icon className="w-3 h-3 shrink-0" aria-hidden />
        <span className="truncate">{hostArch}</span>
        <span aria-hidden className="opacity-60 shrink-0">→</span>
        <span className="truncate">{targetArch}</span>
      </button>
      {open && (
        <div
          ref={popRef}
          role="dialog"
          aria-label="Platform status"
          className="absolute right-0 top-full mt-1 z-50 w-[min(340px,calc(100vw-2rem))] holo-glass-simple rounded-sm border border-[var(--neural-cyan,#67e8f9)]/40 shadow-lg p-3 font-mono text-[11px]"
        >
          <div className="flex items-center gap-1.5 mb-2">
            <Icon className="w-3.5 h-3.5" aria-hidden style={{ color: meta.color }} />
            <span className="tracking-wider font-semibold" style={{ color: meta.color }}>
              {meta.short}
            </span>
            <span className="ml-auto text-[var(--muted-foreground,#94a3b8)] text-[10px]">
              {status.host?.os}
            </span>
          </div>
          <table className="w-full mb-2">
            <tbody className="text-[var(--foreground,#e2e8f0)]">
              <tr>
                <td className="text-[var(--muted-foreground,#94a3b8)] pr-2">HOST</td>
                <td className="tabular-nums">
                  {hostArch}
                  <span className="ml-2 text-[10px] opacity-50">({status.host?.raw})</span>
                </td>
              </tr>
              <tr>
                <td className="text-[var(--muted-foreground,#94a3b8)] pr-2">TARGET</td>
                <td>
                  {status.target ? (
                    <>
                      {targetArch}
                      <span className="ml-2 text-[10px] opacity-60">{status.target.label}</span>
                    </>
                  ) : (
                    <span className="opacity-50">— none —</span>
                  )}
                </td>
              </tr>
              {status.target?.toolchain && (
                <tr>
                  <td className="text-[var(--muted-foreground,#94a3b8)] pr-2 align-top">TOOLCHAIN</td>
                  <td>
                    <code className="text-[10px]">{status.target.toolchain}</code>{" "}
                    {status.target.toolchain_present
                      ? <CheckCircle2 className="inline w-3 h-3 text-[var(--validation-emerald,#10b981)]" aria-label="present" />
                      : <AlertOctagon className="inline w-3 h-3 text-[var(--critical-red,#ef4444)]" aria-label="missing" />}
                  </td>
                </tr>
              )}
              {status.target?.qemu && (
                <tr>
                  <td className="text-[var(--muted-foreground,#94a3b8)] pr-2 align-top">QEMU</td>
                  <td>
                    <code className="text-[10px]">{status.target.qemu}</code>{" "}
                    {status.target.qemu_present
                      ? <CheckCircle2 className="inline w-3 h-3 text-[var(--validation-emerald,#10b981)]" aria-label="present" />
                      : <AlertTriangle className="inline w-3 h-3 text-[var(--fui-orange,#f59e0b)]" aria-label="missing" />}
                  </td>
                </tr>
              )}
              {status.target?.sysroot && (
                <tr>
                  <td className="text-[var(--muted-foreground,#94a3b8)] pr-2 align-top">SYSROOT</td>
                  <td className="text-[10px] break-all">{status.target.sysroot}</td>
                </tr>
              )}
              {status.target?.vendor_id && (
                <tr>
                  <td className="text-[var(--muted-foreground,#94a3b8)] pr-2">VENDOR</td>
                  <td>
                    {status.target.vendor_id}
                    {status.target.sdk_version && (
                      <span className="ml-2 text-[10px] opacity-60">v{status.target.sdk_version}</span>
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <div
            className="text-[11px] leading-snug p-2 rounded-sm"
            style={{ background: meta.bg, color: meta.color, border: `1px solid ${meta.color}40` }}
          >
            {status.advice}
          </div>
        </div>
      )}
    </div>
  )
}
