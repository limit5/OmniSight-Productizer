"use client"

import { useState, useEffect } from "react"
import { 
  ChevronDown, 
  ChevronUp, 
  Coins, 
  TrendingUp, 
  BarChart3,
  Zap,
  DollarSign,
  Clock,
  ArrowUpRight,
  ArrowDownRight
} from "lucide-react"
import { AI_MODEL_INFO, type AIModel } from "./agent-matrix-wall"

// Token usage data per model
export interface ModelTokenUsage {
  model: AIModel
  inputTokens: number
  outputTokens: number
  totalTokens: number
  cost: number
  requestCount: number
  avgLatency: number // ms
  lastUsed: string
}

// Pricing per 1M tokens (input/output)
const MODEL_PRICING: Record<AIModel, { input: number; output: number }> = {
  "claude-opus-4.6": { input: 15, output: 75 },
  "claude-sonnet-4.8": { input: 3, output: 15 },
  "gpt-5.4": { input: 5, output: 15 },
  "gemini-3.1": { input: 0.5, output: 1.5 },
  "gemma-4": { input: 0.1, output: 0.3 },
  "grok-3": { input: 2, output: 10 },
  "codex-2": { input: 3, output: 12 },
  "mistral-large": { input: 2, output: 6 },
  "llama-4": { input: 0.2, output: 0.6 },
}

// Empty state — no LLM calls made yet
function emptyUsage(): ModelTokenUsage[] {
  return []
}

// Format large numbers with K/M suffix
function formatTokens(num: number): string {
  if (num >= 1000000) {
    return (num / 1000000).toFixed(2) + "M"
  }
  if (num >= 1000) {
    return (num / 1000).toFixed(1) + "K"
  }
  return num.toString()
}

// Format cost with appropriate precision
function formatCost(cost: number): string {
  if (cost >= 1) {
    return "$" + cost.toFixed(2)
  }
  return "$" + cost.toFixed(3)
}

interface TokenUsageStatsProps {
  className?: string
  externalUsage?: ModelTokenUsage[]
}

export function TokenUsageStats({ className = "", externalUsage }: TokenUsageStatsProps) {
  const [expanded, setExpanded] = useState(true)
  const [usageData, setUsageData] = useState<ModelTokenUsage[]>(externalUsage ?? emptyUsage())
  const [selectedModel, setSelectedModel] = useState<AIModel | null>(null)

  // Sync from backend when available
  useEffect(() => {
    if (externalUsage && externalUsage.length > 0) {
      setUsageData(externalUsage)
    }
  }, [externalUsage])

  // No simulation — real data comes from backend via externalUsage prop
  useEffect(() => {
  }, [])
  
  // Calculate totals
  const totals = usageData.reduce((acc, item) => ({
    inputTokens: acc.inputTokens + item.inputTokens,
    outputTokens: acc.outputTokens + item.outputTokens,
    totalTokens: acc.totalTokens + item.totalTokens,
    cost: acc.cost + item.cost,
    requestCount: acc.requestCount + item.requestCount,
  }), { inputTokens: 0, outputTokens: 0, totalTokens: 0, cost: 0, requestCount: 0 })
  
  // Sort by total tokens descending
  const sortedData = [...usageData].sort((a, b) => b.totalTokens - a.totalTokens)

  return (
    <div className={`border-b border-[var(--border)] ${className}`}>
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full px-4 py-2 flex items-center justify-between text-xs font-mono text-[var(--muted-foreground)] hover:bg-[var(--secondary)]/50 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Coins size={12} className="text-[var(--hardware-orange)]" />
          <span>TOKEN USAGE</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[var(--validation-emerald)]">{formatTokens(totals.totalTokens)} tokens</span>
          <span className="text-[var(--hardware-orange)]">{formatCost(totals.cost)}</span>
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </div>
      </button>
      
      {expanded && (
        <div className="px-3 pb-3">
          {/* Summary Stats - Vertical Stack */}
          <div className="space-y-2 mb-3">
            {/* Total Tokens */}
            <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <BarChart3 size={14} className="text-[var(--neural-blue)] shrink-0" />
                  <span className="font-mono text-xs text-[var(--muted-foreground)]">Total Tokens</span>
                </div>
                <p className="font-mono text-sm font-semibold text-[var(--foreground)]">{formatTokens(totals.totalTokens)}</p>
              </div>
              <div className="flex items-center justify-between mt-1.5 pl-6">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Input: <span className="text-[var(--validation-emerald)]">{formatTokens(totals.inputTokens)}</span>
                </span>
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Output: <span className="text-[var(--neural-blue)]">{formatTokens(totals.outputTokens)}</span>
                </span>
              </div>
            </div>
            
            {/* Total Cost */}
            <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <DollarSign size={14} className="text-[var(--hardware-orange)] shrink-0" />
                  <span className="font-mono text-xs text-[var(--muted-foreground)]">Total Cost</span>
                </div>
                <p className="font-mono text-sm font-semibold text-[var(--hardware-orange)]">{formatCost(totals.cost)}</p>
              </div>
              <div className="flex items-center justify-between mt-1.5 pl-6">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Avg per Request: <span className="text-[var(--foreground)]">{formatCost(totals.cost / totals.requestCount)}</span>
                </span>
              </div>
            </div>
            
            {/* Requests + Active Models Row */}
            <div className="grid grid-cols-2 gap-2">
              <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Zap size={14} className="text-[var(--artifact-purple)] shrink-0" />
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Requests</span>
                  </div>
                  <p className="font-mono text-sm font-semibold text-[var(--foreground)]">{totals.requestCount}</p>
                </div>
              </div>
              <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <TrendingUp size={14} className="text-[var(--validation-emerald)] shrink-0" />
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Models</span>
                  </div>
                  <p className="font-mono text-sm font-semibold text-[var(--foreground)]">{usageData.length}</p>
                </div>
              </div>
            </div>
          </div>
          
          {/* Per-Model Usage */}
          <div className="space-y-2">
            {sortedData.map(item => {
              const modelInfo = AI_MODEL_INFO[item.model]
              const isSelected = selectedModel === item.model
              const usagePercent = (item.totalTokens / totals.totalTokens) * 100
              
              return (
                <button
                  key={item.model}
                  onClick={() => setSelectedModel(isSelected ? null : item.model)}
                  className={`w-full text-left p-3 rounded-lg transition-all ${
                    isSelected 
                      ? "bg-[var(--artifact-purple)]/20 ring-1 ring-[var(--artifact-purple)]"
                      : "bg-[var(--secondary)] hover:bg-[var(--secondary)]/80"
                  }`}
                >
                  {/* Row 1: Model Name + Provider + Cost */}
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <div 
                        className="w-3 h-3 rounded-full shrink-0"
                        style={{ backgroundColor: modelInfo.color }}
                      />
                      <span className="font-mono text-xs font-medium text-[var(--foreground)]">
                        {modelInfo.label}
                      </span>
                    </div>
                    <span 
                      className="font-mono text-xs font-semibold px-2 py-0.5 rounded"
                      style={{ 
                        backgroundColor: `color-mix(in srgb, ${modelInfo.color} 20%, transparent)`,
                        color: modelInfo.color 
                      }}
                    >
                      {formatCost(item.cost)}
                    </span>
                  </div>
                  
                  {/* Row 2: Provider + Usage Percentage */}
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                      Provider: {modelInfo.provider}
                    </span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                      {usagePercent.toFixed(1)}% of total usage
                    </span>
                  </div>
                  
                  {/* Row 3: Usage Bar */}
                  <div className="h-2 rounded-full bg-[var(--border)] overflow-hidden mb-3">
                    <div 
                      className="h-full rounded-full transition-all duration-500"
                      style={{ 
                        width: `${usagePercent}%`,
                        backgroundColor: modelInfo.color
                      }}
                    />
                  </div>
                  
                  {/* Row 4: Input/Output Tokens */}
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-1.5">
                      <ArrowDownRight size={12} className="text-[var(--validation-emerald)]" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Input Tokens:</span>
                      <span className="font-mono text-[11px] font-medium text-[var(--validation-emerald)]">
                        {formatTokens(item.inputTokens)}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <ArrowUpRight size={12} className="text-[var(--neural-blue)]" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Output Tokens:</span>
                      <span className="font-mono text-[11px] font-medium text-[var(--neural-blue)]">
                        {formatTokens(item.outputTokens)}
                      </span>
                    </div>
                  </div>
                  
                  {/* Row 5: Total Tokens */}
                  <div className="flex items-center justify-between mb-2 pb-2 border-b border-[var(--border)]/50">
                    <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Total Tokens:</span>
                    <span className="font-mono text-[11px] font-medium text-[var(--foreground)]">
                      {formatTokens(item.totalTokens)}
                    </span>
                  </div>
                  
                  {/* Row 6: Request Count */}
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-1.5">
                      <Zap size={11} className="text-[var(--artifact-purple)] shrink-0" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Requests:</span>
                    </div>
                    <span className="font-mono text-[11px] font-medium text-[var(--foreground)]">
                      {item.requestCount}
                    </span>
                  </div>
                  
                  {/* Row 7: Average Latency */}
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-1.5">
                      <Clock size={11} className="text-[var(--hardware-orange)] shrink-0" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Avg Latency:</span>
                    </div>
                    <span className="font-mono text-[11px] font-medium text-[var(--hardware-orange)]">
                      {item.avgLatency}ms
                    </span>
                  </div>
                  
                  {/* Row 8: Last Used */}
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Last Used:</span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                      {item.lastUsed}
                    </span>
                  </div>
                  
                  {/* Expanded Details */}
                  {isSelected && (
                    <div className="mt-3 pt-3 border-t border-[var(--border)]">
                      <p className="font-mono text-[10px] text-[var(--muted-foreground)] mb-2 uppercase tracking-wider">Pricing Details</p>
                      <div className="grid grid-cols-1 gap-1.5 text-[11px] font-mono">
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Input Price (per 1M tokens):</span>
                          <span className="text-[var(--foreground)]">${MODEL_PRICING[item.model].input.toFixed(2)}</span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Output Price (per 1M tokens):</span>
                          <span className="text-[var(--foreground)]">${MODEL_PRICING[item.model].output.toFixed(2)}</span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Average Tokens per Request:</span>
                          <span className="text-[var(--foreground)]">{Math.round(item.totalTokens / item.requestCount).toLocaleString()}</span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Average Cost per Request:</span>
                          <span className="text-[var(--foreground)]">{formatCost(item.cost / item.requestCount)}</span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Input/Output Ratio:</span>
                          <span className="text-[var(--foreground)]">{(item.inputTokens / item.outputTokens).toFixed(2)}:1</span>
                        </div>
                      </div>
                    </div>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
