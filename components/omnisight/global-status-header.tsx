"use client"

import { useState, useEffect } from "react"
import { EmergencyStop } from "./emergency-stop"
import { LanguageToggle } from "./language-toggle"
import { ModeSelector } from "./mode-selector"
import { HelpMenu } from "./help-menu"
import { ArchIndicator } from "./arch-indicator"

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
  unreadNotifications?: number
  onToggleNotifications?: () => void
}

export function GlobalStatusHeader({
  finished = 0,
  total = 0,
  inProgress = 0,
  wslStatus = "OFFLINE",
  usbStatus = "Detecting...",
  onEmergencyStop,
  onResume,
  unreadNotifications = 0,
  onToggleNotifications,
  isHalted = false,
  hasRunningAgents = false,
  settingsButton,
}: StatusHeaderProps & { settingsButton?: React.ReactNode }) {
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

  const progressPercent = total > 0 ? (finished / total) * 100 : 0
  const inProgressPercent = total > 0 ? (inProgress / total) * 100 : 0

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
        
        {/* Mobile Right: Mode + Language + Time + Emergency */}
        <div className="flex items-center gap-2 shrink-0">
          <ModeSelector compact />
          <ArchIndicator compact />
          <HelpMenu />
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
          {/* Status Indicators - hidden on tablet.
            * Each pill has a fixed-width inner span (right-padded to the
            * widest possible label, e.g. WSL2 reserves room for OFFLINE
            * not OK) so changing state doesn't push the rest of the
            * header sideways. USB string is truncated at 14 chars + ….  */}
          <div className="hidden lg:flex items-center gap-4 shrink-0">
            <div className="flex items-center gap-2 shrink-0" style={{ width: 110 }}>
              <div className={`status-dot ${wslStatus === "OK" ? "status-dot-active" : "status-dot-error"}`} />
              <span className="font-mono text-xs text-[var(--foreground)] whitespace-nowrap">
                WSL2:{" "}
                <span
                  className={`inline-block tabular-nums text-right ${wslStatus === "OK" ? "text-[var(--validation-emerald)]" : "text-[var(--critical-red)]"}`}
                  style={{ minWidth: 56 }}
                  title={`WSL2 ${wslStatus}`}
                >
                  {wslStatus}
                </span>
              </span>
            </div>
            <div
              className="flex items-center gap-2 shrink-0 overflow-hidden"
              style={{ width: 140 }}
              title={usbStatus}
            >
              <div className="status-dot status-dot-active shrink-0" />
              <span
                className="font-mono text-xs text-[var(--foreground)] truncate whitespace-nowrap"
                aria-label={`USB ${usbStatus}`}
              >
                {usbStatus.length > 14 ? usbStatus.slice(0, 13) + "…" : usbStatus}
              </span>
            </div>
          </div>
          
          {/* Operation Mode */}
          <ModeSelector />

          {/* Host vs Target arch indicator */}
          <ArchIndicator />

          {/* Help dropdown (desktop) */}
          <HelpMenu />

          {/* Language Toggle */}
          <LanguageToggle />

          {/* Time */}
          <div className="font-mono text-sm text-[var(--neural-blue)] tabular-nums">
            {time}
          </div>
          
          {/* Separator */}
          <div className="w-px h-8 bg-[var(--border)]" />
          
          {/* Settings Button */}
          {settingsButton}

          {/* Notification Bell */}
          {onToggleNotifications && (
            <button
              onClick={onToggleNotifications}
              className="relative z-20 p-1.5 rounded transition-colors bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] cursor-pointer"
              title="Notifications"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
              {unreadNotifications > 0 && (
                <span className="absolute -top-1 -right-1 min-w-[16px] h-4 flex items-center justify-center rounded-full bg-[var(--critical-red)] text-white text-[9px] font-mono font-bold px-1">
                  {unreadNotifications > 99 ? "99+" : unreadNotifications}
                </span>
              )}
            </button>
          )}

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
