"use client"

import { useState } from "react"
import {
  Monitor,
  Cpu,
  Bot,
  Brain,
  ListTodo,
  GitBranch,
  Activity,
  Rocket,
  Zap,
  Gauge,
  Clock3,
  ScrollText,
  BarChart3,
  Workflow,
  Sparkles,
  History,
  Shield,
  X,
  ChevronLeft,
  ChevronRight
} from "lucide-react"

// 48-Fix B: added decisions + budget so mobile users can reach the
// Autonomous Decision panels (previously desktop-only in the right aside).
// 50A: + timeline. 50B: + rules.
export type PanelId =
  | "host" | "spec" | "agents" | "orchestrator" | "tasks" | "source" | "npi" | "vitals"
  | "decisions" | "budget" | "timeline" | "rules" | "forecast" | "dag" | "intent" | "history" | "audit"
  | "pep" | "chatops"

interface MobileNavProps {
  activePanel: PanelId
  onPanelChange: (panel: PanelId) => void
}

const panels: { id: PanelId; label: string; shortLabel: string; icon: React.ElementType; color: string }[] = [
  { id: "host", label: "Host & Devices", shortLabel: "Host", icon: Monitor, color: "var(--hardware-orange)" },
  { id: "spec", label: "Spec Matrix", shortLabel: "Spec", icon: Cpu, color: "var(--neural-blue)" },
  { id: "agents", label: "Agent Matrix", shortLabel: "Agents", icon: Bot, color: "var(--validation-emerald)" },
  { id: "orchestrator", label: "Orchestrator AI", shortLabel: "AI", icon: Brain, color: "var(--artifact-purple)" },
  { id: "tasks", label: "Task Backlog", shortLabel: "Tasks", icon: ListTodo, color: "var(--neural-blue)" },
  { id: "source", label: "Source Control", shortLabel: "Git", icon: GitBranch, color: "var(--validation-emerald)" },
  { id: "npi", label: "NPI Lifecycle", shortLabel: "NPI", icon: Rocket, color: "var(--artifact-purple)" },
  { id: "vitals", label: "Vitals & Artifacts", shortLabel: "Vitals", icon: Activity, color: "var(--hardware-orange)" },
  { id: "decisions", label: "Decision Queue", shortLabel: "Decide", icon: Zap, color: "var(--neural-cyan, #67e8f9)" },
  { id: "budget", label: "Budget Strategy", shortLabel: "Budget", icon: Gauge, color: "var(--neural-blue)" },
  { id: "timeline", label: "Pipeline Timeline", shortLabel: "Timeline", icon: Clock3, color: "var(--neural-cyan, #67e8f9)" },
  { id: "rules", label: "Decision Rules", shortLabel: "Rules", icon: ScrollText, color: "var(--neural-cyan, #67e8f9)" },
  { id: "forecast", label: "Project Forecast", shortLabel: "Forecast", icon: BarChart3, color: "var(--neural-cyan, #67e8f9)" },
  { id: "dag", label: "DAG Editor", shortLabel: "DAG", icon: Workflow, color: "var(--artifact-purple)" },
  { id: "intent", label: "Spec Editor", shortLabel: "Spec", icon: Sparkles, color: "var(--artifact-purple)" },
  { id: "history", label: "Run History", shortLabel: "History", icon: History, color: "var(--neural-cyan, #67e8f9)" },
  { id: "audit", label: "Audit Log", shortLabel: "Audit", icon: Shield, color: "var(--neural-cyan, #67e8f9)" },
  { id: "pep", label: "PEP Live Feed", shortLabel: "PEP", icon: Shield, color: "var(--neural-cyan, #67e8f9)" },
  { id: "chatops", label: "ChatOps Mirror", shortLabel: "Chat", icon: Bot, color: "var(--neural-cyan, #67e8f9)" },
]

export function MobileNav({ activePanel, onPanelChange }: MobileNavProps) {
  const [isMenuOpen, setIsMenuOpen] = useState(false)
  
  const rawIndex = panels.findIndex(p => p.id === activePanel)
  // Invalid activePanel (e.g., stale URL deep-link) would otherwise crash
  // at `currentPanel.icon` below — fall back to the first panel.
  const currentIndex = rawIndex >= 0 ? rawIndex : 0
  const currentPanel = panels[currentIndex]
  
  const goToPrev = () => {
    const prevIndex = currentIndex > 0 ? currentIndex - 1 : panels.length - 1
    onPanelChange(panels[prevIndex].id)
  }
  
  const goToNext = () => {
    const nextIndex = currentIndex < panels.length - 1 ? currentIndex + 1 : 0
    onPanelChange(panels[nextIndex].id)
  }

  return (
    <>
      {/* Mobile Bottom Navigation Bar */}
      <nav className="lg:hidden fixed bottom-0 left-0 right-0 z-50 holo-glass-simple border-t border-[var(--border)] safe-area-bottom">
        <div className="flex items-center justify-between px-2 py-2">
          {/* Previous Button */}
          <button
            onClick={goToPrev}
            className="p-3 rounded-lg text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/10 transition-colors"
            aria-label="Previous panel"
          >
            <ChevronLeft size={24} />
          </button>
          
          {/* Current Panel Indicator */}
          <button
            onClick={() => setIsMenuOpen(true)}
            className="flex-1 flex items-center justify-center gap-2 py-2 px-4 rounded-lg hover:bg-[var(--holo-glass)] transition-colors"
          >
            <currentPanel.icon size={20} style={{ color: currentPanel.color }} />
            <span className="font-sans text-sm font-semibold tracking-fui" style={{ color: currentPanel.color }}>
              {currentPanel.shortLabel}
            </span>
            <span className="text-[var(--muted-foreground)] text-xs">
              ({currentIndex + 1}/{panels.length})
            </span>
          </button>
          
          {/* Next Button */}
          <button
            onClick={goToNext}
            className="p-3 rounded-lg text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/10 transition-colors"
            aria-label="Next panel"
          >
            <ChevronRight size={24} />
          </button>
        </div>
        
        {/* Quick Access Dots */}
        <div className="flex items-center justify-center gap-1.5 pb-2">
          {panels.map((panel, _idx) => (
            <button
              key={panel.id}
              onClick={() => onPanelChange(panel.id)}
              className="relative w-11 h-11 flex items-center justify-center rounded-full transition-all"
              aria-label={panel.label}
              aria-current={activePanel === panel.id ? "page" : undefined}
            >
              {/* Visual dot stays 8px; hit target is 44×44 for WCAG 2.5.5. */}
              <span
                aria-hidden
                className={`block w-2 h-2 rounded-full transition-all ${
                  activePanel === panel.id ? "scale-150" : "opacity-50"
                }`}
                style={{ backgroundColor: panel.color }}
              />
            </button>
          ))}
        </div>
      </nav>
      
      {/* Full Panel Menu Overlay */}
      {isMenuOpen && (
        <div className="lg:hidden fixed inset-0 z-[60] flex flex-col">
          {/* Backdrop */}
          <div 
            className="absolute inset-0 bg-[var(--deep-space-start)]/90 backdrop-blur-sm"
            onClick={() => setIsMenuOpen(false)}
          />
          
          {/* Menu Content */}
          <div className="relative z-10 flex flex-col h-full p-4 safe-area-top safe-area-bottom">
            {/* Header */}
            <div className="flex items-center justify-between mb-6">
              <h2 className="font-sans text-lg font-semibold tracking-fui text-[var(--neural-blue)]">
                SELECT PANEL
              </h2>
              <button
                onClick={() => setIsMenuOpen(false)}
                className="p-2 rounded-lg text-[var(--muted-foreground)] hover:text-[var(--critical-red)] hover:bg-[var(--critical-red)]/10 transition-colors"
              >
                <X size={24} />
              </button>
            </div>
            
            {/* Panel Grid */}
            <div className="flex-1 grid grid-cols-2 gap-3 overflow-auto">
              {panels.map((panel) => {
                const Icon = panel.icon
                const isActive = activePanel === panel.id
                
                return (
                  <button
                    key={panel.id}
                    onClick={() => {
                      onPanelChange(panel.id)
                      setIsMenuOpen(false)
                    }}
                    className={`
                      flex flex-col items-center justify-center gap-3 p-4 rounded-lg
                      transition-all duration-200
                      ${isActive 
                        ? "holo-glass border-2" 
                        : "bg-[var(--secondary)]/50 border border-[var(--border)] hover:border-[var(--neural-blue)]/50"
                      }
                    `}
                    style={{ 
                      borderColor: isActive ? panel.color : undefined,
                      boxShadow: isActive ? `0 0 20px ${panel.color}40` : undefined
                    }}
                  >
                    <Icon 
                      size={32} 
                      style={{ color: panel.color }}
                      className={isActive ? "animate-pulse" : ""}
                    />
                    <span 
                      className="font-sans text-sm font-semibold tracking-fui text-center"
                      style={{ color: isActive ? panel.color : "var(--foreground)" }}
                    >
                      {panel.label}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

// Tablet sidebar navigation
export function TabletNav({ activePanel, onPanelChange }: MobileNavProps) {
  return (
    <nav className="hidden md:flex lg:hidden flex-col gap-1 p-2 holo-glass-simple border-r border-[var(--border)]">
      {panels.map((panel) => {
        const Icon = panel.icon
        const isActive = activePanel === panel.id
        
        return (
          <button
            key={panel.id}
            onClick={() => onPanelChange(panel.id)}
            className={`
              flex items-center gap-2 px-3 py-2.5 rounded-lg
              transition-all duration-200
              ${isActive 
                ? "bg-[var(--holo-glass)]" 
                : "hover:bg-[var(--holo-glass)]"
              }
            `}
            style={{ 
              borderLeft: isActive ? `3px solid ${panel.color}` : "3px solid transparent"
            }}
            title={panel.label}
          >
            <Icon 
              size={20} 
              style={{ color: isActive ? panel.color : "var(--muted-foreground)" }}
            />
            <span 
              className="font-mono text-xs"
              style={{ color: isActive ? panel.color : "var(--muted-foreground)" }}
            >
              {panel.shortLabel}
            </span>
          </button>
        )
      })}
    </nav>
  )
}
