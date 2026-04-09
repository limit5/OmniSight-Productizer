"use client"

import { useState, useEffect } from "react"
import { EmergencyStop } from "./emergency-stop"
import { LanguageToggle } from "./language-toggle"

interface StatusHeaderProps {
  finished: number
  total: number
  inProgress: number
  wslStatus: "OK" | "OFFLINE"
  usbStatus: string
  onEmergencyStop?: () => void
  onResume?: () => void
  isHalted?: boolean
  hasRunningAgents?: boolean
}

export function GlobalStatusHeader({
  finished = 32,
  total = 48,
  inProgress = 3,
  wslStatus = "OK",
  usbStatus = "Dev_01 USB Attached",
  onEmergencyStop,
  onResume,
  isHalted = false,
  hasRunningAgents = false
}: StatusHeaderProps) {
  const [time, setTime] = useState("")
  
  useEffect(() => {
    const updateTime = () => {
      const now = new Date()
      setTime(now.toLocaleTimeString("en-US", { hour12: false }))
    }
    updateTime()
    const interval = setInterval(updateTime, 1000)
    return () => clearInterval(interval)
  }, [])

  const progressPercent = (finished / total) * 100
  const inProgressPercent = (inProgress / total) * 100

  return (
    <header className="holo-glass-simple relative z-10 px-3 md:px-6 py-2 md:py-3 safe-area-top corner-brackets-full holo-flicker scanlines">
      {/* Mobile Layout */}
      <div className="flex md:hidden items-center justify-between gap-2">
        {/* Compact Logo */}
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 shrink-0 rounded-full border-2 border-[var(--neural-blue)] flex items-center justify-center pulse-blue">
            <div className="w-3 h-3 bg-[var(--neural-blue)] rounded-full" />
          </div>
          <div className="min-w-0">
            <h1 className="font-sans text-sm font-bold tracking-fui text-[var(--neural-blue)] text-glow-blue truncate">
              OMNISIGHT
            </h1>
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-[var(--validation-emerald)]">
                {finished}/{total}
              </span>
              <div className="w-16 h-1.5 bg-[var(--secondary)] rounded-full overflow-hidden">
                <div 
                  className="h-full bg-[var(--validation-emerald)]"
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
            </div>
          </div>
        </div>
        
        {/* Mobile Right: Language + Time + Emergency */}
        <div className="flex items-center gap-2 shrink-0">
          <LanguageToggle compact />
          <div className="font-mono text-xs text-[var(--neural-blue)] tabular-nums">
            {time}
          </div>
          {onEmergencyStop && (
            <EmergencyStop 
              onStop={onEmergencyStop}
              onResume={onResume}
              isHalted={isHalted}
              disabled={!hasRunningAgents}
            />
          )}
        </div>
      </div>
      
      {/* Tablet/Desktop Layout */}
      <div className="hidden md:flex items-center justify-between">
        {/* Logo & Title */}
        <div className="flex items-center gap-4">
          <div className="relative">
            <div className="w-10 h-10 rounded-full border-2 border-[var(--neural-blue)] flex items-center justify-center pulse-blue">
              <div className="w-4 h-4 bg-[var(--neural-blue)] rounded-full" />
            </div>
          </div>
          <div>
            <h1 className="font-sans text-base lg:text-lg font-bold tracking-fui text-[var(--neural-blue)] text-glow-blue">
              OMNISIGHT PRODUCTIZER
            </h1>
            <p className="font-mono text-xs text-[var(--muted-foreground)]">
              NEURAL COMMAND CENTER v2.0
            </p>
          </div>
        </div>

        {/* Project Pipeline Progress - hidden on tablet, shown on desktop */}
        <div className="hidden lg:block flex-1 max-w-md mx-8">
          <div className="flex items-center justify-between mb-1">
            <span className="font-mono text-xs text-[var(--muted-foreground)]">PROJECT PIPELINE</span>
            <span className="font-mono text-xs text-[var(--validation-emerald)]">
              {finished}/{total} COMPLETE
            </span>
          </div>
          <div className="h-2 bg-[var(--secondary)] rounded-full overflow-hidden">
            <div className="h-full flex">
              <div 
                className="h-full bg-[var(--validation-emerald)] transition-all duration-500"
                style={{ width: `${progressPercent}%` }}
              />
              <div 
                className="h-full progress-shimmer transition-all duration-500"
                style={{ width: `${inProgressPercent}%` }}
              />
            </div>
          </div>
        </div>
        
        {/* Tablet: Compact progress */}
        <div className="lg:hidden flex items-center gap-3 mx-4">
          <span className="font-mono text-xs text-[var(--validation-emerald)]">
            {finished}/{total}
          </span>
          <div className="w-24 h-2 bg-[var(--secondary)] rounded-full overflow-hidden">
            <div 
              className="h-full bg-[var(--validation-emerald)]"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
        </div>

        {/* Environment Probes & Emergency Stop */}
        <div className="flex items-center gap-4">
          {/* Status Indicators - hidden on tablet */}
          <div className="hidden lg:flex items-center gap-4">
            <div className="flex items-center gap-2">
              <div className={`status-dot ${wslStatus === "OK" ? "status-dot-active" : "status-dot-error"}`} />
              <span className="font-mono text-xs text-[var(--foreground)]">
                WSL2: <span className={wslStatus === "OK" ? "text-[var(--validation-emerald)]" : "text-[var(--critical-red)]"}>{wslStatus}</span>
              </span>
            </div>
            <div className="flex items-center gap-2">
              <div className="status-dot status-dot-active" />
              <span className="font-mono text-xs text-[var(--foreground)]">{usbStatus}</span>
            </div>
          </div>
          
          {/* Language Toggle */}
          <LanguageToggle />
          
          {/* Time */}
          <div className="font-mono text-sm text-[var(--neural-blue)] tabular-nums">
            {time}
          </div>
          
          {/* Separator */}
          <div className="w-px h-8 bg-[var(--border)]" />
          
          {/* Emergency Stop / Resume Button */}
          {onEmergencyStop && (
            <EmergencyStop 
              onStop={onEmergencyStop}
              onResume={onResume}
              isHalted={isHalted}
              disabled={!hasRunningAgents}
            />
          )}
        </div>
      </div>
    </header>
  )
}
