"use client"

/**
 * Full-screen FUI landing page shown when the backend reports
 * ``bootstrap_required`` (503) — i.e. first-run install state before the
 * operator has stepped through the install wizard.
 *
 * B13 Part A (#339). The API client redirects here instead of surfacing
 * a raw 503 toast so end users see a friendly "please finish setup"
 * screen with a single CTA into ``/bootstrap``. Operators who want the
 * raw diagnostic can expand the "技術詳情" disclosure to see the 503 JSON,
 * backend version, and each gate's green/red state.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Cloud,
  FlaskConical,
  KeyRound,
  Loader2,
  RadioTower,
  RefreshCw,
  Rocket,
  Shield,
  X,
  Zap,
} from "lucide-react"
import { NeuralGrid } from "@/components/omnisight/neural-grid"
import {
  getBootstrapStatus,
  getHealth,
  getReadyz,
  type BootstrapGates,
  type BootstrapStatusResponse,
  type ReadyzResponse,
} from "@/lib/api"

// ─── Gate meta table ──────────────────────────────────────────────────

type GateKey = keyof BootstrapGates

/** Whether the gate flips itself based on declarative system state
 *  (`auto`) or requires the operator to press a button in the wizard
 *  (`action`). Communicating this distinction up-front kills the
 *  "is my system broken?" confusion when an `action` gate is red. */
type GateNature = "auto" | "action"

interface GateDef {
  key: GateKey
  label: string
  icon: React.ComponentType<{ size?: number; className?: string }>
  nature: GateNature
  /** Short explainer shown inline — "what this checks + rough
   *  duration if the operator has to act". One line max. */
  hint: string
  /** Returns true if the gate is satisfied (green). */
  isGreen: (g: BootstrapGates) => boolean
}

const GATES: GateDef[] = [
  {
    key: "admin_password_default",
    label: "Admin Password",
    icon: KeyRound,
    nature: "auto",
    hint: "偵測出廠預設密碼是否已輪替。登入後改密碼即綠。",
    // The flag means "still on shipping default" — inverted for green.
    isGreen: (g) => !g.admin_password_default,
  },
  {
    key: "llm_provider_configured",
    label: "LLM Provider",
    icon: Shield,
    nature: "auto",
    hint: "偵測 API key 是否在環境變數或 wizard 中設妥。",
    isGreen: (g) => g.llm_provider_configured,
  },
  {
    key: "cf_tunnel_configured",
    label: "Cloudflare Tunnel",
    icon: Cloud,
    nature: "auto",
    hint: "偵測 wizard 提供的 tunnel、或 compose-managed tunnel token。",
    isGreen: (g) => g.cf_tunnel_configured,
  },
  {
    key: "smoke_passed",
    label: "Smoke Test",
    icon: FlaskConical,
    nature: "action",
    hint: "要你動手：進 wizard 跑兩個工具鏈 DAG (~2 分鐘) 驗工廠出貨。",
    isGreen: (g) => g.smoke_passed,
  },
]

// ─── Gate orbit (round 2-A) ─────────────────────────────────────────
//
// Replaces the 2×2 grid with a "star-chart" — a pulsing bootstrap core
// in the middle, 4 gates riding a dashed orbit ring at the diagonals,
// radial connection lines from core to each gate, and a comet that
// slides around the ring to keep the scene alive without distracting.
//
// Gate state colors:
//   * green (satisfied)     → green ring + sonar-ping pulse
//   * action pending        → purple ring + purple attention pulse
//   * auto pending          → muted cyan ring, no pulse (it'll flip
//                             by itself when the backend detects the
//                             condition; no operator action needed)
//
// Positions are the 45° diagonals of a circle centered at (180,180)
// with r=115 — enough gap from the core halo (~r=28) that labels
// never collide with it. Coordinates are % of the 360×360 viewBox so
// the whole diagram scales responsively inside its aspect-ratio
// container.

const ORBIT_POSITIONS: Record<GateKey, { left: string; top: string }> = {
  admin_password_default: { left: "27.4%", top: "27.4%" }, // top-left
  llm_provider_configured: { left: "72.6%", top: "27.4%" }, // top-right
  cf_tunnel_configured: { left: "72.6%", top: "72.6%" }, // bottom-right
  smoke_passed: { left: "27.4%", top: "72.6%" }, // bottom-left
}

function GateOrbitTile({
  gate,
  green,
  statusLoaded,
}: {
  gate: GateDef
  green: boolean
  statusLoaded: boolean
}) {
  const Icon = gate.icon
  const isAction = gate.nature === "action"
  const pending = !green && statusLoaded
  const pos = ORBIT_POSITIONS[gate.key]

  // Ring / icon color cascade. Green always wins; otherwise ACTION
  // gets purple, AUTO gets muted cyan.
  const ringClass = green
    ? "border-[var(--status-green)] text-[var(--status-green)]"
    : isAction
      ? "border-[var(--artifact-purple)] text-[var(--artifact-purple)]"
      : "border-[var(--neural-blue)]/40 text-[var(--neural-blue)]/70"

  // Pulse animation cascade — the "noise" should be quietest for the
  // steady-state AUTO gates and loudest for the one the operator
  // still has to act on.
  const pulseStyle: React.CSSProperties = green
    ? {
        animation:
          "orbit-gate-pulse-green 2.4s cubic-bezier(0.4,0,0.6,1) infinite",
      }
    : isAction && pending
      ? {
          animation:
            "orbit-gate-pulse-action 1.6s cubic-bezier(0.4,0,0.6,1) infinite",
        }
      : {}

  return (
    <div
      className="absolute flex flex-col items-center gap-1"
      style={{ left: pos.left, top: pos.top, transform: "translate(-50%, -50%)" }}
    >
      <div
        className={`flex h-10 w-10 items-center justify-center rounded-full border-2 bg-black/60 backdrop-blur-sm ${ringClass}`}
        style={pulseStyle}
      >
        <Icon size={16} />
      </div>
      <div className="flex flex-col items-center gap-0.5">
        <span className="whitespace-nowrap font-mono text-[10px] uppercase tracking-wider text-[var(--foreground)]">
          {gate.label}
        </span>
        <span
          className={`font-mono text-[8px] uppercase tracking-wider ${
            isAction
              ? "text-[var(--artifact-purple)]"
              : "text-[var(--neural-blue)]"
          }`}
        >
          {statusLoaded
            ? green
              ? "✓ LOCKED"
              : isAction
                ? "▶ ACTION"
                : "○ AUTO"
            : "… PROBING"}
        </span>
      </div>
    </div>
  )
}

function GateOrbit({
  gates,
  status,
}: {
  gates: GateDef[]
  status: BootstrapStatusResponse | null
}) {
  const statusLoaded = status !== null

  return (
    <div className="relative mx-auto aspect-square w-full max-w-[360px]">
      <svg
        viewBox="0 0 360 360"
        className="absolute inset-0 h-full w-full"
        aria-hidden="true"
      >
        {/* Radial connection lines from core to each gate.
            Drawn first so the orbit ring + core layer on top. */}
        {gates.map((gate) => {
          const pos = ORBIT_POSITIONS[gate.key]
          const x = (parseFloat(pos.left) / 100) * 360
          const y = (parseFloat(pos.top) / 100) * 360
          const green = status ? gate.isGreen(status.status) : false
          const stroke = green
            ? "var(--status-green)"
            : gate.nature === "action"
              ? "var(--artifact-purple)"
              : "var(--neural-blue)"
          return (
            <line
              key={gate.key}
              x1={180}
              y1={180}
              x2={x}
              y2={y}
              stroke={stroke}
              strokeWidth={0.8}
              strokeDasharray="3 4"
              opacity={0.35}
            />
          )
        })}

        {/* Orbit ring — dashed, slow rotation to feel alive. */}
        <g
          style={{
            transformOrigin: "180px 180px",
            animation: "orbit-ring-rotate 40s linear infinite",
          }}
        >
          <circle
            cx={180}
            cy={180}
            r={115}
            fill="none"
            stroke="var(--neural-blue)"
            strokeWidth={0.8}
            strokeDasharray="2 6"
            opacity={0.45}
          />
        </g>

        {/* Bootstrap core — pulsing. Halo first, then solid. */}
        <g style={{ animation: "orbit-core-glow 3s ease-in-out infinite" }}>
          <circle
            cx={180}
            cy={180}
            r={28}
            fill="var(--neural-blue)"
            opacity={0.08}
          />
          <circle
            cx={180}
            cy={180}
            r={18}
            fill="none"
            stroke="var(--neural-blue)"
            strokeWidth={1.2}
            opacity={0.6}
          />
          <circle
            cx={180}
            cy={180}
            r={9}
            fill="var(--neural-blue)"
            opacity={0.9}
          />
        </g>

        {/* Comet travelling the orbit — SMIL animation along a full
            circle path. Bright dot with a short fading tail. */}
        <circle r={3} fill="var(--neural-blue)" opacity={0.95}>
          <animateMotion
            dur="12s"
            repeatCount="indefinite"
            path="M 180,65 A 115,115 0 1,1 179.9,65 Z"
            rotate="auto"
          />
        </circle>
        <circle r={5} fill="var(--neural-blue)" opacity={0.25}>
          <animateMotion
            dur="12s"
            repeatCount="indefinite"
            path="M 180,65 A 115,115 0 1,1 179.9,65 Z"
            rotate="auto"
          />
        </circle>

        {/* CORE label in the center. */}
        <text
          x={180}
          y={215}
          textAnchor="middle"
          fill="var(--muted-foreground)"
          style={{
            fontFamily: "var(--font-mono, monospace)",
            fontSize: 8,
            letterSpacing: "0.3em",
            textTransform: "uppercase",
          }}
        >
          core
        </text>
      </svg>

      {/* Gate overlays — positioned in % so they track the SVG scale. */}
      {gates.map((gate) => (
        <GateOrbitTile
          key={gate.key}
          gate={gate}
          green={status ? gate.isGreen(status.status) : false}
          statusLoaded={statusLoaded}
        />
      ))}
    </div>
  )
}

// ─── Telemetry ticker ───────────────────────────────────────────────
//
// Always-visible thin strip along the bottom of the viewport showing
// live system telemetry. Probes /readyz every 5 s; measures latency as
// the round-trip wall time of the fetch. Data is real — if you see
// "CORE DEGRADED" here, the backend's /readyz is actually reporting at
// least one check red. Styled like ship-bridge telemetry: low vertical
// footprint, monospace, cyan/purple accents, status-green/red for
// health.

interface Telemetry {
  uplink: string
  latencyMs: number | null
  ready: boolean | null
  db: string
  migrations: string
  providers: string
  core: "NOMINAL" | "DEGRADED" | "OFFLINE" | "PROBING"
  timestamp: string
  error: string | null
}

function _formatLatency(ms: number | null): string {
  if (ms == null) return "—"
  if (ms < 1) return "<1ms"
  return `${Math.round(ms)}ms`
}

function _formatTimestamp(d: Date): string {
  const pad = (n: number) => n.toString().padStart(2, "0")
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function _deriveUplink(): string {
  if (typeof window === "undefined") return "—"
  // CF-Tunnel deploys land on a public host; localhost / 127.* means
  // the operator is running the backend locally (smoke / dev). Both
  // are valid — just label honestly.
  return window.location.host || "—"
}

function TelemetryTicker() {
  const [tel, setTel] = useState<Telemetry>({
    uplink: "—",
    latencyMs: null,
    ready: null,
    db: "…",
    migrations: "…",
    providers: "…",
    core: "PROBING",
    timestamp: "--:--:--",
    error: null,
  })

  const probe = useCallback(async () => {
    const uplink = _deriveUplink()
    const t0 = performance.now()
    try {
      const r: ReadyzResponse = await getReadyz()
      const dt = performance.now() - t0
      const checks = r.checks || {}
      const allOk = r.ready && Object.values(checks).every((c) => c.ok)
      setTel({
        uplink,
        latencyMs: dt,
        ready: r.ready,
        db: checks.db?.detail ?? "—",
        migrations: checks.migrations?.detail ?? "—",
        providers: checks.provider_chain?.detail ?? "—",
        core: allOk ? "NOMINAL" : r.ready ? "DEGRADED" : "DEGRADED",
        timestamp: _formatTimestamp(new Date()),
        error: null,
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setTel((prev) => ({
        ...prev,
        uplink,
        latencyMs: null,
        ready: false,
        core: "OFFLINE",
        timestamp: _formatTimestamp(new Date()),
        error: msg,
      }))
    }
  }, [])

  useEffect(() => {
    void probe()
    const id = setInterval(() => void probe(), 5000)
    return () => clearInterval(id)
  }, [probe])

  const coreColor =
    tel.core === "NOMINAL"
      ? "text-[var(--status-green)]"
      : tel.core === "DEGRADED"
        ? "text-[var(--critical-red)]"
        : tel.core === "OFFLINE"
          ? "text-[var(--critical-red)]"
          : "text-[var(--muted-foreground)]"

  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed bottom-0 left-0 right-0 z-20 border-t border-[var(--holo-glass-border)] bg-black/70 backdrop-blur-sm"
    >
      <div className="mx-auto flex max-w-7xl items-center gap-3 overflow-x-auto whitespace-nowrap px-4 py-1.5 font-mono text-[10px] uppercase tracking-wider">
        <span className="inline-flex items-center gap-1 text-[var(--neural-blue)]">
          <RadioTower size={10} className="animate-pulse" />
          TELEMETRY
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span>
          <span className="text-[var(--muted-foreground)]">UPLINK </span>
          <span className="text-[var(--foreground)]">{tel.uplink}</span>
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span>
          <span className="text-[var(--muted-foreground)]">LATENCY </span>
          <span className="text-[var(--foreground)]">
            {_formatLatency(tel.latencyMs)}
          </span>
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span>
          <span className="text-[var(--muted-foreground)]">DB </span>
          <span className="text-[var(--foreground)]">{tel.db}</span>
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span>
          <span className="text-[var(--muted-foreground)]">MIGR </span>
          <span className="text-[var(--foreground)]">{tel.migrations}</span>
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span>
          <span className="text-[var(--muted-foreground)]">LLM </span>
          <span className="text-[var(--foreground)]">{tel.providers}</span>
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span className={`inline-flex items-center gap-1 ${coreColor}`}>
          <Zap size={10} />
          CORE {tel.core}
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span>
          <span className="text-[var(--muted-foreground)]">SESSION </span>
          <span className="text-[var(--artifact-purple)]">
            AWAITING OPERATOR
          </span>
        </span>
        <span className="text-[var(--muted-foreground)]">·</span>
        <span className="text-[var(--muted-foreground)]">
          T {tel.timestamp}
        </span>
      </div>
    </div>
  )
}

// ─── Finalize cinematic transition (round 2-B) ──────────────────────
//
// When the backend reports ``finalized=true`` (the operator wrapped up
// the wizard in another tab, or agent-hosted smoke just flipped the
// last gate), we play a ~2s "boarding cutscene" before the redirect
// to ``/`` instead of the previous hard ``window.location.replace``.
//
// The sequence reads as "core aligned → systems nominal → rocket
// boost off → flash to white → new world loads":
//
//   0.0s   overlay fades in, backdrop blurs, text begins to ignite
//   0.8s   ALL SYSTEMS NOMINAL is fully glowing
//   1.0s   rocket lifts off, thrust streak elongates
//   1.7s   rocket scales to a speck, fades
//   1.7s   whip-flash to white
//   2.0s   window.location.replace("/")
//
// If the user refreshes or aborts inside this window (unlikely — the
// overlay is pointer-events-none on the redirect trigger, but the
// back button still works), the side-effect on unmount cancels the
// timer so nothing ghost-navigates.

function FinalizeTransition({ onDone }: { onDone: () => void }) {
  useEffect(() => {
    const id = window.setTimeout(onDone, 2000)
    return () => window.clearTimeout(id)
  }, [onDone])

  return (
    <div
      role="alert"
      aria-live="assertive"
      aria-label="bootstrap finalized — initializing command center"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-xl"
      style={{
        animation:
          "finalize-overlay-enter 400ms cubic-bezier(0.25,0.1,0.25,1) both",
      }}
    >
      {/* Whip-flash overlay — transparent until the final 200 ms, then
          washes everything to white right before we hand off to "/". */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          animation: "finalize-flash 2000ms cubic-bezier(0.6,0,0.3,1) forwards",
        }}
      />

      {/* Main cinematic column. */}
      <div className="relative z-10 flex flex-col items-center gap-8 px-6 text-center">
        {/* Rocket — centered below the text line; boosts upward. */}
        <div className="relative flex flex-col items-center">
          {/* Thrust streak — a vertical gradient column beneath the
              rocket that elongates as the rocket rises, sold as plasma
              exhaust. */}
          <div
            aria-hidden="true"
            className="absolute left-1/2 top-8 -translate-x-1/2 rounded-full"
            style={{
              width: "6px",
              background:
                "linear-gradient(to bottom, var(--neural-blue) 0%, transparent 100%)",
              animation:
                "finalize-thrust-streak 1600ms cubic-bezier(0.3,0,0.5,1) 400ms forwards",
              transformOrigin: "top center",
            }}
          />
          <div
            style={{
              animation:
                "finalize-rocket-boost 1500ms cubic-bezier(0.4,0,0.2,1) 500ms forwards",
            }}
          >
            <Rocket
              size={52}
              className="text-[var(--neural-blue)]"
              strokeWidth={1.5}
            />
          </div>
        </div>

        {/* Headline — Orbitron glow that ignites into frame. */}
        <div
          className="font-bold uppercase text-[var(--neural-blue)]"
          style={{
            fontFamily: "var(--font-orbitron), sans-serif",
            fontSize: "clamp(1.75rem, 5vw, 3rem)",
            animation:
              "finalize-text-ignite 800ms cubic-bezier(0.25,0.1,0.25,1) forwards",
            opacity: 0,
          }}
        >
          ALL&nbsp;SYSTEMS&nbsp;NOMINAL
        </div>

        {/* Subtitle — blinking dots to sell "still doing things, be
            patient". Kept intentionally minimal so the hero text
            breathes. */}
        <div
          className="flex items-center gap-2 font-mono text-xs uppercase tracking-[0.3em] text-[var(--neural-blue)]/80"
          style={{
            animation: "finalize-text-ignite 800ms 200ms forwards",
            opacity: 0,
          }}
        >
          <span>Initializing Command Center</span>
          <span className="inline-flex gap-1">
            <span
              className="h-1 w-1 rounded-full bg-[var(--neural-blue)]"
              style={{ animation: "blink 1.2s ease-in-out infinite" }}
            />
            <span
              className="h-1 w-1 rounded-full bg-[var(--neural-blue)]"
              style={{
                animation: "blink 1.2s ease-in-out 200ms infinite",
              }}
            />
            <span
              className="h-1 w-1 rounded-full bg-[var(--neural-blue)]"
              style={{
                animation: "blink 1.2s ease-in-out 400ms infinite",
              }}
            />
          </span>
        </div>
      </div>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────

export default function SetupRequiredPage() {
  const [status, setStatus] = useState<BootstrapStatusResponse | null>(null)
  const [backendVersion, setBackendVersion] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  // Round 2-B: once the backend reports finalized=true, flip into the
  // cinematic transition state. The FinalizeTransition component
  // self-terminates via an onDone callback after ~2 s, which then
  // performs the actual route swap.
  const [finalizing, setFinalizing] = useState(false)

  const probe = useCallback(async () => {
    setLoading(true)
    setFetchError(null)
    try {
      const [s, h] = await Promise.allSettled([
        getBootstrapStatus(),
        getHealth(),
      ])
      if (s.status === "fulfilled") setStatus(s.value)
      else setFetchError((s.reason as Error)?.message ?? "status probe failed")
      if (h.status === "fulfilled") setBackendVersion(h.value.version)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void probe()
  }, [probe])

  // If the backend has since come out of the bootstrap-required state
  // (operator finished the wizard in another tab, or the agent-hosted
  // smoke path finalized it), play the ~2s finalize cinematic then
  // send them to the app shell. The redirect itself is owned by the
  // FinalizeTransition's onDone callback so the timing never drifts
  // from the animation duration.
  useEffect(() => {
    if (status?.finalized && !finalizing) {
      setFinalizing(true)
    }
  }, [status?.finalized, finalizing])

  const rawJson = useMemo(() => {
    // The 503 body mirrors ``{"error": "bootstrap_required", "status": <gates>, "missing_steps": [...]}``;
    // rehydrating from the status endpoint gives the same shape users
    // see in the DevTools Network tab.
    if (!status) return null
    return JSON.stringify(
      {
        error: "bootstrap_required",
        status: status.status,
        missing_steps: status.missing_steps,
        finalized: status.finalized,
      },
      null,
      2,
    )
  }, [status])

  return (
    <div className="relative min-h-screen flex items-center justify-center overflow-hidden bg-[var(--deep-space-start)]">
      <NeuralGrid />

      {/* Static CRT scan-line grid — inline so we don't depend on the
          existing `.scanlines` ::after being positioned relative to a
          specific panel geometry. */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-0 opacity-40"
        style={{
          background:
            "repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(56,189,248,0.05) 2px, rgba(56,189,248,0.05) 3px)",
        }}
      />

      {/* Animated FUI scan-line sweep — a single bright bar travels
          top→bottom to sell the "probing system gates" feel. */}
      <div aria-hidden="true" className="fui-scan-sweep" />

      <main className="relative z-10 w-full max-w-2xl px-6 py-10">
        <div className="holo-glass-simple corner-brackets rounded-lg p-8 md:p-10">
          {/* Header stripe */}
          <div className="mb-6 flex items-center justify-center gap-3">
            <div className="h-10 w-10 rounded-full border-2 border-[var(--neural-blue)] flex items-center justify-center animate-pulse">
              <Rocket size={18} className="text-[var(--neural-blue)]" />
            </div>
            <span
              className="text-[10px] uppercase tracking-[0.3em] text-[var(--neural-blue)]"
              style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
            >
              SYS.BOOTSTRAP · AWAITING OPERATOR
            </span>
          </div>

          {/* Orbitron Latin display title — Chinese glyphs fall back to
              system fonts because Orbitron only ships a Latin subset, so
              a dedicated English code-name keeps the Orbitron look front
              and center. */}
          <div
            className="mb-2 text-center text-2xl sm:text-3xl md:text-4xl font-bold uppercase tracking-[0.1em] sm:tracking-[0.2em] text-[var(--neural-blue)] text-glow-blue break-words"
            style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
          >
            BOOTSTRAP REQUIRED
          </div>

          {/* Friendly headline */}
          <h1
            className="text-2xl md:text-3xl font-semibold tracking-fui text-[var(--foreground)] text-center"
            style={{ fontFamily: "var(--font-orbitron), sans-serif" }}
          >
            系統需要完成初始設定
          </h1>
          <p className="mt-3 font-mono text-sm text-[var(--muted-foreground)] text-center leading-relaxed">
            歡迎！這是您第一次使用 OmniSight，
            <br />
            只需幾分鐘即可完成基礎配置。
          </p>

          {/* CTA */}
          <div className="mt-8 flex flex-col items-center gap-3">
            <Link
              href="/bootstrap"
              className="group inline-flex items-center gap-2 rounded-md border border-[var(--neural-blue)] bg-[var(--neural-blue-dim)] px-6 py-3 font-mono text-sm font-semibold tracking-widest text-[var(--foreground)] transition-all hover:bg-[var(--neural-blue)] hover:text-black hover:shadow-[0_0_20px_var(--neural-blue-glow)]"
            >
              <span aria-hidden="true">▶</span>
              <span>開始設定</span>
              <ChevronRight
                size={14}
                className="opacity-60 transition-transform group-hover:translate-x-0.5"
              />
            </Link>
            <button
              type="button"
              onClick={() => void probe()}
              disabled={loading}
              className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] disabled:opacity-50"
            >
              {loading ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <RefreshCw size={12} />
              )}
              <span>重新檢測</span>
            </button>
          </div>

          {/* Gate grid */}
          <div className="mt-8 border-t border-[var(--holo-glass-border)] pt-6">
            <div className="mb-3 flex items-center justify-between">
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]">
                INSTALL GATES
              </div>
              <div className="flex items-center gap-3 font-mono text-[9px] uppercase tracking-wider">
                <span className="inline-flex items-center gap-1 text-[var(--neural-blue)]">
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--neural-blue)]" />
                  AUTO
                </span>
                <span className="inline-flex items-center gap-1 text-[var(--artifact-purple)]">
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--artifact-purple)] animate-pulse" />
                  OPERATOR ACTION
                </span>
              </div>
            </div>
            {/* Round 2-A: orbital star-chart replacing the 2×2 grid.
                Pulsing core + 4 gate satellites on a dashed ring + a
                comet tracing the orbit. Purely visual candy — the
                authoritative per-gate semantics live in the hint list
                below so screen readers + low-motion users still get
                everything. */}
            <GateOrbit gates={GATES} status={status} />

            {/* Compact hint list — kept below the orbit so the
                information density from round 1 (AUTO/ACTION badge +
                hint copy) is not lost to eye-candy. Flat layout, one
                row per gate, aligned left. */}
            <ul className="mt-6 flex flex-col gap-1.5">
              {GATES.map((gate) => {
                const green = status ? gate.isGreen(status.status) : false
                const isAction = gate.nature === "action"
                const pending = !green && status !== null
                return (
                  <li
                    key={gate.key}
                    className="flex items-start gap-3 font-mono text-[10px] leading-relaxed"
                  >
                    <span
                      className={`mt-0.5 inline-flex h-4 w-10 items-center justify-center rounded-sm border text-[8px] uppercase tracking-wider ${
                        isAction
                          ? "border-[var(--artifact-purple)]/60 text-[var(--artifact-purple)]"
                          : "border-[var(--neural-blue)]/60 text-[var(--neural-blue)]"
                      }`}
                    >
                      {isAction ? "ACTION" : "AUTO"}
                    </span>
                    <span
                      className={`mt-0.5 inline-flex h-4 w-4 items-center justify-center ${
                        green
                          ? "text-[var(--status-green)]"
                          : isAction && pending
                            ? "text-[var(--artifact-purple)]"
                            : "text-[var(--muted-foreground)]"
                      }`}
                    >
                      {status ? (
                        green ? (
                          <Check size={12} />
                        ) : isAction ? (
                          <ChevronRight size={12} />
                        ) : (
                          <X size={12} />
                        )
                      ) : (
                        <Loader2 size={10} className="animate-spin" />
                      )}
                    </span>
                    <span className="flex-1 text-[var(--muted-foreground)]">
                      <span className="text-[var(--foreground)]">
                        {gate.label}
                      </span>
                      <span className="mx-1.5 text-[var(--muted-foreground)]">
                        ·
                      </span>
                      {gate.hint}
                    </span>
                  </li>
                )
              })}
            </ul>
          </div>

          {/* Technical details disclosure */}
          <div className="mt-6">
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
                      Backend Version
                    </div>
                    <div className="text-[var(--foreground)]">
                      {backendVersion ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-[var(--muted-foreground)]">
                      HTTP Status
                    </div>
                    <div className="text-[var(--critical-red)]">
                      503 Service Unavailable
                    </div>
                  </div>
                </div>

                {fetchError && (
                  <div className="rounded border border-[var(--critical-red)] bg-[var(--critical-red)]/10 px-2 py-1.5 font-mono text-[11px] text-[var(--critical-red)]">
                    {fetchError}
                  </div>
                )}

                <div>
                  <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                    Response body
                  </div>
                  <pre className="max-h-60 overflow-auto rounded border border-[var(--holo-glass-border)] bg-black/60 p-3 font-mono text-[11px] leading-relaxed text-[var(--neural-blue)]">
                    {rawJson ?? "// waiting for /bootstrap/status…"}
                  </pre>
                </div>

                {status?.missing_steps && status.missing_steps.length > 0 && (
                  <div>
                    <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                      Missing steps
                    </div>
                    <ul className="list-disc pl-5 font-mono text-[11px] text-[var(--foreground)]">
                      {status.missing_steps.map((s) => (
                        <li key={s}>{s}</li>
                      ))}
                    </ul>
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

      {/* Spacer so the telemetry ticker never overlaps the footer copy
          on short viewports. Height matches ~2 lines of the ticker
          content plus its vertical padding. */}
      <div aria-hidden="true" className="h-10" />

      <TelemetryTicker />

      {/* Round 2-B: cinematic finalize overlay. Mounted only while
          ``finalizing`` is true so the normal page stays identical
          to round 1 until the backend actually reports finalized. */}
      {finalizing && (
        <FinalizeTransition onDone={() => window.location.replace("/")} />
      )}
    </div>
  )
}
