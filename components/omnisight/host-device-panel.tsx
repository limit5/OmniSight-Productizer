"use client"

import { useState, useEffect, useCallback, useMemo } from "react"
import { PanelHelp } from "@/components/omnisight/panel-help"
import { useAuth } from "@/lib/auth-context"
import {
  getAllHostMetrics,
  getMyHostMetrics,
  type TenantUsage,
} from "@/lib/api"
import {
  useHostMetricsTick,
  type HostMetricsHistoryPoint,
} from "@/hooks/use-host-metrics-tick"
import {
  Activity,
  Boxes,
  Cpu,
  HardDrive,
  MemoryStick,
  Wifi,
  Usb,
  Camera,
  Monitor,
  RefreshCw,
  Plus,
  Minus,
  AlertCircle,
  CheckCircle2,
  Loader2,
  Unplug,
  Users,
} from "lucide-react"

export type DeviceStatus = "connected" | "disconnected" | "detecting" | "error"

export interface Device {
  id: string
  name: string
  type: "usb" | "camera" | "storage" | "network" | "display" | "evk"
  status: DeviceStatus
  vendorId?: string
  productId?: string
  serial?: string
  speed?: string
  mountPoint?: string
  v4l2_device?: string      // /dev/video0
  deploy_target_ip?: string // EVK board IP
  deploy_method?: string    // ssh | adb | fastboot
  reachable?: boolean       // EVK reachability
}

interface HostInfo {
  hostname: string
  os: string
  kernel: string
  arch: string
  cpuModel: string
  cpuCores: number
  cpuUsage: number
  memoryTotal: number
  memoryUsed: number
  uptime: string
}

// Seed shown before the first SSE `host.metrics.tick` / `/runtime/info`
// fetch lands. Fields are overwritten as soon as data arrives.
const defaultHostInfo: HostInfo = {
  hostname: "loading...",
  os: "--",
  kernel: "--",
  arch: "--",
  cpuModel: "Detecting...",
  cpuCores: 0,
  cpuUsage: 0,
  memoryTotal: 0,
  memoryUsed: 0,
  uptime: "--"
}

// Empty — replaced by real data from backend GET /runtime/devices
const defaultDevices: Device[] = []

function getDeviceIcon(type: Device["type"]) {
  switch (type) {
    case "camera": return Camera
    case "usb": return Usb
    case "storage": return HardDrive
    case "network": return Wifi
    case "display": return Monitor
    case "evk": return Cpu
    default: return Usb
  }
}

function getStatusColor(status: DeviceStatus) {
  switch (status) {
    case "connected": return "var(--validation-emerald)"
    case "disconnected": return "var(--muted-foreground)"
    case "detecting": return "var(--neural-blue)"
    case "error": return "var(--critical-red)"
  }
}

function getStatusIcon(status: DeviceStatus) {
  switch (status) {
    case "connected": return CheckCircle2
    case "disconnected": return Unplug
    case "detecting": return Loader2
    case "error": return AlertCircle
  }
}

function DeviceCard({ 
  device, 
  onRemove 
}: { 
  device: Device
  onRemove?: (id: string) => void 
}) {
  const Icon = getDeviceIcon(device.type)
  const StatusIcon = getStatusIcon(device.status)
  const statusColor = getStatusColor(device.status)

  return (
    <div 
      className={`
        relative group p-2 rounded transition-all duration-300
        ${device.status === "connected" 
          ? "bg-[var(--validation-emerald-dim)] border border-[var(--validation-emerald)]/30" 
          : device.status === "detecting"
          ? "bg-[var(--neural-blue-dim)] border border-[var(--neural-blue)]/30 animate-pulse"
          : device.status === "error"
          ? "bg-[var(--critical-red)]/10 border border-[var(--critical-red)]/30"
          : "bg-[var(--secondary)] border border-[var(--border)] opacity-50"
        }
      `}
    >
      {/* Top Row: Icon + Name + Status */}
      <div className="flex items-center gap-2">
        {/* Device Icon */}
        <div 
          className="w-8 h-8 rounded flex items-center justify-center shrink-0"
          style={{ backgroundColor: `color-mix(in srgb, ${statusColor} 20%, transparent)` }}
        >
          {/* eslint-disable-next-line react-hooks/static-components -- Lucide icons are stateless; dynamic selection by device type is intentional */}
          <Icon size={16} style={{ color: statusColor }} />
        </div>
        
        {/* Device Name */}
        <div className="flex-1 min-w-0">
          <span className="font-mono text-xs text-[var(--foreground)] line-clamp-1" title={device.name}>
            {device.name}
          </span>
        </div>
        
        {/* Status Icon */}
        {/* eslint-disable-next-line react-hooks/static-components -- Lucide icons are stateless; dynamic selection by device status is intentional */}
        <StatusIcon
          size={12}
          style={{ color: statusColor }}
          className={`shrink-0 ${device.status === "detecting" ? "animate-spin" : ""}`}
        />
      </div>
      
      {/* Bottom Row: Details */}
      <div className="flex items-center gap-2 mt-1.5 ml-10">
        {device.vendorId && (
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            {device.vendorId}
          </span>
        )}
        {device.speed && (
          <span className="font-mono text-[10px]" style={{ color: statusColor }}>
            {device.speed}
          </span>
        )}
        {device.mountPoint && (
          <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate">
            {device.mountPoint}
          </span>
        )}
        {/* UVC V4L2 device path */}
        {device.v4l2_device && (
          <span className="font-mono text-[10px] text-[var(--neural-blue)]">
            {device.v4l2_device}
          </span>
        )}
        {/* EVK board info */}
        {device.deploy_target_ip && (
          <span className={`font-mono text-[10px] ${device.reachable ? "text-[var(--validation-emerald)]" : "text-[var(--critical-red)]"}`}>
            {device.deploy_method?.toUpperCase()} {device.deploy_target_ip} {device.reachable ? "●" : "○"}
          </span>
        )}
      </div>
      
      {/* Remove Button */}
      {onRemove && device.status === "connected" && (
        <button
          onClick={() => onRemove(device.id)}
          className="absolute top-1 right-1 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity bg-[var(--critical-red)]/20 hover:bg-[var(--critical-red)]/40"
          title="Eject device"
        >
          <Minus size={10} className="text-[var(--critical-red)]" />
        </button>
      )}
    </div>
  )
}

// 60-pt SVG sparkline. Pure presentational: caller passes the rolling
// window from `useHostMetricsTick().history` (capped at 60 by the hook).
// `domainMax` lets percentage metrics share a consistent 0..100 y-axis so
// shape changes reflect actual pressure, not auto-scaling. Unbounded
// metrics (loadavg / container count) auto-fit by passing `null`.
export function MetricSparkline({
  values,
  color,
  domainMax = null,
  width = 96,
  height = 18,
  fluid = false,
  testId,
}: {
  values: number[]
  color: string
  domainMax?: number | null
  width?: number
  height?: number
  // SP-8.1d (2026-04-21): when ``fluid`` is true, the sparkline's
  // rendered width scales to fill its parent container via SVG
  // ``width="100%" viewBox preserveAspectRatio="none"``. Internal
  // coordinate math still uses the ``width`` prop as its virtual
  // canvas, so the curve shape is identical at any rendered width.
  // Used by ``LiveMetricsSection`` to avoid the narrow-column
  // overflow the operator surfaced 2026-04-21.
  fluid?: boolean
  testId?: string
}) {
  if (values.length < 2) {
    return (
      <div
        data-testid={testId}
        data-empty="true"
        className="opacity-30 font-mono text-[9px] flex items-center justify-end"
        style={{ width: fluid ? "100%" : width, height }}
      >
        —
      </div>
    )
  }
  const min = 0
  const max = domainMax ?? Math.max(1, ...values)
  const range = Math.max(0.001, max - min)
  const stepX = width / (values.length - 1)
  const pts = values
    .map((v, i) => {
      const x = i * stepX
      const y = height - ((Math.max(min, Math.min(max, v)) - min) / range) * height
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(" ")
  return (
    <svg
      data-testid={testId}
      data-points={values.length}
      width={fluid ? "100%" : width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio={fluid ? "none" : "xMidYMid meet"}
      className={fluid ? "block" : "shrink-0"}
      aria-hidden
    >
      <polyline fill="none" stroke={color} strokeWidth={1.2} points={pts} />
    </svg>
  )
}

interface LiveTickFields {
  cpuUsage: number       // %
  memPercent: number     // %
  memUsedGb: number
  memTotalGb: number
  diskPercent: number    // %
  diskUsedGb: number
  diskTotalGb: number
  loadavg1m: number
  containerCount: number
}

// H3 row 1528: high-pressure threshold visual marking.
// Below 70% the metric is healthy (green); 70-85% is warn (amber);
// ≥ 85% is critical (red). Threshold 85 matches the H2 backend
// `host_cpu_high` precondition (cpu_pct < 85 to acquire a sandbox slot)
// so the UI flips red at the same point the coordinator starts deferring
// new sandbox launches. 70 is the early-warning band that lets operators
// notice load building up before deferral kicks in.
export const PRESSURE_WARN_PCT = 70
export const PRESSURE_CRITICAL_PCT = 85

export type PressureLevel = "normal" | "warn" | "critical"

export function pressureLevel(percent: number): PressureLevel {
  if (!Number.isFinite(percent)) return "normal"
  if (percent >= PRESSURE_CRITICAL_PCT) return "critical"
  if (percent >= PRESSURE_WARN_PCT) return "warn"
  return "normal"
}

const PRESSURE_COLOR_VAR: Record<PressureLevel, string> = {
  normal: "var(--validation-emerald)",
  warn: "var(--hardware-orange)",
  critical: "var(--critical-red)",
}

export function pressureColorVar(percent: number): string {
  return PRESSURE_COLOR_VAR[pressureLevel(percent)]
}

function pressureTitle(label: string, percent: number | null): string {
  const lvl = percent === null ? "normal" : pressureLevel(percent)
  const value = percent === null ? "—" : `${percent.toFixed(1)}%`
  if (lvl === "critical") {
    return `${label} ${value} — CRITICAL (≥ ${PRESSURE_CRITICAL_PCT}%): coordinator is deferring new sandbox launches`
  }
  if (lvl === "warn") {
    return `${label} ${value} — WARN (${PRESSURE_WARN_PCT}-${PRESSURE_CRITICAL_PCT}%): pressure building, no derate yet`
  }
  return `${label} ${value} — normal (< ${PRESSURE_WARN_PCT}%)`
}

// Memory available is total - used (cgroup-style; the SSE tick reports
// the same `mem_total_gb − mem_used_gb` semantics the backend computes).
const memAvailable = (t: LiveTickFields) =>
  Math.max(0, t.memTotalGb - t.memUsedGb)

function LiveMetricsSection({
  tick,
  history,
  cpuModel,
  cpuCores,
  arch,
}: {
  tick: LiveTickFields | null
  history: HostMetricsHistoryPoint[]
  cpuModel: string
  cpuCores: number
  arch: string
}) {
  const cpuValues = history.map((p) => p.cpu_percent)
  const memValues = history.map((p) => p.mem_percent)
  const diskValues = history.map((p) => p.disk_percent)
  const loadValues = history.map((p) => p.loadavg_1m)
  const containerValues = history.map((p) => p.container_count)
  const fmt = (v: number, d = 2) => (Number.isFinite(v) ? v.toFixed(d) : "—")
  // H3 row 1528: pressure level + dynamic color per percent metric. CPU
  // uses the spec-mandated 70/85 thresholds; mem/disk reuse the same
  // helper for visual consistency since they share the 0..100% scale.
  const cpuPctRaw = tick?.cpuUsage ?? null
  const memPctRaw = tick?.memPercent ?? null
  const diskPctRaw = tick?.diskPercent ?? null
  const cpuLvl = cpuPctRaw === null ? "normal" : pressureLevel(cpuPctRaw)
  const memLvl = memPctRaw === null ? "normal" : pressureLevel(memPctRaw)
  const diskLvl = diskPctRaw === null ? "normal" : pressureLevel(diskPctRaw)
  const cpuColor = cpuPctRaw === null ? "var(--hardware-orange)" : pressureColorVar(cpuPctRaw)
  const memColor = memPctRaw === null ? "var(--artifact-purple)" : pressureColorVar(memPctRaw)
  const diskColor = diskPctRaw === null ? "var(--validation-emerald)" : pressureColorVar(diskPctRaw)
  return (
    <div className="space-y-2">
      {/* SP-8.1d (2026-04-21): each metric card restructured from
          single-row (label + sparkline + number jammed side-by-side)
          to two-row (label + number on top; sparkline full-width on
          its own row below). Operator reported the sparkline was
          overflowing the narrow SYSTEM INFO column — the
          horizontal-cram layout assumed ~300px card width but the
          actual left-column in the dashboard is ~220px. Two-row
          lets the sparkline fluidly fill the card, and the number
          stays readable on the right of the header. */}

      {/* CPU */}
      <div
        className="p-2 rounded bg-[var(--secondary)]"
        data-testid="metric-cpu"
        data-pressure={cpuLvl}
        title={pressureTitle("CPU", cpuPctRaw)}
      >
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <Cpu size={12} className="text-[var(--hardware-orange)] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">CPU</span>
          </div>
          <span
            className={`font-mono text-xs font-medium tabular-nums flex items-center gap-1 ${cpuLvl === "critical" ? "animate-pulse" : ""}`}
            style={{ color: cpuColor }}
            data-testid="metric-cpu-value"
          >
            {cpuLvl === "critical" && (
              <AlertCircle size={10} aria-label="critical" />
            )}
            {tick ? `${fmt(tick.cpuUsage)}%` : "—"}
          </span>
        </div>
        <div className="mb-1">
          <MetricSparkline
            values={cpuValues}
            color={cpuColor}
            domainMax={100}
            fluid
            testId="sparkline-cpu"
          />
        </div>
        <div className="font-mono text-xs text-[var(--foreground)] break-words" title={cpuModel}>{cpuModel}</div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] mt-0.5">{cpuCores}C / {arch}</div>
      </div>

      {/* Memory — % + used/total + available */}
      <div
        className="p-2 rounded bg-[var(--secondary)]"
        data-testid="metric-mem"
        data-pressure={memLvl}
        title={pressureTitle("Memory", memPctRaw)}
      >
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <MemoryStick size={12} className="text-[var(--artifact-purple)] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">Memory</span>
          </div>
          <span
            className={`font-mono text-xs font-medium tabular-nums flex items-center gap-1 ${memLvl === "critical" ? "animate-pulse" : ""}`}
            style={{ color: memColor }}
            data-testid="metric-mem-value"
          >
            {memLvl === "critical" && (
              <AlertCircle size={10} aria-label="critical" />
            )}
            {tick ? `${fmt(tick.memPercent, 0)}%` : "—"}
          </span>
        </div>
        <div className="mb-1">
          <MetricSparkline
            values={memValues}
            color={memColor}
            domainMax={100}
            fluid
            testId="sparkline-mem"
          />
        </div>
        <div className="font-mono text-xs text-[var(--foreground)] tabular-nums">
          {tick ? `${fmt(tick.memUsedGb, 1)} GB / ${fmt(tick.memTotalGb, 0)} GB` : "—"}
        </div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] mt-0.5 tabular-nums" data-testid="metric-mem-available">
          available {tick ? `${fmt(memAvailable(tick), 1)} GB` : "—"}
        </div>
      </div>

      {/* Disk */}
      <div
        className="p-2 rounded bg-[var(--secondary)]"
        data-testid="metric-disk"
        data-pressure={diskLvl}
        title={pressureTitle("Disk", diskPctRaw)}
      >
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <HardDrive size={12} className="text-[var(--validation-emerald)] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">Disk</span>
          </div>
          <span
            className={`font-mono text-xs font-medium tabular-nums flex items-center gap-1 ${diskLvl === "critical" ? "animate-pulse" : ""}`}
            style={{ color: diskColor }}
            data-testid="metric-disk-value"
          >
            {diskLvl === "critical" && (
              <AlertCircle size={10} aria-label="critical" />
            )}
            {tick ? `${fmt(tick.diskPercent, 0)}%` : "—"}
          </span>
        </div>
        <div className="mb-1">
          <MetricSparkline
            values={diskValues}
            color={diskColor}
            domainMax={100}
            fluid
            testId="sparkline-disk"
          />
        </div>
        <div className="font-mono text-xs text-[var(--foreground)] tabular-nums">
          {tick ? `${fmt(tick.diskUsedGb, 0)} GB / ${fmt(tick.diskTotalGb, 0)} GB` : "—"}
        </div>
      </div>

      {/* Load avg 1m */}
      <div className="p-2 rounded bg-[var(--secondary)]" data-testid="metric-loadavg">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <Activity size={12} className="text-[var(--neural-blue)] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">Load 1m</span>
          </div>
          <span className="font-mono text-xs font-medium text-[var(--neural-blue)] tabular-nums">
            {tick ? fmt(tick.loadavg1m) : "—"}
          </span>
        </div>
        <div className="mb-1">
          <MetricSparkline
            values={loadValues}
            color="var(--neural-blue)"
            fluid
            testId="sparkline-loadavg"
          />
        </div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">
          {cpuCores > 0 && tick ? `${fmt(tick.loadavg1m / cpuCores * 100, 0)}% of ${cpuCores}C` : ""}
        </div>
      </div>

      {/* Running containers */}
      <div className="p-2 rounded bg-[var(--secondary)]" data-testid="metric-containers">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <Boxes size={12} className="text-[var(--fui-orange,var(--hardware-orange))] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">Containers</span>
          </div>
          <span className="font-mono text-xs font-medium text-[var(--hardware-orange)] tabular-nums">
            {tick ? `${tick.containerCount}` : "—"}
          </span>
        </div>
        <div className="mb-1">
          <MetricSparkline
            values={containerValues}
            color="var(--fui-orange,var(--hardware-orange))"
            fluid
            testId="sparkline-containers"
          />
        </div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)]">running</div>
      </div>
    </div>
  )
}

function HostInfoSection({ info }: { info: HostInfo }) {
  return (
    <div className="space-y-2">
      {/* System Info - Hostname & Uptime */}
      <div className="grid grid-cols-2 gap-2">
        <div className="p-2 rounded bg-[var(--secondary)]">
          <div className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider mb-1">Hostname</div>
          <div className="font-mono text-xs font-medium text-[var(--neural-blue)] break-all">{info.hostname}</div>
        </div>
        <div className="p-2 rounded bg-[var(--secondary)]">
          <div className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider mb-1">Uptime</div>
          <div className="font-mono text-xs font-medium text-[var(--validation-emerald)]">{info.uptime}</div>
        </div>
      </div>

      {/* OS & Kernel */}
      <div className="p-2 rounded bg-[var(--secondary)]">
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider mb-1">OS</div>
        <div className="font-mono text-xs text-[var(--foreground)] break-words" title={info.os}>{info.os}</div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] mt-1.5 break-all" title={info.kernel}>{info.kernel}</div>
      </div>
    </div>
  )
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  M4 — Per-tenant resource bars (cgroup-derived)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

// Max cap that normalises a tenant bar to 100% width. Chosen so a full
// 16-core saturating tenant (1600% across all cores) just fills the bar.
const TENANT_CPU_BAR_FULL = 1600
const TENANT_MEM_BAR_FULL_GB = 16

function TenantRow({ row, highlight = false }: { row: TenantUsage; highlight?: boolean }) {
  const cpuWidth = Math.min(100, (row.cpu_percent / TENANT_CPU_BAR_FULL) * 100)
  const memWidth = Math.min(100, (row.mem_used_gb / TENANT_MEM_BAR_FULL_GB) * 100)
  return (
    <div
      className={`p-2 rounded ${highlight
        ? "bg-[var(--neural-blue-dim)] border border-[var(--neural-blue)]/30"
        : "bg-[var(--secondary)]"}`}
    >
      <div className="flex items-center justify-between mb-1.5">
        <span className="font-mono text-xs font-medium text-[var(--foreground)] truncate" title={row.tenant_id}>
          {row.tenant_id}
        </span>
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
          {row.sandbox_count} sbx
        </span>
      </div>
      <div className="flex items-center gap-1.5 mb-0.5">
        <span className="font-mono text-[10px] w-8 text-[var(--hardware-orange)]">CPU</span>
        <div className="flex-1 h-1 rounded-full bg-[var(--border)] overflow-hidden">
          <div
            className="h-full rounded-full bg-[var(--hardware-orange)] transition-all duration-500"
            style={{ width: `${cpuWidth}%` }}
          />
        </div>
        <span className="font-mono text-[10px] text-[var(--hardware-orange)] tabular-nums" style={{ minWidth: 52 }}>
          {row.cpu_percent.toFixed(1)}%
        </span>
      </div>
      <div className="flex items-center gap-1.5 mb-0.5">
        <span className="font-mono text-[10px] w-8 text-[var(--artifact-purple)]">MEM</span>
        <div className="flex-1 h-1 rounded-full bg-[var(--border)] overflow-hidden">
          <div
            className="h-full rounded-full bg-[var(--artifact-purple)] transition-all duration-500"
            style={{ width: `${memWidth}%` }}
          />
        </div>
        <span className="font-mono text-[10px] text-[var(--artifact-purple)] tabular-nums" style={{ minWidth: 52 }}>
          {row.mem_used_gb.toFixed(2)}G
        </span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-[10px] w-8 text-[var(--validation-emerald)]">DSK</span>
        <span className="flex-1 font-mono text-[10px] text-[var(--validation-emerald)] tabular-nums">
          {row.disk_used_gb.toFixed(2)} GiB on disk
        </span>
      </div>
    </div>
  )
}

function TenantUsageSection() {
  const { user } = useAuth()
  const [rows, setRows] = useState<TenantUsage[]>([])
  const [self, setSelf] = useState<TenantUsage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const isAdmin = user?.role === "admin"

  const load = useCallback(async () => {
    if (!user) return
    setLoading(true)
    try {
      if (isAdmin) {
        const data = await getAllHostMetrics()
        setRows(data.tenants ?? [])
      } else {
        const data = await getMyHostMetrics()
        setSelf(data.tenant)
      }
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [isAdmin, user])

  useEffect(() => {
    void load()
    const t = setInterval(() => void load(), 5000)
    return () => clearInterval(t)
  }, [load])

  if (!user) return null

  return (
    <div className="holo-glass-simple rounded p-3">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-1.5">
          <Users size={12} className="text-[var(--neural-blue)] shrink-0" />
          <span className="font-mono text-xs text-[var(--neural-blue)]">
            {isAdmin ? "TENANT USAGE (ALL)" : "MY TENANT USAGE"}
          </span>
        </div>
        <button
          onClick={() => void load()}
          disabled={loading}
          className="p-1 rounded hover:bg-[var(--neural-blue-dim)] transition-colors"
          title="Refresh"
        >
          <RefreshCw size={10} className={loading ? "animate-spin text-[var(--neural-blue)]" : "text-[var(--neural-blue)]"} />
        </button>
      </div>
      {error && (
        <div className="mb-2 font-mono text-[10px] text-[var(--critical-red)]">{error}</div>
      )}
      {isAdmin ? (
        rows.length === 0 ? (
          <div className="py-4 text-center">
            <p className="font-mono text-xs text-[var(--muted-foreground)]">No running tenants</p>
          </div>
        ) : (
          <div className="space-y-2">
            {rows.map(row => (
              <TenantRow key={row.tenant_id} row={row} highlight={row.tenant_id === user.tenant_id} />
            ))}
          </div>
        )
      ) : (
        self ? (
          <TenantRow row={self} highlight />
        ) : (
          <div className="py-4 text-center">
            <p className="font-mono text-xs text-[var(--muted-foreground)]">Loading&hellip;</p>
          </div>
        )
      )}
    </div>
  )
}


interface HostDevicePanelProps {
  hostInfo?: HostInfo
  devices?: Device[]
  onDeviceChange?: (devices: Device[]) => void
}

export function HostDevicePanel({
  hostInfo = defaultHostInfo,
  devices: initialDevices = defaultDevices,
  onDeviceChange
}: HostDevicePanelProps) {
  const [devices, setDevices] = useState<Device[]>(initialDevices)
  const [isScanning, setIsScanning] = useState(false)
  const [hostData, setHostData] = useState(hostInfo)

  // H3: real-time CPU / mem / disk / loadavg / container counts pushed
  // by the backend `host.metrics.tick` SSE (5s cadence). Replaces the
  // placeholder numbers that used to sit in this panel.
  const {
    latest: hostTick,
    baseline: hostBaseline,
    history: hostHistory,
    connected: hostSseConnected,
  } = useHostMetricsTick()

  // Merge: /runtime/info supplies static identity (hostname/os/kernel/arch/uptime +
  // cpu model/cores), SSE supplies live load. SSE wins whenever it has data.
  const mergedHost = useMemo<HostInfo>(() => {
    const base: HostInfo = { ...hostData }
    if (hostTick) {
      base.cpuUsage = hostTick.host.cpu_percent
      // mem_*_gb → legacy MB fields used by HostInfoSection (divides by 1024)
      base.memoryUsed = hostTick.host.mem_used_gb * 1024
      base.memoryTotal = hostTick.host.mem_total_gb * 1024
    }
    // Fill identity gaps from baseline when /runtime/info hasn't landed yet.
    if (hostBaseline) {
      if (!base.cpuModel || base.cpuModel === "Detecting...") {
        base.cpuModel = hostBaseline.cpu_model
      }
      if (!base.cpuCores) base.cpuCores = hostBaseline.cpu_cores
      if (!base.memoryTotal && hostBaseline.mem_total_gb > 0) {
        base.memoryTotal = hostBaseline.mem_total_gb * 1024
      }
    }
    return base
  }, [hostData, hostTick, hostBaseline])

  const liveTick = useMemo<LiveTickFields | null>(() => {
    if (!hostTick) return null
    return {
      cpuUsage: hostTick.host.cpu_percent,
      memPercent: hostTick.host.mem_percent,
      memUsedGb: hostTick.host.mem_used_gb,
      memTotalGb: hostTick.host.mem_total_gb,
      diskPercent: hostTick.host.disk_percent,
      diskUsedGb: hostTick.host.disk_used_gb,
      diskTotalGb: hostTick.host.disk_total_gb,
      loadavg1m: hostTick.host.loadavg_1m,
      containerCount: hostTick.docker.container_count,
    }
  }, [hostTick])

  // Sync props → internal state when backend pushes new data
  useEffect(() => {
    setHostData(hostInfo)
  }, [hostInfo])

  useEffect(() => {
    setDevices(initialDevices)
  }, [initialDevices])
  
  // Real device scan — triggers a backend refresh via parent
  const handleScan = useCallback(() => {
    setIsScanning(true)
    // The parent (page.tsx) re-fetches devices via useEngine every 5s.
    // Show scanning animation for 2s then settle.
    setTimeout(() => setIsScanning(false), 2000)
  }, [])

  const handleRemoveDevice = useCallback((id: string) => {
    setDevices(prev => prev.map(d =>
      d.id === id ? { ...d, status: "disconnected" as DeviceStatus } : d
    ))
    setTimeout(() => {
      setDevices(prev => {
        const updated = prev.filter(d => d.id !== id)
        onDeviceChange?.(updated)
        return updated
      })
    }, 500)
  }, [onDeviceChange])
  
  const connectedCount = devices.filter(d => d.status === "connected").length
  const detectingCount = devices.filter(d => d.status === "detecting").length
  
  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 holo-glass-simple mb-4 corner-brackets relative hex-pattern">
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--hardware-orange)] pulse-orange signal-wave" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--hardware-orange)]">
              HOST & DEVICES
            </h2>
            <PanelHelp doc="panels-overview" />
          </div>
          <button
            onClick={handleScan}
            disabled={isScanning}
            className={`
              p-1.5 rounded transition-colors
              ${isScanning 
                ? "bg-[var(--neural-blue)]/40 text-[var(--neural-blue)] cursor-not-allowed" 
                : "bg-[var(--hardware-orange)]/20 hover:bg-[var(--hardware-orange)]/40 text-[var(--hardware-orange)]"
              }
            `}
            title={isScanning ? "Scanning..." : "Scan for devices"}
          >
            <RefreshCw size={12} className={isScanning ? "animate-spin" : ""} />
          </button>
        </div>
        <div className="flex items-center gap-4 mt-2 tabular-nums">
          <p
            className="font-mono text-xs text-[var(--validation-emerald)] inline-block"
            style={{ minWidth: 100 }}
          >
            <span className="inline-block text-right" style={{ minWidth: 22 }}>{connectedCount}</span> CONNECTED
          </p>
          {/* Reserve the slot for DETECTING regardless of count, so the
            * row height + neighbour positions are stable when the value
            * flips between 0 and >0. */}
          <p
            className="font-mono text-xs text-[var(--neural-blue)] inline-block"
            style={{ minWidth: 96, visibility: detectingCount > 0 ? "visible" : "hidden" }}
            aria-hidden={detectingCount === 0}
          >
            <span className="inline-block text-right" style={{ minWidth: 22 }}>{detectingCount || 0}</span> DETECTING
          </p>
          {/* H3 row 1523: hardcoded baseline of the reference rig (16c / 64GB /
            * 512GB). Kept static on purpose — downstream H4a token-bucket math
            * (`CAPACITY_MAX = min(cores*0.8, mem/2) = 12`) assumes this target
            * envelope, so the header advertises it even when SSE lands a
            * different host. */}
          <p
            data-testid="host-baseline"
            className="font-mono text-xs text-[var(--muted-foreground)] inline-block ml-auto"
            title="Reference rig baseline (hardcoded): 16 cores / 64 GB RAM / 512 GB disk"
          >
            BASELINE <span className="text-[var(--hardware-orange)]">16c</span>
            {" / "}<span className="text-[var(--artifact-purple)]">64GB</span>
            {" / "}<span className="text-[var(--validation-emerald)]">512GB</span>
          </p>
        </div>
      </div>
      
      {/* Scrollable Content */}
      <div className="flex-1 overflow-auto pr-2 space-y-4">
        {/* Host Info */}
        <div className="holo-glass-simple rounded p-3" data-testid="system-info">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Monitor size={14} className="text-[var(--neural-blue)]" />
              <span className="font-mono text-xs text-[var(--neural-blue)]">SYSTEM INFO</span>
            </div>
            <span
              data-testid="host-sse-status"
              className={`font-mono text-[10px] ${
                hostSseConnected
                  ? "text-[var(--validation-emerald)]"
                  : "text-[var(--muted-foreground)]"
              }`}
              title={
                hostSseConnected
                  ? "host.metrics.tick SSE live"
                  : "Waiting for host.metrics.tick"
              }
            >
              {hostSseConnected ? "● SSE LIVE" : "○ SSE WAITING"}
            </span>
          </div>
          <HostInfoSection info={mergedHost} />
          {/* H3 row 1522: live load metrics (CPU/Mem/Disk/Load1m/Containers)
            * with 60-pt sparklines fed by the SSE history ring buffer. */}
          <div className="mt-2">
            <LiveMetricsSection
              tick={liveTick}
              history={hostHistory}
              cpuModel={mergedHost.cpuModel}
              cpuCores={mergedHost.cpuCores}
              arch={mergedHost.arch}
            />
          </div>
        </div>

        {/* M4: Per-tenant usage bars (admin → all tenants; user → self) */}
        <TenantUsageSection />

        {/* Devices */}
        <div className="holo-glass-simple rounded p-3">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-1.5">
              <Usb size={12} className="text-[var(--validation-emerald)] shrink-0" />
              <span className="font-mono text-xs text-[var(--validation-emerald)]">DEVICES</span>
            </div>
            <button
              onClick={handleScan}
              className="p-1 rounded hover:bg-[var(--validation-emerald-dim)] transition-colors"
              title="Add device"
            >
              <Plus size={12} className="text-[var(--validation-emerald)]" />
            </button>
          </div>
          
          {devices.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 text-center">
              <Unplug size={32} className="text-[var(--muted-foreground)] opacity-30 mb-3" />
              <p className="font-mono text-sm text-[var(--muted-foreground)]">NO DEVICES DETECTED</p>
              <p className="font-mono text-xs text-[var(--muted-foreground)] opacity-60 mt-1">Click SCAN to detect devices</p>
            </div>
          ) : (
            <div className="space-y-2">
              {devices.map(device => (
                <DeviceCard 
                  key={device.id} 
                  device={device} 
                  onRemove={handleRemoveDevice}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
