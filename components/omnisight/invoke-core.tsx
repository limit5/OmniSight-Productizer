"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import { Send, Zap } from "lucide-react"
import { matchCommands, CATEGORY_COLORS, type SlashCommand } from "@/lib/slash-commands"

interface InvokeCoreProps {
  onInvoke?: () => void
  onCommand?: (command: string) => void
  onCommandChange?: (command: string) => void
}

export function InvokeCore({ onInvoke, onCommand, onCommandChange }: InvokeCoreProps) {
  const [command, setCommand] = useState("")
  const [isInvoking, setIsInvoking] = useState(false)
  const [showBeams, setShowBeams] = useState(false)
  const [suggestions, setSuggestions] = useState<SlashCommand[]>([])
  const [selectedIdx, setSelectedIdx] = useState(0)
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
      setSuggestions([])
      onCommand?.(command)
      setCommand("")
      onCommandChange?.("")
    }
  }

  const handleInputChange = useCallback((value: string) => {
    setCommand(value)
    onCommandChange?.(value)
    // Show autocomplete for / commands
    if (value.startsWith("/")) {
      const matches = matchCommands(value)
      setSuggestions(matches.slice(0, 8))
      setSelectedIdx(0)
    } else {
      setSuggestions([])
    }
  }, [onCommandChange])

  const selectSuggestion = useCallback((cmd: SlashCommand) => {
    const newVal = `/${cmd.name} `
    setCommand(newVal)
    onCommandChange?.(newVal)
    setSuggestions([])
    inputRef.current?.focus()
  }, [onCommandChange])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (suggestions.length === 0) return
    if (e.key === "ArrowDown") {
      e.preventDefault()
      setSelectedIdx(i => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setSelectedIdx(i => Math.max(i - 1, 0))
    } else if (e.key === "Tab" || (e.key === "Enter" && suggestions.length > 0 && command.endsWith(suggestions[selectedIdx]?.name || ""))) {
      // Tab always selects; Enter only selects if we haven't typed args yet
      if (e.key === "Tab") {
        e.preventDefault()
        selectSuggestion(suggestions[selectedIdx])
      }
    } else if (e.key === "Escape") {
      setSuggestions([])
    }
  }, [suggestions, selectedIdx, command, selectSuggestion])

  // Focus input on "/" key (global) — only when no input/textarea is focused
  useEffect(() => {
    const handleGlobalKey = (e: KeyboardEvent) => {
      if (e.key === "/" && !(document.activeElement instanceof HTMLInputElement) && !(document.activeElement instanceof HTMLTextAreaElement)) {
        e.preventDefault()
        inputRef.current?.focus()
        // Inject "/" into the input to trigger autocomplete
        setCommand("/")
        onCommandChange?.("/")
        const matches = matchCommands("/")
        setSuggestions(matches.slice(0, 8))
        setSelectedIdx(0)
      }
    }
    window.addEventListener("keydown", handleGlobalKey)
    return () => window.removeEventListener("keydown", handleGlobalKey)
  }, [onCommandChange])

  return (
    <div className="relative">
      {/* Energy Beams */}
      {showBeams && (
        <>
          <div
            className="absolute left-0 top-1/2 -translate-y-1/2 h-0.5 energy-beam"
            style={{ width: '40%', transform: 'translateY(-50%) translateX(-100%)', background: 'linear-gradient(270deg, var(--neural-blue), transparent)' }}
          />
          <div
            className="absolute right-0 top-1/2 -translate-y-1/2 h-0.5 energy-beam"
            style={{ width: '40%', transform: 'translateY(-50%) translateX(100%)', background: 'linear-gradient(90deg, var(--neural-blue), transparent)' }}
          />
          <div
            className="absolute left-1/2 top-0 w-0.5 energy-beam"
            style={{ height: '200%', transform: 'translateX(-50%) translateY(-100%)', background: 'linear-gradient(0deg, var(--neural-blue), transparent)' }}
          />
        </>
      )}

      <div className="holo-glass-simple py-3 px-4 corner-brackets-full relative overflow-visible">
        <div className="absolute inset-0 holo-shimmer opacity-30 pointer-events-none" />

        <div className="flex items-center gap-3 relative z-10">
          {/* Invoke Button */}
          <button
            onClick={handleInvoke}
            disabled={isInvoking}
            className={`relative shrink-0 ${isInvoking ? 'scale-90 glitch' : 'hover:scale-105'} transition-transform duration-300`}
            title={isInvoking ? 'Syncing...' : 'Invoke Core'}
          >
            {isInvoking && (
              <div className="absolute inset-0 rounded-full animate-ping"
                style={{ background: 'var(--neural-blue)', opacity: 0.2, transform: 'scale(1.2)' }}
              />
            )}
            <div className="absolute inset-0 rounded-full pulse-ring" />
            <div className={`relative w-10 h-10 rounded-full flex items-center justify-center core-glow neon-border ${isInvoking ? 'bg-[var(--neural-blue)] power-up' : 'bg-[var(--secondary)]'} border-2 border-[var(--neural-blue)] transition-colors duration-300`}>
              <div className="absolute inset-1 rounded-full border border-[var(--neural-blue)] border-dashed ring-spin opacity-50" />
              <div className="absolute inset-2 rounded-full border border-[var(--artifact-purple)] border-dotted ring-spin-reverse opacity-30" />
              <Zap size={16} className={`relative z-10 transition-colors duration-300 ${isInvoking ? 'text-[var(--deep-space-start)] fill-[var(--deep-space-start)]' : 'text-[var(--neural-blue)]'}`} />
            </div>
          </button>

          {/* Command Input with Autocomplete */}
          <form onSubmit={handleSubmit} className="flex-1 min-w-0">
            <div className="relative">
              <input
                ref={inputRef}
                type="text"
                value={command}
                onChange={e => handleInputChange(e.target.value)}
                onKeyDown={handleKeyDown}
                onBlur={() => setTimeout(() => setSuggestions([]), 150)}
                placeholder="> enter command or /help ..."
                className="w-full fui-input px-3 py-2 pr-10 font-mono text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] transition-colors"
              />
              <button
                type="submit"
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] transition-colors"
              >
                <Send size={14} />
              </button>

              {/* Autocomplete Dropdown */}
              {suggestions.length > 0 && (
                <div className="absolute left-0 right-0 bottom-full mb-1 z-50 bg-[var(--card)] border border-[var(--border)] rounded-md shadow-lg overflow-hidden max-h-[240px] overflow-y-auto">
                  {suggestions.map((cmd, idx) => {
                    const catColor = CATEGORY_COLORS[cmd.category] || "var(--muted-foreground)"
                    return (
                      <button
                        key={cmd.name}
                        type="button"
                        onMouseDown={(e) => { e.preventDefault(); selectSuggestion(cmd) }}
                        onMouseEnter={() => setSelectedIdx(idx)}
                        className={`w-full flex items-center gap-2 px-3 py-1.5 text-left transition-colors ${
                          idx === selectedIdx ? "bg-[var(--neural-blue)]/10" : "hover:bg-[var(--secondary)]"
                        }`}
                      >
                        <span className="font-mono text-[9px] px-1 py-0.5 rounded" style={{ color: catColor, backgroundColor: `color-mix(in srgb, ${catColor} 15%, transparent)` }}>
                          {cmd.category.toUpperCase().slice(0, 3)}
                        </span>
                        <span className="font-mono text-xs text-[var(--neural-blue)]">/{cmd.name}</span>
                        {cmd.args && <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{cmd.args}</span>}
                        <span className="font-mono text-[10px] text-[var(--muted-foreground)] ml-auto truncate max-w-[40%]">{cmd.description}</span>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          </form>

          {/* Status indicators */}
          <div className="flex items-center gap-2 shrink-0">
            <div className="w-2 h-2 rounded-full bg-[var(--validation-emerald)] status-dot-active" title="Ready" />
            <div className="w-2 h-2 rounded-full bg-[var(--neural-blue)] pulse-blue" title="Connected" />
          </div>
        </div>
      </div>
    </div>
  )
}
