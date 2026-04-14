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

  // Layout-safety constants — every visual state of this button is
  // boxed into the same dimensions so siblings (clock, language
  // toggle, etc.) never move when state changes.
  //
  // Width budget for the longest variant: " CONFIRM " (7 chars at
  // text-xs mono ≈ 7px each = 49px) + 14px icon + gap-2 (8px) +
  // px-3 (24px) = ~95px. Cap at 100px.
  // Border swap (1px → 2px on CONFIRM) replaced with `outline` so
  // it never affects the box dimensions.
  const dims: React.CSSProperties = {
    width: 100,
    height: 32,
    flexShrink: 0,
  }

  // Show resume button when halted
  if (isHalted) {
    return (
      <button
        onClick={handleResumeClick}
        disabled={disabled || !onResume}
        style={dims}
        className={`
          relative group flex items-center justify-center gap-2 rounded-lg
          font-mono text-xs font-semibold tracking-wider
          transition-[colors,box-shadow] duration-300
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
        <Play size={14} className="relative z-10 fill-current shrink-0" />

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
      style={dims}
      className={`
        relative group flex items-center justify-center gap-2 rounded-lg
        font-mono text-xs font-semibold tracking-wider
        transition-[colors,box-shadow] duration-300
        border border-[var(--critical-red)]/50
        ${disabled
          ? "opacity-40 cursor-not-allowed bg-[var(--secondary)]"
          : isActivating
            ? "bg-[var(--critical-red)] text-white animate-pulse outline outline-2 outline-[var(--critical-red)]"
            : showConfirm
              ? "bg-[var(--critical-red)]/30 text-[var(--critical-red)] outline outline-2 outline-[var(--critical-red)] outline-offset-[-1px]"
              : "bg-[var(--critical-red)]/10 text-[var(--critical-red)] hover:bg-[var(--critical-red)]/20 hover:border-[var(--critical-red)]"
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
        className={`relative z-10 shrink-0 ${isActivating ? "animate-spin" : showConfirm ? "animate-pulse" : ""}`}
      />

      {/* Text — fixed-width slot so E-STOP / CONFIRM / HALT do not
       * change the button width; left-padded with NBSP for HALT (4
       * chars) so the icon + text always render centred. */}
      <span
        className="relative z-10 inline-block text-center tabular-nums"
        style={{ width: 50 }}
      >
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
