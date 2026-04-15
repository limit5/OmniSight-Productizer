"use client"

/**
 * Phase 68-C — Spec Template Editor.
 *
 * Free-form prose ↔ structured form tabs. Operator types a natural-
 * language command in the prose tab; the component calls
 * POST /intent/parse (Phase 68-A), renders the resulting ParsedSpec
 * with confidence tints + a conflict panel. The form tab surfaces
 * each field as a dropdown so an operator who prefers structured
 * input can fill it directly at confidence 1.0 — no LLM round trip,
 * no heuristic guesswork.
 *
 * When the parsed spec carries `conflicts[]`, a proposal-style
 * panel shows each conflict's message + radio options; clicking an
 * option calls POST /intent/clarify and re-renders. Fully iterative
 * — the operator can burn multiple rounds if they keep picking
 * non-resolving choices (caller's MAX_CLARIFY_ROUNDS guard is on
 * the backend side).
 *
 * The component does NOT submit to the DAG drafter. That's the
 * caller's job — typically wire `onSpecReady(spec)` to whatever
 * downstream orchestrator you want (DagEditor, raw invoke, etc.).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  AlertCircle, AlertTriangle, Code, Loader2, ListChecks, Sparkles,
} from "lucide-react"
import {
  parseIntent, clarifyIntent,
  type ParsedSpec, type IntentField, type IntentConflict,
} from "@/lib/api"

// ─── Options for the Form tab dropdowns ────────────────────────
// Match backend/intent_parser.py literal unions so picks round-trip
// cleanly through the YAML conflict rulebook.

const OPTS = {
  project_type: [
    "unknown", "embedded_firmware", "web_app", "data_pipeline",
    "research", "cli_tool",
  ],
  runtime_model: ["unknown", "ssg", "ssr", "isr", "spa", "cli", "batch"],
  target_arch: ["unknown", "x86_64", "arm64", "arm32", "riscv64"],
  target_os: ["linux", "darwin", "windows", "rtos", "unknown"],
  persistence: [
    "unknown", "sqlite", "postgres", "mysql", "redis", "flat_file", "none",
  ],
  deploy_target: ["unknown", "local", "ssh", "edge_device", "cloud"],
  hardware_required: ["unknown", "yes", "no"],
} as const

type FieldName =
  | "project_type" | "runtime_model" | "target_arch" | "target_os"
  | "framework" | "persistence" | "deploy_target" | "hardware_required"

const DEBOUNCE_MS = 600

interface Props {
  /** Optional — forwarded to downstream consumers when the operator
   * clicks "Continue". If omitted the button is hidden (component
   * still functions as a spec inspector). */
  onSpecReady?: (spec: ParsedSpec) => void
}

export function SpecTemplateEditor({ onSpecReady }: Props) {
  const [tab, setTab] = useState<"prose" | "form">("prose")
  const [text, setText] = useState("")
  const [spec, setSpec] = useState<ParsedSpec | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Cancel-previous AbortController keyed via a ref so a slow
  // parse can't clobber a newer one.
  const inflight = useRef<AbortController | null>(null)

  const runParse = useCallback(async (raw: string) => {
    if (!raw.trim()) {
      setSpec(null)
      setError(null)
      return
    }
    inflight.current?.abort()
    const ac = new AbortController()
    inflight.current = ac
    setLoading(true)
    try {
      const result = await parseIntent(raw)
      if (ac.signal.aborted) return
      setSpec(result)
      setError(null)
    } catch (exc) {
      if (ac.signal.aborted) return
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      if (!ac.signal.aborted) setLoading(false)
    }
  }, [])

  // Debounced prose → parse. The form tab edits spec directly (no
  // LLM round-trip needed for explicit picks).
  useEffect(() => {
    if (tab !== "prose") return
    const t = setTimeout(() => void runParse(text), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [text, tab, runParse])

  const patchField = (name: FieldName, value: string) => {
    if (!spec) {
      // First form edit with no prose parse — synthesize an empty
      // spec so operator doesn't need to type prose first.
      setSpec({
        project_type:      { value: "unknown",  confidence: 0 },
        runtime_model:     { value: "unknown",  confidence: 0 },
        target_arch:       { value: "unknown",  confidence: 0 },
        target_os:         { value: "linux",    confidence: 0.3 },
        framework:         { value: "unknown",  confidence: 0 },
        persistence:       { value: "unknown",  confidence: 0 },
        deploy_target:     { value: "unknown",  confidence: 0 },
        hardware_required: { value: "no",       confidence: 0.3 },
        raw_text: "",
        conflicts: [],
        [name]: { value, confidence: 1.0 },
      } as ParsedSpec)
      return
    }
    setSpec({ ...spec, [name]: { value, confidence: 1.0 } })
  }

  const onClarify = async (conflictId: string, optionId: string) => {
    if (!spec) return
    setLoading(true)
    try {
      const updated = await clarifyIntent(spec, conflictId, optionId)
      setSpec(updated)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }

  const canContinue = useMemo(() => {
    if (!spec) return false
    if (spec.conflicts.length > 0) return false
    // "Low confidence" threshold matches backend's 0.7 default.
    for (const n of [
      "project_type", "runtime_model", "target_arch", "target_os",
      "framework", "persistence", "deploy_target",
    ] as FieldName[]) {
      if ((spec as any)[n].confidence < 0.7) return false
    }
    return true
  }, [spec])

  return (
    <div className="flex flex-col gap-3 p-3 rounded-lg bg-[var(--card)] border border-[var(--border)]">
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-mono text-sm font-semibold text-[var(--foreground)] flex items-center gap-2">
          <Sparkles size={14} className="text-[var(--artifact-purple)]" />
          Spec Editor
        </h3>
        <div className="flex items-center gap-2">
          <div role="tablist" aria-label="Editor mode" className="flex rounded border border-[var(--border)] overflow-hidden">
            <button
              type="button" role="tab"
              aria-selected={tab === "prose"}
              onClick={() => setTab("prose")}
              className={`text-xs font-mono px-2 py-0.5 flex items-center gap-1 ${
                tab === "prose"
                  ? "bg-[var(--artifact-purple)] text-white"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]"
              }`}
            >
              <Code size={10} /> Prose
            </button>
            <button
              type="button" role="tab"
              aria-selected={tab === "form"}
              onClick={() => setTab("form")}
              className={`text-xs font-mono px-2 py-0.5 flex items-center gap-1 ${
                tab === "form"
                  ? "bg-[var(--artifact-purple)] text-white"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]"
              }`}
            >
              <ListChecks size={10} /> Form
            </button>
          </div>
          {loading && <Loader2 size={12} className="animate-spin text-[var(--muted-foreground)]" />}
        </div>
      </div>

      {/* Prose tab */}
      {tab === "prose" && (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Describe the project. Example: Build a Next.js static site that reads from a local SQLite at build time, deploy on x86_64."
          aria-label="Project prose"
          className="w-full min-h-[120px] max-h-[240px] font-mono text-xs leading-relaxed p-2 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)] resize-y"
        />
      )}

      {/* Form tab */}
      {tab === "form" && (
        <div className="grid grid-cols-2 gap-2">
          {(["project_type", "runtime_model", "target_arch", "target_os",
             "persistence", "deploy_target", "hardware_required"] as FieldName[])
            .map((name) => (
              <FieldDropdown
                key={name}
                name={name}
                options={(OPTS as any)[name]}
                current={spec ? (spec as any)[name] as IntentField : null}
                onChange={(v) => patchField(name, v)}
              />
            ))}
          {/* Framework is free-form — many frameworks exist outside
              any enum. Separate input so operator can type e.g. "axum". */}
          <label className="flex flex-col gap-0.5 col-span-2">
            <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--muted-foreground)]">
              framework
              {spec && <ConfidenceBadge conf={spec.framework.confidence} />}
            </span>
            <input
              type="text"
              value={spec?.framework.value === "unknown" ? "" : spec?.framework.value || ""}
              onChange={(e) => patchField("framework", e.target.value || "unknown")}
              placeholder="nextjs, django, axum, ..."
              className="text-xs font-mono px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
            />
          </label>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs">
          <AlertCircle size={12} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Conflict panels */}
      {spec?.conflicts.map((c) => (
        <ConflictPanel
          key={c.id}
          conflict={c}
          onPick={(optId) => void onClarify(c.id, optId)}
        />
      ))}

      {/* Continue button — caller decides what "continue" means */}
      {onSpecReady && spec && (
        <div className="flex items-center justify-between gap-2 pt-1 border-t border-[var(--border)]">
          <span className="text-[10px] font-mono text-[var(--muted-foreground)]">
            {spec.conflicts.length > 0
              ? `${spec.conflicts.length} conflict${spec.conflicts.length > 1 ? "s" : ""} to resolve first`
              : canContinue
                ? "Ready"
                : "Some fields still have low confidence — fill them in the Form tab"}
          </span>
          <button
            type="button"
            onClick={() => onSpecReady(spec)}
            disabled={!canContinue}
            className="text-xs font-mono px-3 py-1 rounded bg-[var(--artifact-purple)] text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Continue
          </button>
        </div>
      )}
    </div>
  )
}

// ─── Subcomponents ──────────────────────────────────────────────

function FieldDropdown({
  name, options, current, onChange,
}: {
  name: FieldName
  options: readonly string[]
  current: IntentField | null
  onChange: (value: string) => void
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--muted-foreground)] flex items-center gap-1">
        {name}
        {current && <ConfidenceBadge conf={current.confidence} />}
      </span>
      <select
        value={current?.value || "unknown"}
        onChange={(e) => onChange(e.target.value)}
        aria-label={name}
        className="text-xs font-mono px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)]"
      >
        {options.map((v) => (
          <option key={v} value={v}>{v}</option>
        ))}
      </select>
    </label>
  )
}

function ConfidenceBadge({ conf }: { conf: number }) {
  // Tint + label mirror the backend's threshold (0.7 default).
  let color = "var(--muted-foreground)"
  let label = `${Math.round(conf * 100)}%`
  if (conf === 0) {
    color = "var(--muted-foreground)"
    label = "?"
  } else if (conf < 0.5) {
    color = "var(--destructive)"
  } else if (conf < 0.7) {
    color = "var(--fui-orange, #f59e0b)"
  } else if (conf < 1.0) {
    color = "var(--validation-emerald, #10b981)"
  } else {
    color = "var(--neural-cyan, #67e8f9)"
    label = "✓"  // operator-set confidence
  }
  return (
    <span
      className="text-[9px] font-mono px-1 rounded-sm"
      style={{ color, background: `${color}22` }}
      title={`confidence ${Math.round(conf * 100)}%`}
    >
      {label}
    </span>
  )
}

function ConflictPanel({
  conflict, onPick,
}: {
  conflict: IntentConflict
  onPick: (optionId: string) => void
}) {
  const priorId = conflict.prior_choice?.option_id
  return (
    <div className="rounded border border-[var(--fui-orange,#f59e0b)] bg-[var(--fui-orange,#f59e0b)]/10 p-2">
      <div className="flex items-start gap-2">
        <AlertTriangle size={14} className="shrink-0 text-[var(--fui-orange,#f59e0b)] mt-0.5" />
        <div className="flex-1">
          <div className="font-mono text-[10px] tracking-wider uppercase text-[var(--fui-orange,#f59e0b)] mb-0.5">
            {conflict.id}
          </div>
          <div className="text-xs text-[var(--foreground)] whitespace-pre-line mb-2">
            {conflict.message}
          </div>
          {priorId && (
            // Phase 68-D: prior-choice hint. Small + clear that it's
            // a suggestion, not an auto-apply. Clicking still counts
            // as a fresh decision that gets its own L3 row, so
            // repeated picks naturally strengthen the signal via
            // Phase 63-E decay rather than a special counter.
            <div
              className="text-[10px] font-mono text-[var(--neural-cyan,#67e8f9)] mb-1.5"
              aria-label="prior choice suggestion"
            >
              💡 Last time you picked this option
            </div>
          )}
          <div className="flex flex-col gap-1">
            {conflict.options.map((opt) => {
              const isPrior = opt.id === priorId
              return (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => onPick(opt.id)}
                  className={
                    "text-left text-xs font-mono px-2 py-1 rounded border text-[var(--foreground)] " +
                    (isPrior
                      ? "border-[var(--neural-cyan,#67e8f9)] bg-[var(--neural-cyan,#67e8f9)]/10 hover:bg-[var(--neural-cyan,#67e8f9)]/20"
                      : "border-[var(--border)] hover:bg-[var(--muted)]")
                  }
                  title={opt.desc || ""}
                  data-prior={isPrior || undefined}
                >
                  <span className="font-semibold flex items-center gap-1">
                    {isPrior && <span aria-hidden>⭐</span>}
                    {opt.label}
                  </span>
                  {opt.desc && (
                    <div className="text-[10px] text-[var(--muted-foreground)] mt-0.5">
                      {opt.desc}
                    </div>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
