"use client"

import { useState, useRef, useEffect } from "react"
import { Send, Zap } from "lucide-react"

interface InvokeCoreProps {
  onInvoke?: () => void
  onCommand?: (command: string) => void
  onCommandChange?: (command: string) => void
}

export function InvokeCore({ onInvoke, onCommand, onCommandChange }: InvokeCoreProps) {
  const [command, setCommand] = useState("")
  const [isInvoking, setIsInvoking] = useState(false)
  const [showBeams, setShowBeams] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleInvoke = () => {
    setIsInvoking(true)
    setShowBeams(true)
    
    setTimeout(() => {
      setIsInvoking(false)
      onInvoke?.()
    }, 1500)
    
    setTimeout(() => {
      setShowBeams(false)
    }, 800)
  }

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (command.trim()) {
      onCommand?.(command)
      setCommand("")
      onCommandChange?.("")
    }
  }

  // Focus input on "/" key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "/" && document.activeElement !== inputRef.current) {
        e.preventDefault()
        inputRef.current?.focus()
      }
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [])

  return (
    <div className="relative">
      {/* Energy Beams */}
      {showBeams && (
        <>
          {/* Left beam */}
          <div 
            className="absolute left-0 top-1/2 -translate-y-1/2 h-0.5 energy-beam"
            style={{ 
              width: '40%',
              transform: 'translateY(-50%) translateX(-100%)',
              background: 'linear-gradient(270deg, var(--neural-blue), transparent)'
            }}
          />
          {/* Right beam */}
          <div 
            className="absolute right-0 top-1/2 -translate-y-1/2 h-0.5 energy-beam"
            style={{ 
              width: '40%',
              transform: 'translateY(-50%) translateX(100%)',
              background: 'linear-gradient(90deg, var(--neural-blue), transparent)'
            }}
          />
          {/* Top beam */}
          <div 
            className="absolute left-1/2 top-0 w-0.5 energy-beam"
            style={{ 
              height: '200%',
              transform: 'translateX(-50%) translateY(-100%)',
              background: 'linear-gradient(0deg, var(--neural-blue), transparent)'
            }}
          />
        </>
      )}
      
      <div className="holo-glass-simple py-3 px-4 corner-brackets-full relative overflow-hidden">
        {/* Holographic shimmer background */}
        <div className="absolute inset-0 holo-shimmer opacity-30 pointer-events-none" />
        
        <div className="flex items-center gap-3 relative z-10">
          {/* Invoke Button - Compact */}
          <button
            onClick={handleInvoke}
            disabled={isInvoking}
            className={`relative shrink-0 ${isInvoking ? 'scale-90 glitch' : 'hover:scale-105'} transition-transform duration-300`}
            title={isInvoking ? 'Syncing...' : 'Invoke Core'}
          >
            {/* Outer glow ring */}
            {isInvoking && (
              <div className="absolute inset-0 rounded-full animate-ping" 
                style={{ 
                  background: 'var(--neural-blue)',
                  opacity: 0.2,
                  transform: 'scale(1.2)'
                }} 
              />
            )}
            
            {/* Pulsing ring effect */}
            <div className="absolute inset-0 rounded-full pulse-ring" />
            
            {/* Main button */}
            <div className={`relative w-10 h-10 rounded-full flex items-center justify-center core-glow neon-border ${isInvoking ? 'bg-[var(--neural-blue)] power-up' : 'bg-[var(--secondary)]'} border-2 border-[var(--neural-blue)] transition-colors duration-300`}>
              {/* Inner rotating ring */}
              <div className="absolute inset-1 rounded-full border border-[var(--neural-blue)] border-dashed ring-spin opacity-50" />
              {/* Second counter-rotating ring */}
              <div className="absolute inset-2 rounded-full border border-[var(--artifact-purple)] border-dotted ring-spin-reverse opacity-30" />
              
              {/* Icon */}
              <Zap 
                size={16} 
                className={`relative z-10 transition-colors duration-300 ${isInvoking ? 'text-[var(--deep-space-start)] fill-[var(--deep-space-start)]' : 'text-[var(--neural-blue)]'}`}
              />
            </div>
          </button>
          
          {/* Command Input - Expanded */}
          <form onSubmit={handleSubmit} className="flex-1 min-w-0">
            <div className="relative">
              <input
                ref={inputRef}
                type="text"
                value={command}
                onChange={e => { setCommand(e.target.value); onCommandChange?.(e.target.value) }}
                placeholder="> enter command..."
                className="w-full fui-input px-3 py-2 pr-10 font-mono text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] transition-colors"
              />
              <button
                type="submit"
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] transition-colors"
              >
                <Send size={14} />
              </button>
            </div>
          </form>
          
          {/* Status indicators - Compact */}
          <div className="flex items-center gap-2 shrink-0">
            <div className="w-2 h-2 rounded-full bg-[var(--validation-emerald)] status-dot-active" title="Ready" />
            <div className="w-2 h-2 rounded-full bg-[var(--neural-blue)] pulse-blue" title="Connected" />
          </div>
        </div>
      </div>
    </div>
  )
}
