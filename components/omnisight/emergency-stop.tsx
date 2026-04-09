"use client"

import { useState, useEffect } from "react"
import { OctagonX, Play } from "lucide-react"

interface EmergencyStopProps {
  onStop: () => void
  onResume?: () => void
  isHalted?: boolean
  disabled?: boolean
}

export function EmergencyStop({ onStop, onResume, isHalted = false, disabled = false }: EmergencyStopProps) {
  const [isActivating, setIsActivating] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)

  // Reset states when halted state changes externally
  useEffect(() => {
    if (!isHalted) {
      setIsActivating(false)
      setShowConfirm(false)
    }
  }, [isHalted])

  const handleStopClick = () => {
    if (disabled || isHalted) return
    
    if (!showConfirm) {
      setShowConfirm(true)
      // Auto-dismiss confirm state after 3 seconds
      setTimeout(() => setShowConfirm(false), 3000)
      return
    }

    // Second click - actually stop
    setIsActivating(true)
    onStop()
    
    // Reset activating animation after a moment
    setTimeout(() => {
      setIsActivating(false)
      setShowConfirm(false)
    }, 1000)
  }

  const handleResumeClick = () => {
    if (disabled || !isHalted || !onResume) return
    onResume()
  }

  // Show resume button when halted
  if (isHalted) {
    return (
      <button
        onClick={handleResumeClick}
        disabled={disabled || !onResume}
        className={`
          relative group flex items-center gap-2 px-3 py-2 rounded-lg
          font-mono text-xs font-semibold tracking-wider
          transition-all duration-300 
          ${disabled || !onResume
            ? "opacity-40 cursor-not-allowed bg-[var(--secondary)]" 
            : "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)] border border-[var(--validation-emerald)]/50 hover:bg-[var(--validation-emerald)]/30 hover:border-[var(--validation-emerald)]"
          }
        `}
        title="Resume all halted operations"
      >
        {/* Pulsing indicator */}
        <span className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-[var(--validation-emerald)] animate-pulse" />
        
        {/* Icon */}
        <Play size={14} className="relative z-10 fill-current" />
        
        {/* Text */}
        <span className="relative z-10">RESUME</span>
        
        {/* Glow effect on hover */}
        <span className="absolute inset-0 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-300 shadow-[0_0_15px_var(--validation-emerald)] pointer-events-none" />
      </button>
    )
  }

  return (
    <button
      onClick={handleStopClick}
      disabled={disabled}
      className={`
        relative group flex items-center gap-2 px-3 py-2 rounded-lg
        font-mono text-xs font-semibold tracking-wider
        transition-all duration-300 
        ${disabled 
          ? "opacity-40 cursor-not-allowed bg-[var(--secondary)]" 
          : isActivating
            ? "bg-[var(--critical-red)] text-white animate-pulse scale-105"
            : showConfirm
              ? "bg-[var(--critical-red)]/30 text-[var(--critical-red)] border-2 border-[var(--critical-red)] scale-105"
              : "bg-[var(--critical-red)]/10 text-[var(--critical-red)] border border-[var(--critical-red)]/50 hover:bg-[var(--critical-red)]/20 hover:border-[var(--critical-red)]"
        }
      `}
      title="Emergency stop - halt all running operations"
    >
      {/* Pulsing ring when activating */}
      {isActivating && (
        <>
          <span className="absolute inset-0 rounded-lg bg-[var(--critical-red)] animate-ping opacity-50" />
          <span className="absolute inset-0 rounded-lg bg-[var(--critical-red)] animate-ping opacity-30 animation-delay-150" />
        </>
      )}
      
      {/* Icon */}
      <OctagonX 
        size={14} 
        className={`relative z-10 ${isActivating ? "animate-spin" : showConfirm ? "animate-pulse" : ""}`}
      />
      
      {/* Text */}
      <span className="relative z-10">
        {isActivating 
          ? "HALT" 
          : showConfirm 
            ? "CONFIRM" 
            : "E-STOP"
        }
      </span>
      
      {/* Glow effect on hover */}
      {!disabled && !isActivating && (
        <span className="absolute inset-0 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-300 shadow-[0_0_15px_var(--critical-red)] pointer-events-none" />
      )}
    </button>
  )
}
