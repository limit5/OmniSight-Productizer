"use client"

/**
 * Phase 50B — Decision Rules Editor.
 *
 * CRUD the in-memory rule list + dry-run against a comma-separated list
 * of sample kinds. Rows are priority-ordered; the +/- buttons nudge a
 * row up or down (whole-list PUT keeps the backend lock-free).
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  FlaskConical,
  Plus,
  Save,
  ScrollText,
  Trash2,
} from "lucide-react"
import {
  type DecisionRule,
  type DecisionRulesTestHit,
  type DecisionSeverity,
  type OperationMode,
  getDecisionRules,
  putDecisionRules,
  testDecisionRules,
} from "@/lib/api"

function blankRule(priority: number): DecisionRule {
  return {
    id: `tmp-${Math.random().toString(36).slice(2, 10)}`,
    kind_pattern: "",
    severity: null,
    auto_in_modes: [],
    default_option_id: null,
    priority,
    enabled: true,
    note: "",
  }
}

export function DecisionRulesEditor() {
  const [rules, setRules] = useState<DecisionRule[]>([])
  const [severities, setSeverities] = useState<DecisionSeverity[]>([])
  const [modes, setModes] = useState<OperationMode[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [testKinds, setTestKinds] = useState("")
  const [testMode, setTestMode] = useState<OperationMode | "">("")
  const [testHits, setTestHits] = useState<DecisionRulesTestHit[] | null>(null)

  const refresh = useCallback(async () => {
    try {
      const info = await getDecisionRules()
      setRules(info.rules)
      setSeverities(info.severities)
      setModes(info.modes)
      setDirty(false)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const sorted = useMemo(
    () => [...rules].sort((a, b) => a.priority - b.priority),
    [rules],
  )

  const updateRule = (id: string, patch: Partial<DecisionRule>) => {
    setRules((cur) => cur.map((r) => (r.id === id ? { ...r, ...patch } : r)))
    setDirty(true)
  }

  const moveRule = (id: string, direction: -1 | 1) => {
    setRules((cur) => {
      const ordered = [...cur].sort((a, b) => a.priority - b.priority)
      const idx = ordered.findIndex((r) => r.id === id)
      const swapIdx = idx + direction
      if (idx < 0 || swapIdx < 0 || swapIdx >= ordered.length) return cur
      const a = ordered[idx]
      const b = ordered[swapIdx]
      // Swap priorities — stable two-number swap keeps the list compact.
      ordered[idx] = { ...a, priority: b.priority }
      ordered[swapIdx] = { ...b, priority: a.priority }
      return ordered
    })
    setDirty(true)
  }

  const addRule = () => {
    const maxP = rules.reduce((m, r) => Math.max(m, r.priority), 0)
    setRules((cur) => [...cur, blankRule(maxP + 10)])
    setDirty(true)
  }

  const removeRule = (id: string) => {
    setRules((cur) => cur.filter((r) => r.id !== id))
    setDirty(true)
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      // Strip local tmp- ids so the backend mints real ones for new rows.
      const payload = sorted.map((r) => {
        const { id, ...rest } = r
        return id.startsWith("tmp-") ? rest : r
      })
      const res = await putDecisionRules(payload)
      setRules(res.rules)
      setDirty(false)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const runTest = async () => {
    const kinds = testKinds
      .split(",").map((s) => s.trim()).filter(Boolean)
    if (!kinds.length) { setTestHits([]); return }
    try {
      const res = await testDecisionRules(
        kinds,
        testMode || undefined,
      )
      setTestHits(res.hits)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Decision Rules"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <ScrollText className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            DECISION RULES
          </h2>
          <span className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
            {rules.length} {rules.length === 1 ? "rule" : "rules"}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={addRule}
            className="font-mono text-[10px] tracking-wider px-2 py-0.5 rounded-sm border border-[var(--neural-cyan,#67e8f9)] text-[var(--neural-cyan,#67e8f9)] hover:bg-[var(--neural-cyan,#67e8f9)]/10 flex items-center gap-1"
            aria-label="add rule"
          >
            <Plus className="w-3 h-3" aria-hidden /> ADD
          </button>
          <button
            onClick={() => void save()}
            disabled={!dirty || busy}
            className={`font-mono text-[10px] tracking-wider px-2 py-0.5 rounded-sm border flex items-center gap-1 ${
              dirty && !busy
                ? "border-[var(--validation-emerald,#10b981)] text-[var(--validation-emerald,#10b981)] hover:bg-[var(--validation-emerald,#10b981)]/10"
                : "border-[var(--neural-border,rgba(148,163,184,0.35))] text-[var(--muted-foreground,#94a3b8)] cursor-not-allowed"
            }`}
          >
            <Save className="w-3 h-3" aria-hidden /> {busy ? "SAVING" : "SAVE"}
          </button>
        </div>
      </header>

      {error && (
        <div className="px-3 py-1.5 flex items-center gap-2 font-mono text-[10px] text-[var(--critical-red,#ef4444)]" role="alert">
          <AlertTriangle className="w-3 h-3 shrink-0" aria-hidden />
          <span className="truncate">{error}</span>
        </div>
      )}

      <ul className="divide-y divide-[var(--neural-border,rgba(148,163,184,0.35))]">
        {sorted.length === 0 && (
          <li className="px-3 py-4 font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)] text-center">
            no rules — ADD one to steer decisions before they reach the queue
          </li>
        )}
        {sorted.map((r, idx) => (
          <li
            key={r.id}
            data-testid={`rule-row-${r.id}`}
            className={`grid grid-cols-[auto_1fr_auto] gap-2 items-start px-2 py-2 ${
              r.enabled ? "" : "opacity-55"
            }`}
          >
            {/* Reorder + index */}
            <div className="flex flex-col items-center gap-0.5 pt-1">
              <button
                onClick={() => moveRule(r.id, -1)}
                disabled={idx === 0}
                aria-label="move up"
                className="p-0.5 text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] disabled:opacity-30"
              >
                <ArrowUp className="w-3 h-3" aria-hidden />
              </button>
              <span className="font-mono text-[9px] tabular-nums text-[var(--muted-foreground,#94a3b8)]">
                {String(idx + 1).padStart(2, "0")}
              </span>
              <button
                onClick={() => moveRule(r.id, 1)}
                disabled={idx === sorted.length - 1}
                aria-label="move down"
                className="p-0.5 text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] disabled:opacity-30"
              >
                <ArrowDown className="w-3 h-3" aria-hidden />
              </button>
            </div>

            {/* Fields */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
              <label className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                KIND PATTERN
                <input
                  value={r.kind_pattern}
                  onChange={(e) => updateRule(r.id, { kind_pattern: e.target.value })}
                  placeholder="e.g. stuck/*"
                  className="mt-0.5 w-full bg-[var(--background,#020617)]/60 border border-[var(--neural-border,rgba(148,163,184,0.35))] rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-[var(--foreground,#e2e8f0)] focus:outline-none focus:border-[var(--neural-cyan,#67e8f9)]"
                />
              </label>
              <label className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                SEVERITY
                <select
                  value={r.severity ?? ""}
                  onChange={(e) => updateRule(r.id, { severity: (e.target.value || null) as DecisionSeverity | null })}
                  className="mt-0.5 w-full bg-[var(--background,#020617)]/60 border border-[var(--neural-border,rgba(148,163,184,0.35))] rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-[var(--foreground,#e2e8f0)]"
                >
                  <option value="">— keep —</option>
                  {severities.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </label>
              <label className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                DEFAULT OPTION ID
                <input
                  value={r.default_option_id ?? ""}
                  onChange={(e) => updateRule(r.id, { default_option_id: e.target.value || null })}
                  placeholder="(leave empty to keep)"
                  className="mt-0.5 w-full bg-[var(--background,#020617)]/60 border border-[var(--neural-border,rgba(148,163,184,0.35))] rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-[var(--foreground,#e2e8f0)]"
                />
              </label>
              <label className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                AUTO IN MODES
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {modes.map((m) => {
                    const picked = r.auto_in_modes.includes(m)
                    return (
                      <button
                        key={m}
                        type="button"
                        onClick={() => updateRule(r.id, {
                          auto_in_modes: picked
                            ? r.auto_in_modes.filter((x) => x !== m)
                            : [...r.auto_in_modes, m],
                        })}
                        aria-pressed={picked}
                        className={`px-1.5 py-0.5 rounded-sm border font-mono text-[10px] tracking-wider ${
                          picked
                            ? "border-[var(--neural-cyan,#67e8f9)] text-[var(--neural-cyan,#67e8f9)] bg-[var(--neural-cyan,#67e8f9)]/10"
                            : "border-[var(--neural-border,rgba(148,163,184,0.35))] text-[var(--muted-foreground,#94a3b8)]"
                        }`}
                      >
                        {m}
                      </button>
                    )
                  })}
                </div>
              </label>
              <label className="col-span-1 sm:col-span-2 font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
                NOTE
                <input
                  value={r.note}
                  onChange={(e) => updateRule(r.id, { note: e.target.value })}
                  placeholder="why this rule exists — operators will see it"
                  className="mt-0.5 w-full bg-[var(--background,#020617)]/60 border border-[var(--neural-border,rgba(148,163,184,0.35))] rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-[var(--foreground,#e2e8f0)]"
                />
              </label>
            </div>

            {/* Toggle + delete */}
            <div className="flex flex-col items-end gap-1">
              <label className="flex items-center gap-1 font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={r.enabled}
                  onChange={(e) => updateRule(r.id, { enabled: e.target.checked })}
                  className="accent-[var(--neural-cyan,#67e8f9)]"
                />
                ENABLED
              </label>
              <button
                onClick={() => removeRule(r.id)}
                aria-label="delete rule"
                className="p-0.5 text-[var(--critical-red,#ef4444)] hover:bg-[var(--critical-red,#ef4444)]/10 rounded-sm"
              >
                <Trash2 className="w-3.5 h-3.5" aria-hidden />
              </button>
            </div>
          </li>
        ))}
      </ul>

      {/* Dry-run */}
      <div className="border-t border-[var(--neural-border,rgba(148,163,184,0.35))] p-2 flex flex-col gap-1.5">
        <div className="flex items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
          <FlaskConical className="w-3 h-3 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <span>TEST AGAINST KINDS</span>
        </div>
        <div className="flex flex-col sm:flex-row gap-1.5 items-stretch sm:items-center">
          <input
            value={testKinds}
            onChange={(e) => setTestKinds(e.target.value)}
            placeholder="stuck/loop, ambiguity/spec, …"
            className="flex-1 bg-[var(--background,#020617)]/60 border border-[var(--neural-border,rgba(148,163,184,0.35))] rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-[var(--foreground,#e2e8f0)] focus:outline-none focus:border-[var(--neural-cyan,#67e8f9)]"
          />
          <select
            value={testMode}
            onChange={(e) => setTestMode(e.target.value as OperationMode | "")}
            aria-label="test mode"
            className="bg-[var(--background,#020617)]/60 border border-[var(--neural-border,rgba(148,163,184,0.35))] rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-[var(--foreground,#e2e8f0)]"
          >
            <option value="">current mode</option>
            {modes.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          <button
            onClick={() => void runTest()}
            className="font-mono text-[10px] tracking-wider px-2 py-0.5 rounded-sm border border-[var(--neural-cyan,#67e8f9)] text-[var(--neural-cyan,#67e8f9)] hover:bg-[var(--neural-cyan,#67e8f9)]/10"
          >
            RUN
          </button>
        </div>
        {testHits && (
          <ul aria-label="test results" className="font-mono text-[10px] text-[var(--foreground,#e2e8f0)] space-y-0.5">
            {testHits.map((h) => (
              <li key={h.kind} className="flex items-center gap-2">
                <span className="truncate flex-1">{h.kind}</span>
                {h.rule_id ? (
                  <span className="text-[var(--validation-emerald,#10b981)]">
                    → {h.rule_id}{h.severity ? ` · ${h.severity}` : ""}{h.auto ? " · AUTO" : ""}
                  </span>
                ) : (
                  <span className="text-[var(--muted-foreground,#94a3b8)]">no match</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
