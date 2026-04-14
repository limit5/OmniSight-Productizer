"use client"

import { useState, useEffect, useCallback } from "react"
import { FileText, Download, ExternalLink, Camera, Radio, ChevronDown, Signal, Wifi, WifiOff, Plus, X, Grid2X2, Grid3X3, Maximize2, Minimize2, Cpu, Play, ToggleLeft, ToggleRight } from "lucide-react"
import type { SimulationItem } from "@/lib/api"
import { getArtifactDownloadUrl } from "@/lib/api"
import { PanelHelp } from "@/components/omnisight/panel-help"

export type StreamType = "uvc" | "rtsp"

export interface StreamSource {
  id: string
  name: string
  type: StreamType
  url?: string // RTSP URL for network streams
  deviceId?: string // Device ID for UVC devices
  status: "online" | "offline" | "connecting"
}

interface VitalsData {
  fps: number
  fpsTarget: number
  latency: number
  bitrate: number
  resolution: string
  encoding: string
  protocol?: string
}

interface Artifact {
  id: string
  name: string
  type: "pdf" | "markdown" | "json" | "log" | "html" | "binary" | "firmware" | "kernel_module" | "sdk" | "model" | "archive"
  timestamp: string
  size: number | string
  checksum?: string
  version?: string
}

interface LogEntry {
  timestamp: string
  message: string
  level: "info" | "warn" | "error"
}

// Defaults used only when backend is not connected (empty = no streams detected)
const emptyVitals: VitalsData = {
  fps: 0,
  fpsTarget: 0,
  latency: 0,
  bitrate: 0,
  resolution: "--",
  encoding: "--",
  protocol: "--"
}

const noStreamSources: StreamSource[] = []
const noArtifacts: Artifact[] = []
const noLogs: LogEntry[] = [
  { timestamp: "--:--:--", message: "Awaiting backend connection...", level: "info" }
]

interface StreamPreviewProps {
  vitals: VitalsData
  sources?: StreamSource[]
  selectedSourceId?: string
  onSourceChange?: (sourceId: string) => void
}

function StreamPreview({
  vitals,
  sources = noStreamSources,
  selectedSourceId,
  onSourceChange
}: StreamPreviewProps) {
  const [frame, setFrame] = useState(0)
  const [timestamp, setTimestamp] = useState("--:--:--.---")
  const [dropdownOpen, setDropdownOpen] = useState(false)
  
  const selectedSource = sources.find(s => s.id === selectedSourceId) || sources.find(s => s.status === "online") || sources[0]
  
  useEffect(() => {
    const interval = setInterval(() => {
      setFrame(f => (f + 1) % 100)
      setTimestamp(new Date().toISOString().slice(11, 23))
    }, 33)
    return () => clearInterval(interval)
  }, [])

  const fpsHealth = vitals.fps >= vitals.fpsTarget * 0.95 ? "good" : vitals.fps >= vitals.fpsTarget * 0.8 ? "warning" : "critical"
  const isRTSP = selectedSource?.type === "rtsp"

  return (
    <div className="holo-glass-simple rounded overflow-hidden">
      {/* Preview Header with Source Selector */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border)]">
        <div className="flex items-center gap-2">
          {selectedSource?.status === "online" ? (
            <>
              <div className="w-2 h-2 rounded-full bg-[var(--critical-red)] live-indicator" />
              <span className="font-mono text-xs text-[var(--critical-red)]">LIVE</span>
            </>
          ) : selectedSource?.status === "connecting" ? (
            <>
              <div className="w-2 h-2 rounded-full bg-[var(--hardware-orange)] pulse-orange" />
              <span className="font-mono text-xs text-[var(--hardware-orange)]">CONNECTING</span>
            </>
          ) : (
            <>
              <div className="w-2 h-2 rounded-full bg-[var(--muted-foreground)]" />
              <span className="font-mono text-xs text-[var(--muted-foreground)]">OFFLINE</span>
            </>
          )}
        </div>
        
        {/* Source Selector Dropdown */}
        <div className="relative">
          <button
            onClick={() => setDropdownOpen(!dropdownOpen)}
            className="flex items-center gap-2 px-2 py-1 rounded text-xs font-mono bg-[var(--secondary)] hover:bg-[var(--secondary-foreground)]/10 transition-colors"
          >
            {isRTSP ? (
              <Radio size={12} className="text-[var(--neural-blue)]" />
            ) : (
              <Camera size={12} className="text-[var(--hardware-orange)]" />
            )}
            <span className="text-[var(--foreground)]">{selectedSource?.name || "SELECT SOURCE"}</span>
            <ChevronDown size={12} className={`text-[var(--muted-foreground)] transition-transform ${dropdownOpen ? "rotate-180" : ""}`} />
          </button>
          
          {/* Dropdown Menu */}
          {dropdownOpen && (
            <div className="absolute right-0 top-full mt-1 w-64 bg-[var(--card)] border border-[var(--border)] rounded shadow-lg z-50 overflow-hidden">
              {/* UVC Sources */}
              <div className="px-2 py-1.5 bg-[var(--secondary)] border-b border-[var(--border)]">
                <div className="flex items-center gap-1.5">
                  <Camera size={10} className="text-[var(--hardware-orange)]" />
                  <span className="font-mono text-xs text-[var(--hardware-orange)]">UVC DEVICES</span>
                </div>
              </div>
              {sources.filter(s => s.type === "uvc").map(source => (
                <button
                  key={source.id}
                  onClick={() => {
                    onSourceChange?.(source.id)
                    setDropdownOpen(false)
                  }}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-[var(--secondary)] transition-colors ${
                    selectedSource?.id === source.id ? "bg-[var(--secondary)]" : ""
                  }`}
                >
                  <div className={`w-1.5 h-1.5 rounded-full ${
                    source.status === "online" ? "bg-[var(--validation-emerald)]" :
                    source.status === "connecting" ? "bg-[var(--hardware-orange)] pulse-orange" :
                    "bg-[var(--muted-foreground)]"
                  }`} />
                  <span className="font-mono text-xs text-[var(--foreground)] flex-1">{source.name}</span>
                  {source.status === "offline" && (
                    <WifiOff size={10} className="text-[var(--muted-foreground)]" />
                  )}
                </button>
              ))}
              
              {/* RTSP Sources */}
              <div className="px-2 py-1.5 bg-[var(--secondary)] border-y border-[var(--border)]">
                <div className="flex items-center gap-1.5">
                  <Radio size={10} className="text-[var(--neural-blue)]" />
                  <span className="font-mono text-xs text-[var(--neural-blue)]">RTSP STREAMS</span>
                </div>
              </div>
              {sources.filter(s => s.type === "rtsp").map(source => (
                <button
                  key={source.id}
                  onClick={() => {
                    onSourceChange?.(source.id)
                    setDropdownOpen(false)
                  }}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-[var(--secondary)] transition-colors ${
                    selectedSource?.id === source.id ? "bg-[var(--secondary)]" : ""
                  }`}
                >
                  <div className={`w-1.5 h-1.5 rounded-full ${
                    source.status === "online" ? "bg-[var(--validation-emerald)]" :
                    source.status === "connecting" ? "bg-[var(--hardware-orange)] pulse-orange" :
                    "bg-[var(--muted-foreground)]"
                  }`} />
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-xs text-[var(--foreground)]">{source.name}</div>
                    <div className="font-mono text-xs text-[var(--muted-foreground)] truncate">{source.url}</div>
                  </div>
                  {source.status === "online" && (
                    <Signal size={10} className="text-[var(--validation-emerald)]" />
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      
      {/* Simulated Video Feed */}
      <div className="relative aspect-video bg-[#0a0a0a] overflow-hidden">
        {selectedSource?.status === "offline" ? (
          /* Offline State */
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <WifiOff size={32} className="text-[var(--muted-foreground)] mb-2" />
            <span className="font-mono text-sm text-[var(--muted-foreground)]">STREAM OFFLINE</span>
            <span className="font-mono text-xs text-[var(--muted-foreground)] opacity-60 mt-1">
              {isRTSP ? selectedSource?.url : `Device: ${selectedSource?.deviceId}`}
            </span>
          </div>
        ) : selectedSource?.status === "connecting" ? (
          /* Connecting State */
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <div className="relative">
              <Wifi size={32} className="text-[var(--hardware-orange)] animate-pulse" />
              <div className="absolute inset-0 animate-ping">
                <Wifi size={32} className="text-[var(--hardware-orange)] opacity-30" />
              </div>
            </div>
            <span className="font-mono text-sm text-[var(--hardware-orange)] mt-2">ESTABLISHING CONNECTION</span>
            <span className="font-mono text-xs text-[var(--muted-foreground)] mt-1">
              {isRTSP ? selectedSource?.url : `Device: ${selectedSource?.deviceId}`}
            </span>
          </div>
        ) : (
          /* Live Feed */
          <>
            {/* Scan lines effect */}
            <div className="absolute inset-0 pointer-events-none" style={{
              background: `repeating-linear-gradient(
                0deg,
                transparent,
                transparent 2px,
                rgba(0, 0, 0, 0.3) 2px,
                rgba(0, 0, 0, 0.3) 4px
              )`
            }} />
            
            {/* Grid overlay - different color for RTSP */}
            <div className="absolute inset-0 pointer-events-none" style={{
              backgroundImage: `
                linear-gradient(${isRTSP ? "rgba(56, 189, 248, 0.1)" : "rgba(249, 115, 22, 0.1)"} 1px, transparent 1px),
                linear-gradient(90deg, ${isRTSP ? "rgba(56, 189, 248, 0.1)" : "rgba(249, 115, 22, 0.1)"} 1px, transparent 1px)
              `,
              backgroundSize: '20% 20%'
            }} />
            
            {/* Simulated detection boxes */}
            <div 
              className="absolute border-2 border-[var(--validation-emerald)] rounded"
              style={{
                left: `${20 + Math.sin(frame / 30) * 5}%`,
                top: '25%',
                width: '15%',
                height: '30%',
                boxShadow: '0 0 10px var(--validation-emerald-dim)'
              }}
            >
              <div className="absolute -top-5 left-0 font-mono text-xs text-[var(--validation-emerald)] bg-[var(--validation-emerald-dim)] px-1 rounded">
                person 0.94
              </div>
            </div>
            
            <div 
              className="absolute border-2 border-[var(--neural-blue)] rounded"
              style={{
                left: `${55 + Math.cos(frame / 25) * 3}%`,
                top: '35%',
                width: '12%',
                height: '25%',
                boxShadow: '0 0 10px var(--neural-blue-dim)'
              }}
            >
              <div className="absolute -top-5 left-0 font-mono text-xs text-[var(--neural-blue)] bg-[var(--neural-blue-dim)] px-1 rounded">
                car 0.87
              </div>
            </div>
            
            {/* Corner markers - color based on stream type */}
            <div className={`absolute top-2 left-2 w-4 h-4 border-l-2 border-t-2 ${isRTSP ? "border-[var(--neural-blue)]" : "border-[var(--hardware-orange)]"}`} />
            <div className={`absolute top-2 right-2 w-4 h-4 border-r-2 border-t-2 ${isRTSP ? "border-[var(--neural-blue)]" : "border-[var(--hardware-orange)]"}`} />
            <div className={`absolute bottom-2 left-2 w-4 h-4 border-l-2 border-b-2 ${isRTSP ? "border-[var(--neural-blue)]" : "border-[var(--hardware-orange)]"}`} />
            <div className={`absolute bottom-2 right-2 w-4 h-4 border-r-2 border-b-2 ${isRTSP ? "border-[var(--neural-blue)]" : "border-[var(--hardware-orange)]"}`} />
            
            {/* Stream Type Badge */}
            <div className={`absolute top-2 left-8 px-2 py-0.5 rounded font-mono text-xs ${
              isRTSP 
                ? "bg-[var(--neural-blue-dim)] text-[var(--neural-blue)]" 
                : "bg-[var(--hardware-orange-dim)] text-[var(--hardware-orange)]"
            }`}>
              {isRTSP ? "RTSP" : "UVC"}
            </div>
            
            {/* Network latency indicator for RTSP */}
            {isRTSP && (
              <div className="absolute top-2 right-8 flex items-center gap-1 px-2 py-0.5 rounded bg-black/50">
                <Signal size={10} className="text-[var(--validation-emerald)]" />
                <span className="font-mono text-xs text-[var(--foreground)]">{vitals.latency}ms</span>
              </div>
            )}
            
            {/* Timestamp */}
            <div className="absolute bottom-2 left-1/2 -translate-x-1/2 font-mono text-xs text-[var(--foreground)] bg-black/50 px-2 py-0.5 rounded">
              {timestamp}
            </div>
          </>
        )}
      </div>
      
      {/* Vitals Bar */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 p-2 bg-[var(--secondary)]">
        {/* FPS */}
        <div className="flex items-center gap-1">
          <span className={`font-mono text-sm font-semibold tabular-nums ${
            fpsHealth === "good" ? "text-[var(--validation-emerald)]" :
            fpsHealth === "warning" ? "text-[var(--hardware-orange)]" :
            "text-[var(--critical-red)]"
          }`}>
            {selectedSource?.status === "online" ? vitals.fps.toFixed(1) : "--"}
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">FPS</span>
        </div>
        
        {/* Latency */}
        <div className="flex items-center gap-1">
          <span className="font-mono text-sm font-semibold text-[var(--neural-blue)] tabular-nums">
            {selectedSource?.status === "online" ? vitals.latency : "--"}
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">ms</span>
        </div>
        
        {/* Bitrate */}
        <div className="flex items-center gap-1">
          <span className="font-mono text-sm font-semibold text-[var(--artifact-purple)] tabular-nums">
            {selectedSource?.status === "online" ? vitals.bitrate : "--"}
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Mbps</span>
        </div>
        
        {/* Encoding + Resolution */}
        <div className="flex items-center gap-1">
          <span className="font-mono text-xs font-semibold text-[var(--foreground)]">
            {selectedSource?.status === "online" ? vitals.encoding : "--"}
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{vitals.resolution}</span>
        </div>
        
        {/* Protocol */}
        <div className={`font-mono text-xs font-semibold px-1.5 py-0.5 rounded ${
          isRTSP 
            ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]" 
            : "bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)]"
        }`}>
          {isRTSP ? "RTSP" : "UVC"}
        </div>
      </div>
    </div>
  )
}

function SimulationResults({
  simulations,
  onTriggerSimulation,
}: {
  simulations: SimulationItem[]
  onTriggerSimulation?: (track: string, module: string, mock: boolean, npuModel?: string, npuFramework?: string) => void
}) {
  const [collapsed, setCollapsed] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [formTrack, setFormTrack] = useState<"algo" | "hw" | "npu">("algo")
  const [formModule, setFormModule] = useState("")
  const [formMock, setFormMock] = useState(true)
  const [formModelPath, setFormModelPath] = useState("")
  const [formFramework, setFormFramework] = useState("rknn")

  return (
    <div className="border-b border-[var(--border)]">
      {/* Header */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--secondary)] transition-colors"
      >
        <Cpu size={12} className="text-[var(--neural-blue)]" />
        <span className="font-mono text-[9px] font-semibold tracking-fui text-[var(--neural-blue)] flex-1 text-left">
          SIMULATION RESULTS
        </span>
        <span className="font-mono text-[9px] text-[var(--muted-foreground)]">{simulations.length}</span>
        <ChevronDown size={10} className={`text-[var(--muted-foreground)] transition-transform ${collapsed ? "-rotate-90" : ""}`} />
      </button>

      {!collapsed && (
        <div className="px-3 pb-2 space-y-1.5">
          {/* Simulation items — scrollable when list is long */}
          <div className="max-h-[120px] overflow-y-auto space-y-1">
          {simulations.map(sim => {
            const statusColor =
              sim.status === "pass" ? "text-[var(--validation-emerald)]" :
              sim.status === "fail" ? "text-[var(--critical-red)]" :
              sim.status === "running" ? "text-[var(--hardware-orange)]" :
              "text-[var(--critical-red)]"
            const statusBg =
              sim.status === "pass" ? "bg-[var(--validation-emerald)]/15" :
              sim.status === "fail" ? "bg-[var(--critical-red)]/15" :
              sim.status === "running" ? "bg-[var(--hardware-orange)]/15" :
              "bg-[var(--critical-red)]/15"

            return (
              <div key={sim.id} className="flex items-center gap-1.5 p-1.5 rounded bg-[var(--secondary)] hover:bg-[var(--secondary-foreground)]/10 transition-colors">
                {/* Track icon */}
                <Cpu size={10} className={sim.track === "algo" ? "text-[var(--neural-blue)]" : sim.track === "npu" ? "text-[var(--artifact-purple)]" : "text-[var(--hardware-orange)]"} />
                {/* Module */}
                <span className="font-mono text-[9px] text-[var(--foreground)] flex-1 truncate">{sim.module}</span>
                {/* Status badge */}
                <span className={`font-mono text-[8px] font-semibold px-1.5 py-0.5 rounded ${statusColor} ${statusBg}`}>
                  {sim.status.toUpperCase()}
                </span>
                {/* Test count */}
                <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
                  {sim.tests_passed}/{sim.tests_total}
                </span>
                {/* Duration */}
                <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
                  {sim.duration_ms}ms
                </span>
                {/* Valgrind errors */}
                {sim.valgrind_errors > 0 && (
                  <span className="font-mono text-[8px] text-[var(--critical-red)] font-semibold">
                    V:{sim.valgrind_errors}
                  </span>
                )}
                {/* NPU metrics */}
                {sim.track === "npu" && sim.npu_latency_ms != null && (
                  <span className="font-mono text-[8px] text-[var(--artifact-purple)]" title={`${(sim.npu_throughput_fps || 0).toFixed(1)}fps`}>
                    {(sim.npu_latency_ms || 0).toFixed(1)}ms
                  </span>
                )}
              </div>
            )
          })}

          {simulations.length === 0 && (
            <div className="font-mono text-[9px] text-[var(--muted-foreground)] py-2 text-center opacity-60">
              No simulation results yet
            </div>
          )}
          </div>

          {/* RUN button / inline form */}
          {!showForm ? (
            <button
              onClick={() => setShowForm(true)}
              className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded border border-dashed border-[var(--border)] hover:border-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/5 transition-all group"
            >
              <Play size={10} className="text-[var(--muted-foreground)] group-hover:text-[var(--neural-blue)]" />
              <span className="font-mono text-[9px] text-[var(--muted-foreground)] group-hover:text-[var(--neural-blue)]">RUN</span>
            </button>
          ) : (
            <div className="space-y-1.5 p-2 rounded border border-[var(--border)] bg-[var(--background)]">
              {/* Track selector */}
              <div className="flex items-center gap-2">
                <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-10">TRACK</span>
                <div className="flex gap-1 flex-1">
                  <button
                    onClick={() => setFormTrack("algo")}
                    className={`flex-1 font-mono text-[9px] py-1 rounded transition-colors ${
                      formTrack === "algo"
                        ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)] border border-[var(--neural-blue)]"
                        : "bg-[var(--secondary)] text-[var(--muted-foreground)] border border-[var(--border)]"
                    }`}
                  >
                    ALGO
                  </button>
                  <button
                    onClick={() => setFormTrack("hw")}
                    className={`flex-1 font-mono text-[9px] py-1 rounded transition-colors ${
                      formTrack === "hw"
                        ? "bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)] border border-[var(--hardware-orange)]"
                        : "bg-[var(--secondary)] text-[var(--muted-foreground)] border border-[var(--border)]"
                    }`}
                  >
                    HW
                  </button>
                  <button
                    onClick={() => setFormTrack("npu")}
                    className={`flex-1 font-mono text-[9px] py-1 rounded transition-colors ${
                      formTrack === "npu"
                        ? "bg-[var(--artifact-purple)]/20 text-[var(--artifact-purple)] border border-[var(--artifact-purple)]"
                        : "bg-[var(--secondary)] text-[var(--muted-foreground)] border border-[var(--border)]"
                    }`}
                  >
                    NPU
                  </button>
                </div>
              </div>
              {/* Module input */}
              <div className="flex items-center gap-2">
                <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-10">MOD</span>
                <input
                  type="text"
                  value={formModule}
                  onChange={(e) => setFormModule(e.target.value)}
                  placeholder="module name"
                  className="flex-1 font-mono text-[9px] px-2 py-1 rounded bg-[var(--secondary)] border border-[var(--border)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
                />
              </div>
              {/* NPU-specific fields */}
              {formTrack === "npu" && (
                <>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-10">MODEL</span>
                    <input
                      type="text"
                      value={formModelPath}
                      onChange={(e) => setFormModelPath(e.target.value)}
                      placeholder="model.rknn"
                      className="flex-1 font-mono text-[9px] px-2 py-1 rounded bg-[var(--secondary)] border border-[var(--border)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:border-[var(--artifact-purple)]"
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-10">FW</span>
                    <div className="flex gap-1 flex-1">
                      {["rknn", "tflite", "tensorrt"].map(fw => (
                        <button
                          key={fw}
                          onClick={() => setFormFramework(fw)}
                          className={`flex-1 font-mono text-[8px] py-0.5 rounded transition-colors ${
                            formFramework === fw
                              ? "bg-[var(--artifact-purple)]/20 text-[var(--artifact-purple)] border border-[var(--artifact-purple)]"
                              : "bg-[var(--secondary)] text-[var(--muted-foreground)] border border-[var(--border)]"
                          }`}
                        >
                          {fw.toUpperCase()}
                        </button>
                      ))}
                    </div>
                  </div>
                </>
              )}
              {/* Mock toggle (algo/hw only) */}
              {formTrack !== "npu" && (
              <div className="flex items-center gap-2">
                <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-10">MOCK</span>
                <button
                  onClick={() => setFormMock(!formMock)}
                  className="flex items-center gap-1"
                >
                  {formMock ? (
                    <ToggleRight size={16} className="text-[var(--validation-emerald)]" />
                  ) : (
                    <ToggleLeft size={16} className="text-[var(--muted-foreground)]" />
                  )}
                  <span className={`font-mono text-[9px] ${formMock ? "text-[var(--validation-emerald)]" : "text-[var(--muted-foreground)]"}`}>
                    {formMock ? "ON" : "OFF"}
                  </span>
                </button>
              </div>
              )}
              {/* Submit / Cancel */}
              <div className="flex gap-1.5 pt-1">
                <button
                  onClick={() => {
                    if (formModule.trim() && onTriggerSimulation) {
                      onTriggerSimulation(formTrack, formModule.trim(), formMock, formTrack === "npu" ? formModelPath : undefined, formTrack === "npu" ? formFramework : undefined)
                      setFormModule("")
                      setShowForm(false)
                    }
                  }}
                  disabled={!formModule.trim()}
                  className="flex-1 font-mono text-[9px] py-1.5 rounded bg-[var(--neural-blue)] text-black font-semibold hover:opacity-90 transition-opacity disabled:opacity-30"
                >
                  EXECUTE
                </button>
                <button
                  onClick={() => setShowForm(false)}
                  className="font-mono text-[9px] py-1.5 px-3 rounded border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--secondary)] transition-colors"
                >
                  CANCEL
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ReporterVortex({ logs, artifacts, simulations = [], onTriggerSimulation }: { logs: LogEntry[]; artifacts: Artifact[]; simulations?: SimulationItem[]; onTriggerSimulation?: (track: string, module: string, mock: boolean) => void }) {
  return (
    <div className="holo-glass-simple rounded flex-1 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border)]">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-full border border-[var(--artifact-purple)] flex items-center justify-center vortex">
            <div className="w-2 h-2 bg-[var(--artifact-purple)] rounded-full" />
          </div>
          <span className="font-sans text-xs font-semibold tracking-fui text-[var(--artifact-purple)]">REPORTER VORTEX</span>
        </div>
      </div>
      
      {/* Simulation Results — above log stream so it's always visible */}
      <SimulationResults simulations={simulations} onTriggerSimulation={onTriggerSimulation} />

      {/* Log Stream */}
      <div className="flex-1 overflow-y-auto overflow-x-hidden p-3 space-y-1 min-h-0">
        {logs.map((log, i) => {
          // Tag-based color extraction
          const tagMatch = log.message.match(/^\[([A-Z]+)\]/)
          const tag = tagMatch?.[1] || ""

          // Level color (highest priority)
          const levelColor =
            log.level === "error" ? "text-[var(--critical-red)]" :
            log.level === "warn" ? "text-[var(--hardware-orange)]" :
            ""

          // Tag color (when level is info)
          const tagColor = !levelColor ? (
            tag === "AGENT"     ? "text-[var(--neural-blue)]" :
            tag === "TOOL"      ? "text-[var(--validation-emerald)]" :
            tag === "PIPELINE"  ? "text-[var(--artifact-purple)]" :
            tag === "TASK"      ? "text-[var(--hardware-orange)]" :
            tag === "WORKSPACE" ? "text-cyan-400" :
            tag === "DOCKER"    ? "text-sky-400" :
            tag === "INVOKE"    ? "text-yellow-300" :
            tag === "REPORT"    ? "text-pink-400" :
            "text-[var(--foreground)] opacity-70"
          ) : ""

          // Split message into tag part + rest for dual coloring
          const msgBody = tagMatch ? log.message.slice(tagMatch[0].length) : log.message
          const tagLabel = tagMatch?.[0] || ""

          return (
            <div key={i} className="flex items-start gap-2 font-mono text-xs leading-relaxed">
              <span className="text-[var(--muted-foreground)] shrink-0 opacity-60">{log.timestamp}</span>
              {levelColor ? (
                <span className={levelColor}>{log.message}</span>
              ) : (
                <span>
                  {tagLabel && <span className={`${tagColor} font-semibold`}>{tagLabel}</span>}
                  <span className="text-[var(--foreground)] opacity-80">{msgBody}</span>
                </span>
              )}
            </div>
          )
        })}
      </div>

      {/* Artifacts */}
      <div className="border-t border-[var(--border)] p-3">
        <div className="font-mono text-xs text-[var(--muted-foreground)] mb-2">GENERATED ARTIFACTS</div>
        <div className="space-y-2">
          {artifacts.map(artifact => (
            <div 
              key={artifact.id}
              className="flex items-center gap-2 p-2 rounded bg-[var(--artifact-purple-dim)] hover:bg-[var(--artifact-purple)] hover:bg-opacity-20 transition-colors cursor-pointer group"
            >
              <FileText size={14} className="text-[var(--artifact-purple)]" />
              <div className="flex-1 min-w-0">
                <div className="font-mono text-xs text-[var(--foreground)] truncate">{artifact.name}</div>
                <div className="font-mono text-xs text-[var(--muted-foreground)]">
                  {artifact.timestamp} - {typeof artifact.size === "number" ? `${(artifact.size / 1024).toFixed(1)} KB` : artifact.size}
                  {artifact.version && <span className="ml-1 text-[var(--artifact-purple)]">v{artifact.version}</span>}
                </div>
              </div>
              <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <a
                  href={getArtifactDownloadUrl(artifact.id)}
                  download={artifact.name}
                  className="p-1 hover:bg-[var(--artifact-purple-dim)] rounded"
                  title="Download"
                >
                  <Download size={12} className="text-[var(--artifact-purple)]" />
                </a>
                <a
                  href={getArtifactDownloadUrl(artifact.id)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="p-1 hover:bg-[var(--artifact-purple-dim)] rounded"
                  title="Open in new tab"
                >
                  <ExternalLink size={12} className="text-[var(--artifact-purple)]" />
                </a>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

interface VitalsArtifactsPanelProps {
  vitals?: VitalsData
  logs?: LogEntry[]
  artifacts?: Artifact[]
  streamSources?: StreamSource[]
  selectedStreamId?: string
  onStreamChange?: (streamId: string) => void
  simulations?: SimulationItem[]
  onTriggerSimulation?: (track: string, module: string, mock: boolean) => void
}

// Compact feed card for multi-view
function CompactFeedCard({ 
  source,
  vitals,
  onRemove,
  onMaximize,
  isMaximized
}: { 
  source: StreamSource
  vitals: VitalsData
  onRemove: () => void
  onMaximize: () => void
  isMaximized: boolean
}) {
  const [timestamp, setTimestamp] = useState("--:--:--.---")
  
  useEffect(() => {
    const interval = setInterval(() => {
      setTimestamp(new Date().toISOString().slice(11, 23))
    }, 33)
    return () => clearInterval(interval)
  }, [])
  
  const isRTSP = source.type === "rtsp"
  
  return (
    <div className={`holo-glass-simple rounded overflow-hidden flex flex-col scanlines corner-brackets ${isMaximized ? "col-span-full row-span-2" : ""}`}>
      {/* Mini Header */}
      <div className="flex items-center justify-between px-2 py-1 border-b border-[var(--border)] bg-[var(--background)]/50">
        <div className="flex items-center gap-1.5 min-w-0">
          {source.status === "online" ? (
            <div className="w-1.5 h-1.5 rounded-full bg-[var(--critical-red)] live-indicator shrink-0" />
          ) : (
            <div className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] shrink-0" />
          )}
          {isRTSP ? (
            <Radio size={10} className="text-[var(--neural-blue)] shrink-0" />
          ) : (
            <Camera size={10} className="text-[var(--hardware-orange)] shrink-0" />
          )}
          <span className="font-mono text-[10px] text-[var(--foreground)] truncate">{source.name}</span>
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          <button
            onClick={onMaximize}
            className="p-0.5 rounded hover:bg-[var(--secondary)] transition-colors"
            title={isMaximized ? "Minimize" : "Maximize"}
          >
            {isMaximized ? (
              <Minimize2 size={10} className="text-[var(--muted-foreground)]" />
            ) : (
              <Maximize2 size={10} className="text-[var(--muted-foreground)]" />
            )}
          </button>
          <button
            onClick={onRemove}
            className="p-0.5 rounded hover:bg-[var(--critical-red)]/20 transition-colors"
            title="Remove feed"
          >
            <X size={10} className="text-[var(--muted-foreground)] hover:text-[var(--critical-red)]" />
          </button>
        </div>
      </div>
      
      {/* Video Area */}
      <div className="relative flex-1 min-h-[80px] bg-[#0a0a0a]">
        {source.status === "offline" ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <WifiOff size={16} className="text-[var(--muted-foreground)] mb-1" />
            <span className="font-mono text-[10px] text-[var(--muted-foreground)]">OFFLINE</span>
          </div>
        ) : (
          <>
            {/* Scan lines effect */}
            <div className="absolute inset-0 bg-gradient-to-b from-transparent via-[var(--neural-blue)]/5 to-transparent bg-[length:100%_4px] pointer-events-none" />
            
            {/* Detection boxes - simplified for compact view */}
            <div 
              className="absolute border border-[var(--neural-blue)] bg-[var(--neural-blue)]/10"
              style={{ left: "20%", top: "30%", width: "25%", height: "40%" }}
            >
              <span className="absolute -top-4 left-0 font-mono text-[8px] px-1 bg-[var(--neural-blue)] text-black">
                obj
              </span>
            </div>
            
            {/* Timestamp */}
            <div className="absolute bottom-1 left-1/2 -translate-x-1/2">
              <span className="font-mono text-[10px] text-[var(--foreground)] bg-black/60 px-1 rounded" suppressHydrationWarning>
                {timestamp}
              </span>
            </div>
            
            {/* Protocol badge */}
            <div className="absolute top-1 right-1">
              <span className={`font-mono text-[8px] px-1 py-0.5 rounded ${
                isRTSP 
                  ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]" 
                  : "bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)]"
              }`}>
                {isRTSP ? "RTSP" : "UVC"}
              </span>
            </div>
          </>
        )}
      </div>
      
      {/* Mini Stats */}
      {source.status === "online" && (
        <div className="flex items-center justify-between px-2 py-1 bg-[var(--secondary)] text-[var(--muted-foreground)]">
          <span className="font-mono text-[9px]">{vitals.fps.toFixed(0)} FPS</span>
          <span className="font-mono text-[9px]">{vitals.latency}ms</span>
          <span className="font-mono text-[9px]">{vitals.encoding}</span>
        </div>
      )}
    </div>
  )
}

// Add feed button
function AddFeedButton({ 
  availableSources, 
  onAdd 
}: { 
  availableSources: StreamSource[]
  onAdd: (sourceId: string) => void 
}) {
  const [showMenu, setShowMenu] = useState(false)
  
  if (availableSources.length === 0) {
    return (
      <div className="flex items-center justify-center p-4 border-2 border-dashed border-[var(--border)] rounded opacity-50">
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">No sources available</span>
      </div>
    )
  }
  
  return (
    <div className="relative">
      <button
        onClick={() => setShowMenu(!showMenu)}
        className="w-full flex items-center justify-center gap-2 p-4 border-2 border-dashed border-[var(--border)] rounded hover:border-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/5 transition-all group"
      >
        <Plus size={16} className="text-[var(--muted-foreground)] group-hover:text-[var(--neural-blue)]" />
        <span className="font-mono text-xs text-[var(--muted-foreground)] group-hover:text-[var(--neural-blue)]">
          ADD FEED
        </span>
      </button>
      
      {showMenu && (
        <div className="absolute left-0 right-0 top-full mt-1 bg-[var(--card)] border border-[var(--border)] rounded shadow-lg z-50 overflow-hidden">
          {availableSources.map(source => (
            <button
              key={source.id}
              onClick={() => {
                onAdd(source.id)
                setShowMenu(false)
              }}
              className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-[var(--secondary)] transition-colors"
            >
              <div className={`w-1.5 h-1.5 rounded-full ${
                source.status === "online" ? "bg-[var(--validation-emerald)]" :
                source.status === "connecting" ? "bg-[var(--hardware-orange)]" :
                "bg-[var(--muted-foreground)]"
              }`} />
              {source.type === "rtsp" ? (
                <Radio size={12} className="text-[var(--neural-blue)]" />
              ) : (
                <Camera size={12} className="text-[var(--hardware-orange)]" />
              )}
              <span className="font-mono text-xs text-[var(--foreground)]">{source.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function VitalsArtifactsPanel({
  vitals = emptyVitals,
  logs = noLogs,
  artifacts = noArtifacts,
  streamSources = noStreamSources,
  selectedStreamId,
  onStreamChange,
  simulations = [],
  onTriggerSimulation,
}: VitalsArtifactsPanelProps) {
  // Track multiple active feeds (0-8)
  const [activeFeeds, setActiveFeeds] = useState<string[]>(() => {
    const onlineSource = streamSources.find(s => s.status === "online")
    return onlineSource ? [onlineSource.id] : []
  })
  const [maximizedFeed, setMaximizedFeed] = useState<string | null>(null)
  
  const availableSources = streamSources.filter(s => !activeFeeds.includes(s.id))
  const canAddMore = activeFeeds.length < 8
  
  const handleAddFeed = useCallback((sourceId: string) => {
    if (activeFeeds.length < 8) {
      setActiveFeeds(prev => [...prev, sourceId])
    }
  }, [activeFeeds.length])
  
  const handleRemoveFeed = useCallback((sourceId: string) => {
    setActiveFeeds(prev => prev.filter(id => id !== sourceId))
    if (maximizedFeed === sourceId) {
      setMaximizedFeed(null)
    }
  }, [maximizedFeed])
  
  const handleMaximize = useCallback((sourceId: string) => {
    setMaximizedFeed(prev => prev === sourceId ? null : sourceId)
  }, [])
  
  // Determine grid layout based on feed count
  const getGridClass = () => {
    const count = activeFeeds.length
    if (count === 0) return ""
    if (count === 1) return "grid-cols-1"
    if (count === 2) return "grid-cols-2"
    if (count <= 4) return "grid-cols-2"
    if (count <= 6) return "grid-cols-3"
    return "grid-cols-4"
  }

  return (
    <div className="h-full flex flex-col gap-3">
      {/* Header */}
      <div className="px-3 py-2 holo-glass-simple corner-brackets relative">
        {/* Radar sweep effect */}
        <div className="absolute top-1 right-1 w-6 h-6 rounded-full overflow-hidden opacity-30">
          <div className="w-full h-full radar-sweep" />
        </div>
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--validation-emerald)] pulse-emerald signal-wave" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--validation-emerald)] mr-1">
              LIVE FEEDS
            </h2>
            <PanelHelp doc="panels-overview" />
          </div>
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-[var(--muted-foreground)]">
              {activeFeeds.length}/8
            </span>
            {activeFeeds.length > 1 && (
              <div className="flex items-center gap-1">
                <Grid2X2 size={12} className={`${activeFeeds.length <= 4 ? "text-[var(--neural-blue)]" : "text-[var(--muted-foreground)]"}`} />
                <Grid3X3 size={12} className={`${activeFeeds.length > 4 ? "text-[var(--neural-blue)]" : "text-[var(--muted-foreground)]"}`} />
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 mt-1">
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            {streamSources.filter(s => s.status === "online").length} ONLINE
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            {streamSources.filter(s => s.type === "uvc").length} UVC
          </span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            {streamSources.filter(s => s.type === "rtsp").length} RTSP
          </span>
        </div>
      </div>
      
      {/* Multi-Feed Grid */}
      <div className="flex-1 overflow-y-auto overflow-x-hidden">
        {activeFeeds.length === 0 ? (
          /* Empty State */
          <div className="flex flex-col items-center justify-center h-full min-h-[200px] text-center px-4">
            <div className="w-16 h-16 rounded-full border-2 border-dashed border-[var(--border)] flex items-center justify-center mb-4">
              <Camera size={24} className="text-[var(--muted-foreground)] opacity-50" />
            </div>
            <p className="font-mono text-xs text-[var(--muted-foreground)] mb-1">
              No active feeds
            </p>
            <p className="font-mono text-[10px] text-[var(--muted-foreground)] opacity-60 mb-4">
              Add a camera feed to begin monitoring
            </p>
            <AddFeedButton 
              availableSources={availableSources}
              onAdd={handleAddFeed}
            />
          </div>
        ) : (
          <div className={`grid ${getGridClass()} gap-2`}>
            {activeFeeds.map(feedId => {
              const source = streamSources.find(s => s.id === feedId)
              if (!source) return null
              return (
                <CompactFeedCard
                  key={feedId}
                  source={source}
                  vitals={vitals}
                  onRemove={() => handleRemoveFeed(feedId)}
                  onMaximize={() => handleMaximize(feedId)}
                  isMaximized={maximizedFeed === feedId}
                />
              )
            })}
            
            {/* Add Feed Button in Grid */}
            {canAddMore && availableSources.length > 0 && (
              <AddFeedButton 
                availableSources={availableSources}
                onAdd={handleAddFeed}
              />
            )}
          </div>
        )}
      </div>
      
      {/* Reporter Vortex - Always visible (contains simulation results, artifacts, logs) */}
      <ReporterVortex logs={logs} artifacts={artifacts} simulations={simulations} onTriggerSimulation={onTriggerSimulation} />
    </div>
  )
}
