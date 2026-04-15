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

// ─── Starter prose templates ───────────────────────────────────
// Same idea as DAG-E's TEMPLATES list: spare the operator from
// staring at an empty textarea. Each template is a real, parseable
// sentence that the heuristic + LLM both extract well; click sets
// `text`, the existing debounced effect parses it, and the operator
// can edit before clicking Continue.
//
// Mix of CJK and English on purpose — operators in this codebase
// type both. The intent_parser regex patterns cover both.

interface SpecTemplate { id: string; label: string; prose: string }

const SPEC_TEMPLATES: SpecTemplate[] = [
  {
    id: "web_ssg",
    label: "Web · SSG",
    prose: "Build a Next.js static site on x86_64 that reads from a local SQLite at build time, deploy locally with nginx.",
  },
  {
    id: "web_ssr",
    label: "Web · SSR",
    prose: "FastAPI backend on x86_64 with PostgreSQL, deploy to a cloud VM. SSR React frontend talks to it at request time.",
  },
  {
    id: "embedded_arm64",
    label: "Embedded · arm64",
    prose: "Write an arm64 firmware driver for the IMX335 sensor over MIPI CSI, target a Raspberry Pi 4 with FreeRTOS, includes I2C and SPI peripheral access.",
  },
  {
    id: "data_pipeline",
    label: "Data Pipeline",
    prose: "Batch processing pipeline in Python: read CSVs from local flat files, transform with pandas, write parquet output. Runs on x86_64.",
  },
  {
    id: "cli_tool",
    label: "CLI Tool",
    prose: "Rust CLI tool to scan a directory tree and report duplicate files. x86_64 native, no DB.",
  },
  {
    id: "research",
    label: "Research / Notebook",
    prose: "Jupyter notebook for data analysis on x86_64. Reads from local SQLite, no deployment target needed.",
  },
  {
    id: "embedded_static",
    label: "Embedded Static UI",
    prose: "x86_64 工業電腦上跑 Next.js 靜態網頁，從本地 SQLite 在 build time 讀資料展示。",
  },
]

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

// localStorage key for the last spec the operator handed off via
// Continue. Picked back up on mount so a "Back to Spec" jump (from
// a failed DAG submit) lands the operator exactly where they were,
// not on a blank prose textarea. Survives reload; cleared after a
// fresh handoff so stale state can't shadow new intent.
const LS_LAST_SPEC = "omnisight:intent:last_spec"

interface DagFailureContext {
  reason: string
  rules: string[]
  target_platform: string | null
}

export function SpecTemplateEditor({ onSpecReady }: Props) {
  const [tab, setTab] = useState<"prose" | "form">("prose")
  const [text, setText] = useState("")
  const [spec, setSpec] = useState<ParsedSpec | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Phase 68 → DAG round-trip: failure context captured on a
  // back-jump from DagEditor. Cleared once the operator types or
  // picks anything (intent has changed, banner is stale).
  const [failure, setFailure] = useState<DagFailureContext | null>(null)
  // Hydration flag: localStorage isn't accessible during SSR, so we
  // restore on mount and then never write before the first user-
  // initiated change. Avoids overwriting valid prose with empty
  // string during the first render pass.
  const [hydrated, setHydrated] = useState(false)

  // Cancel-previous AbortController keyed via a ref so a slow
  // parse can't clobber a newer one.
  const inflight = useRef<AbortController | null>(null)

  // ─── DAG → Spec back-jump context ──────────────────────────────
  // DagEditor dispatches `omnisight:spec-failure-context` right
  // before navigating us, so we already have the reason in state
  // when the panel renders. Listening on window means we're
  // robust to event arrival order (event before mount or after).
  useEffect(() => {
    if (typeof window === "undefined") return
    const onFailure = (e: Event) => {
      const detail = (e as CustomEvent<DagFailureContext>).detail
      if (!detail) return
      setFailure(detail)
    }
    window.addEventListener("omnisight:spec-failure-context", onFailure as EventListener)
    return () =>
      window.removeEventListener("omnisight:spec-failure-context", onFailure as EventListener)
  }, [])

  // ─── Persistence: restore prior session on mount ───────────────
  useEffect(() => {
    if (typeof window === "undefined") return
    try {
      const raw = window.localStorage.getItem(LS_LAST_SPEC)
      if (raw) {
        const cached = JSON.parse(raw) as ParsedSpec
        setSpec(cached)
        setText(cached.raw_text || "")
      }
    } catch (exc) {
      // Bad JSON / quota issues — silently start fresh.
      console.debug("[SpecTemplateEditor] restore failed:", exc)
    }
    setHydrated(true)
  }, [])

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
    // Operator changed something — failure context goes stale.
    if (failure) setFailure(null)
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
        <div className="flex flex-col gap-2">
          {/* Template chip row — same UX as DAG-E's gallery. Empty
              textarea is the worst onboarding moment; one click
              gets the operator a parseable starting point. */}
          <div className="flex flex-wrap gap-1">
            {SPEC_TEMPLATES.map((tpl) => (
              <button
                key={tpl.id}
                type="button"
                onClick={() => setText(tpl.prose)}
                title={tpl.prose}
                className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--muted)] text-[var(--muted-foreground)]"
              >
                {tpl.label}
              </button>
            ))}
          </div>
          <textarea
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              if (failure) setFailure(null)
            }}
            placeholder="Describe the project, or click a template chip above to start."
            aria-label="Project prose"
            className="w-full min-h-[120px] max-h-[240px] font-mono text-xs leading-relaxed p-2 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)] resize-y"
          />
        </div>
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

      {/* DAG failure context banner — shown when the operator
          back-jumped from a failed DagEditor submit. Auto-clears on
          any edit (typing in prose or picking a form field), so an
          intentional re-clarification doesn't leave stale yellow on
          screen. Hint suggests which fields likely need attention
          based on the rule names that fired. */}
      {failure && (
        <div
          role="alert"
          aria-label="DAG failure context"
          className="flex items-start gap-2 p-2 rounded border border-[var(--fui-orange,#f59e0b)] bg-[var(--fui-orange,#f59e0b)]/10 text-xs font-mono"
        >
          <AlertCircle size={12} className="shrink-0 mt-0.5 text-[var(--fui-orange,#f59e0b)]" />
          <div className="flex-1 min-w-0">
            <div className="text-[var(--fui-orange,#f59e0b)] font-semibold mb-0.5">
              Last DAG submit failed — re-clarify and try again
            </div>
            <div className="text-[var(--foreground)] break-words mb-1">
              {failure.reason}
            </div>
            {failure.rules.length > 0 && (
              <div className="text-[10px] text-[var(--muted-foreground)]">
                Rules: {failure.rules.join(", ")}
                {failure.rules.includes("tier_violation") && (
                  <> · likely fix: check <strong>target_arch</strong> / <strong>hardware_required</strong> below</>
                )}
                {failure.rules.includes("io_entity") && (
                  <> · likely fix: check <strong>persistence</strong> / <strong>deploy_target</strong> path shape</>
                )}
              </div>
            )}
            {failure.target_platform && (
              <div className="text-[10px] text-[var(--muted-foreground)]">
                Submitted with target_platform=<strong>{failure.target_platform}</strong>
              </div>
            )}
          </div>
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
            onClick={() => {
              // Persist BEFORE handoff so a "Back to Spec" jump
              // from the next panel finds the same state we just
              // shipped. Best-effort — quota / serialisation
              // failures don't block the handoff.
              if (hydrated && typeof window !== "undefined") {
                try {
                  window.localStorage.setItem(LS_LAST_SPEC, JSON.stringify(spec))
                } catch (exc) {
                  console.debug("[SpecTemplateEditor] persist failed:", exc)
                }
              }
              onSpecReady(spec)
            }}
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
