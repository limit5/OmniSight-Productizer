"use client"

import { useState, useEffect, useCallback } from "react"
import { PanelHelp } from "@/components/omnisight/panel-help"
import {
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
  Unplug
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

// Placeholder — replaced by real data from backend GET /system/info
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

// Empty — replaced by real data from backend GET /system/devices
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
          <Icon size={16} style={{ color: statusColor }} />
        </div>
        
        {/* Device Name */}
        <div className="flex-1 min-w-0">
          <span className="font-mono text-xs text-[var(--foreground)] line-clamp-1" title={device.name}>
            {device.name}
          </span>
        </div>
        
        {/* Status Icon */}
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

function HostInfoSection({ info }: { info: HostInfo }) {
  const memoryPercent = info.memoryTotal > 0 ? Math.round((info.memoryUsed / info.memoryTotal) * 100) : 0
  
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
      
      {/* CPU */}
      <div className="p-2 rounded bg-[var(--secondary)]">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-1.5">
            <Cpu size={12} className="text-[var(--hardware-orange)] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">CPU</span>
          </div>
          <span className="font-mono text-xs font-medium text-[var(--hardware-orange)]">{info.cpuUsage.toFixed(2)}%</span>
        </div>
        <div className="font-mono text-xs text-[var(--foreground)] break-words" title={info.cpuModel}>{info.cpuModel}</div>
        <div className="font-mono text-[10px] text-[var(--muted-foreground)] mt-0.5">{info.cpuCores}C / {info.arch}</div>
        <div className="mt-2 h-1 rounded-full bg-[var(--border)] overflow-hidden">
          <div 
            className="h-full rounded-full bg-[var(--hardware-orange)] transition-all duration-500"
            style={{ width: `${info.cpuUsage}%` }}
          />
        </div>
      </div>
      
      {/* Memory */}
      <div className="p-2 rounded bg-[var(--secondary)]">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-1.5">
            <MemoryStick size={12} className="text-[var(--artifact-purple)] shrink-0" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">Memory</span>
          </div>
          <span className="font-mono text-xs font-medium text-[var(--artifact-purple)]">{memoryPercent}%</span>
        </div>
        <div className="font-mono text-xs text-[var(--foreground)]">
          {(info.memoryUsed / 1024).toFixed(1)} GB / {(info.memoryTotal / 1024).toFixed(0)} GB
        </div>
        <div className="mt-2 h-1 rounded-full bg-[var(--border)] overflow-hidden">
          <div 
            className="h-full rounded-full bg-[var(--artifact-purple)] transition-all duration-500"
            style={{ width: `${memoryPercent}%` }}
          />
        </div>
      </div>
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
        </div>
      </div>
      
      {/* Scrollable Content */}
      <div className="flex-1 overflow-auto pr-2 space-y-4">
        {/* Host Info */}
        <div className="holo-glass-simple rounded p-3">
          <div className="flex items-center gap-2 mb-3">
            <Monitor size={14} className="text-[var(--neural-blue)]" />
            <span className="font-mono text-xs text-[var(--neural-blue)]">SYSTEM INFO</span>
          </div>
          <HostInfoSection info={hostData} />
        </div>
        
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
