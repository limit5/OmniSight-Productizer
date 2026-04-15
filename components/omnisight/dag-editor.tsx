"use client"

/**
 * Phase 56-DAG-E — DAG Authoring Editor
 *
 * A JSON editor with live validation. Debounced hit on POST /dag/validate
 * on every keystroke so the operator sees rule-level errors (cycle,
 * tier_violation, mece, ...) without submitting. Templates load from a
 * static bundle so a fresh operator isn't staring at an empty textarea.
 *
 * Deliberately minimal: plain textarea + line-numbered error panel. A
 * Monaco upgrade or visual canvas (react-flow) can come in DAG-F/G.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertCircle, CheckCircle2, Loader2, Play, FileText, Copy, ArrowRight, Code, List, Workflow } from "lucide-react"
import {
  validateDag,
  submitDag,
  type DAGValidateResponse,
  type DAGValidationError,
} from "@/lib/api"
import { DagFormEditor, type FormDAG } from "@/components/omnisight/dag-form-editor"
import { DagCanvas } from "@/components/omnisight/dag-canvas"
import { PanelHelp } from "@/components/omnisight/panel-help"

// ─── Templates ──────────────────────────────────────────────────────

type Template = { id: string; label: string; description: string; body: object }

const TEMPLATES: Template[] = [
  {
    id: "minimal",
    label: "Minimal (1 task)",
    description: "Single T1 compile — the smallest valid DAG.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-minimal",
      tasks: [
        {
          task_id: "compile",
          description: "Build the firmware image",
          required_tier: "t1",
          toolchain: "cmake",
          inputs: [],
          expected_output: "build/firmware.bin",
          depends_on: [],
        },
      ],
    },
  },
  {
    id: "compile-flash",
    label: "Compile → Flash (2 tasks)",
    description: "T1 compile then T3 flash. Typical happy-path pipeline.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-compile-flash",
      tasks: [
        {
          task_id: "compile",
          description: "Build the firmware image",
          required_tier: "t1",
          toolchain: "cmake",
          inputs: [],
          expected_output: "build/firmware.bin",
          depends_on: [],
        },
        {
          task_id: "flash",
          description: "Flash the built image onto the target board",
          required_tier: "t3",
          toolchain: "flash_board",
          inputs: ["build/firmware.bin"],
          expected_output: "logs/flash.log",
          depends_on: ["compile"],
        },
      ],
    },
  },
  {
    id: "fan-out",
    label: "Fan-out (1 → 3)",
    description: "Build then three parallel simulations. Exercise parallelism.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-fanout",
      tasks: [
        {
          task_id: "build",
          description: "Produce the test artifact",
          required_tier: "t1",
          toolchain: "cmake",
          inputs: [],
          expected_output: "build/app.bin",
          depends_on: [],
        },
        {
          task_id: "sim_cpu",
          description: "CPU-only simulation",
          required_tier: "t1",
          toolchain: "simulate",
          inputs: ["build/app.bin"],
          expected_output: "reports/cpu.json",
          depends_on: ["build"],
        },
        {
          task_id: "sim_npu",
          description: "NPU simulation",
          required_tier: "t1",
          toolchain: "simulate",
          inputs: ["build/app.bin"],
          expected_output: "reports/npu.json",
          depends_on: ["build"],
        },
        {
          task_id: "sim_power",
          description: "Power profiling",
          required_tier: "t1",
          toolchain: "simulate",
          inputs: ["build/app.bin"],
          expected_output: "reports/power.json",
          depends_on: ["build"],
        },
      ],
    },
  },
  // ─── Real-world templates ─────────────────────────────────────
  // These exist because operators kept asking "how do I write a
  // DAG that…" and the three above didn't cover it. Each matches
  // a pattern already present elsewhere in the system — don't
  // invent new toolchains here, wire to the ones the agents know.
  {
    id: "tier-mix",
    label: "Tier Mix (T1+NET+T3)",
    description: "Build on T1, download deps over the network, flash on T3. Exercises tier handoffs.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-tier-mix",
      tasks: [
        {
          task_id: "build",
          description: "Compile firmware in the airgapped T1 sandbox",
          required_tier: "t1",
          toolchain: "cmake",
          inputs: [],
          expected_output: "build/firmware.bin",
          depends_on: [],
        },
        {
          task_id: "fetch_vendor_blob",
          description: "Download the proprietary vendor partition from the CDN",
          required_tier: "networked",
          toolchain: "http_download",
          inputs: [],
          expected_output: "artifacts/vendor.img",
          depends_on: [],
        },
        {
          task_id: "flash",
          description: "Write firmware + vendor partition to the target board",
          required_tier: "t3",
          toolchain: "flash_board",
          inputs: ["build/firmware.bin", "artifacts/vendor.img"],
          expected_output: "logs/flash.log",
          depends_on: ["build", "fetch_vendor_blob"],
        },
      ],
    },
  },
  {
    id: "cross-compile",
    label: "Cross-compile (sysroot)",
    description: "T1 cross-compile for an embedded SoC with explicit sysroot + toolchain file.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-cross-compile",
      tasks: [
        {
          task_id: "configure",
          description: "Run cmake with CMAKE_TOOLCHAIN_FILE + --sysroot for the target platform",
          required_tier: "t1",
          toolchain: "cmake",
          inputs: [],
          expected_output: "build/CMakeCache.txt",
          depends_on: [],
        },
        {
          task_id: "compile",
          description: "Build the cross-compiled firmware image",
          required_tier: "t1",
          toolchain: "cmake",
          inputs: ["build/CMakeCache.txt"],
          expected_output: "build/app.elf",
          depends_on: ["configure"],
        },
        {
          task_id: "checkpatch",
          description: "Run checkpatch.pl --strict before the artifact is considered good",
          required_tier: "t1",
          toolchain: "checkpatch",
          inputs: ["build/app.elf"],
          expected_output: "reports/checkpatch.log",
          depends_on: ["compile"],
        },
      ],
    },
  },
  {
    id: "fine-tune",
    label: "Fine-tune (Phase 65)",
    description: "Export JSONL → submit backend → poll → eval. Feeds the nightly self-improvement loop.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-finetune",
      tasks: [
        {
          task_id: "export_jsonl",
          description: "Build the training-set JSONL from completed workflow runs (Phase 65 S1)",
          required_tier: "t1",
          toolchain: "finetune_export",
          inputs: [],
          expected_output: "artifacts/train.jsonl",
          depends_on: [],
        },
        {
          task_id: "submit_job",
          description: "Hand the JSONL to the configured backend (noop | openai | unsloth)",
          required_tier: "networked",
          toolchain: "finetune_submit",
          inputs: ["artifacts/train.jsonl"],
          expected_output: "git:finetune-job-id",
          depends_on: ["export_jsonl"],
        },
        {
          task_id: "eval_holdout",
          description: "Compare candidate vs baseline against configs/iq_benchmark/holdout-finetune.yaml",
          required_tier: "networked",
          toolchain: "finetune_eval",
          inputs: ["git:finetune-job-id"],
          expected_output: "reports/finetune-eval.json",
          depends_on: ["submit_job"],
        },
      ],
    },
  },
  {
    id: "diff-patch",
    label: "Diff-Patch (Phase 67-B)",
    description: "Generate a unified diff, review via DE, apply under the workspace lock.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-diff-patch",
      tasks: [
        {
          task_id: "propose_patch",
          description: "Ask the specialist for a focused diff against the failing module",
          required_tier: "t1",
          toolchain: "patch_propose",
          inputs: [],
          expected_output: "artifacts/proposal.diff",
          depends_on: [],
        },
        {
          task_id: "dry_run",
          description: "git apply --check so we know the patch is clean before the DE sees it",
          required_tier: "t1",
          toolchain: "git",
          inputs: ["artifacts/proposal.diff"],
          expected_output: "logs/dry-run.log",
          depends_on: ["propose_patch"],
        },
        {
          task_id: "apply",
          description: "Apply the approved patch (DE proposal `patch/apply` must be accepted first)",
          required_tier: "t1",
          toolchain: "git",
          inputs: ["artifacts/proposal.diff"],
          expected_output: "git:HEAD",
          depends_on: ["dry_run"],
        },
      ],
    },
  },
]

// ─── Component ──────────────────────────────────────────────────────

const VALIDATE_DEBOUNCE_MS = 500

export function DagEditor() {
  const [text, setText] = useState<string>(() => JSON.stringify(TEMPLATES[0].body, null, 2))
  const [validation, setValidation] = useState<DAGValidateResponse | null>(null)
  const [validating, setValidating] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitMessage, setSubmitMessage] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submittedRunId, setSubmittedRunId] = useState<string | null>(null)
  const [mutate, setMutate] = useState(false)
  const [parseError, setParseError] = useState<string | null>(null)
  // Phase 56-DAG-F: tab state. `text` stays canonical — the form view
  // derives FormDAG from it and serializes back on every mutation, so
  // switching tabs never loses work (as long as JSON is parseable).
  const [tab, setTab] = useState<"json" | "form" | "canvas">("json")
  // Phase 56-DAG-G follow-up: Canvas clicks dispatch a
  // `omnisight:dag-focus-task` custom event. We catch it here, flip
  // to Form, and pass the task id down so the form can scroll /
  // highlight the matching row. The counter is bumped on every
  // request so the same id in a row (click → edit → click again)
  // still re-triggers the scroll.
  const [focusRequest, setFocusRequest] = useState<{ taskId: string; n: number } | null>(null)
  useEffect(() => {
    if (typeof window === "undefined") return
    const onFocus = (e: Event) => {
      const detail = (e as CustomEvent<{ taskId?: string }>).detail
      if (!detail?.taskId) return
      setTab("form")
      setFocusRequest((prev) => ({
        taskId: detail.taskId!,
        n: (prev?.n ?? 0) + 1,
      }))
    }
    window.addEventListener("omnisight:dag-focus-task", onFocus as EventListener)
    return () =>
      window.removeEventListener("omnisight:dag-focus-task", onFocus as EventListener)
  }, [])

  // Cancel-previous pattern: keep latest request's signal so a stale
  // response can't clobber a fresher one.
  const inflight = useRef<AbortController | null>(null)

  // ─── live validate ──────────────────────────────────────────────

  const runValidate = useCallback(async (raw: string) => {
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
      setParseError(null)
    } catch (exc) {
      setParseError(exc instanceof Error ? exc.message : String(exc))
      setValidation(null)
      return
    }

    inflight.current?.abort()
    const ac = new AbortController()
    inflight.current = ac
    setValidating(true)
    try {
      const res = await validateDag(parsed)
      if (ac.signal.aborted) return
      setValidation(res)
    } catch (exc) {
      if (ac.signal.aborted) return
      setValidation({
        ok: false,
        stage: "semantic",
        errors: [{ rule: "network", task_id: null, message: exc instanceof Error ? exc.message : String(exc) }],
      })
    } finally {
      if (!ac.signal.aborted) setValidating(false)
    }
  }, [])

  useEffect(() => {
    const t = setTimeout(() => void runValidate(text), VALIDATE_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [text, runValidate])

  // ─── actions ────────────────────────────────────────────────────

  const loadTemplate = (tpl: Template) => {
    setText(JSON.stringify(tpl.body, null, 2))
    setSubmitMessage(null)
    setSubmitError(null)
  }

  const formatJson = () => {
    try {
      setText(JSON.stringify(JSON.parse(text), null, 2))
    } catch {
      // leave unchanged — the parse error is already shown
    }
  }

  const copyJson = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setSubmitMessage("Copied JSON to clipboard.")
      setTimeout(() => setSubmitMessage(null), 2000)
    } catch {
      setSubmitError("Clipboard write failed.")
    }
  }

  const canSubmit = !!validation?.ok && !parseError && !submitting

  const handleSubmit = async () => {
    if (!canSubmit) return
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      return
    }
    setSubmitting(true)
    setSubmitError(null)
    setSubmitMessage(null)
    setSubmittedRunId(null)
    try {
      const res = await submitDag(parsed, { mutate })
      setSubmitMessage(
        `✓ Submitted — run ${res.run_id}, plan ${res.plan_id ?? "?"} (${res.status}).` +
          (res.mutation_rounds ? ` mutation rounds: ${res.mutation_rounds}` : ""),
      )
      setSubmittedRunId(res.run_id)
    } catch (exc) {
      setSubmitError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSubmitting(false)
    }
  }

  // Hand off to the Pipeline Timeline panel so the operator can watch
  // execution progress on the run they just submitted. Top-level
  // page.tsx listens for this custom event and switches activePanel.
  const jumpToTimeline = () => {
    if (typeof window === "undefined") return
    window.dispatchEvent(
      new CustomEvent("omnisight:navigate", { detail: { panel: "timeline" } }),
    )
  }

  // ─── error index (used to decorate error rows) ──────────────────

  const errors: DAGValidationError[] = useMemo(() => {
    if (parseError) return [{ rule: "json_parse", task_id: null, message: parseError }]
    return validation?.errors ?? []
  }, [parseError, validation])

  // Form tab derives its FormDAG from the canonical text. If JSON is
  // unparseable we render a nudge instead of the form — editing the
  // form under a broken JSON would silently discard the user's WIP.
  const formDag: FormDAG | null = useMemo(() => {
    try {
      const obj = JSON.parse(text) as Partial<FormDAG>
      if (!obj || !Array.isArray(obj.tasks)) return null
      return {
        schema_version: obj.schema_version ?? 1,
        dag_id: obj.dag_id ?? "",
        tasks: obj.tasks,
      }
    } catch {
      return null
    }
  }, [text])

  const handleFormChange = (next: FormDAG) => {
    setText(JSON.stringify(next, null, 2))
  }

  const status: "ok" | "error" | "unknown" = parseError
    ? "error"
    : validation?.ok
      ? "ok"
      : validation
        ? "error"
        : "unknown"

  // ─── render ─────────────────────────────────────────────────────

  return (
    <div className="flex flex-col gap-3 p-3 rounded-lg bg-[var(--card)] border border-[var(--border)]">
      {/* Header + tabs + status */}
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-mono text-sm font-semibold text-[var(--foreground)] flex items-center gap-2">
          <FileText size={14} className="text-[var(--artifact-purple)]" />
          DAG Editor
          <PanelHelp doc="dag-authoring" />
        </h3>
        <div className="flex items-center gap-2">
          <div role="tablist" aria-label="Editor mode" className="flex rounded border border-[var(--border)] overflow-hidden">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "json"}
              onClick={() => setTab("json")}
              className={
                "text-xs font-mono px-2 py-0.5 flex items-center gap-1 " +
                (tab === "json"
                  ? "bg-[var(--artifact-purple)] text-white"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]")
              }
            >
              <Code size={10} /> JSON
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "form"}
              onClick={() => setTab("form")}
              className={
                "text-xs font-mono px-2 py-0.5 flex items-center gap-1 " +
                (tab === "form"
                  ? "bg-[var(--artifact-purple)] text-white"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]")
              }
            >
              <List size={10} /> Form
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "canvas"}
              onClick={() => setTab("canvas")}
              className={
                "text-xs font-mono px-2 py-0.5 flex items-center gap-1 " +
                (tab === "canvas"
                  ? "bg-[var(--artifact-purple)] text-white"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]")
              }
            >
              <Workflow size={10} /> Canvas
            </button>
          </div>
          <StatusBadge status={status} validating={validating} />
        </div>
      </div>

      {/* Templates */}
      <div className="flex flex-wrap gap-1">
        {TEMPLATES.map((tpl) => (
          <button
            key={tpl.id}
            type="button"
            onClick={() => loadTemplate(tpl)}
            title={tpl.description}
            className="text-xs font-mono px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--muted)] text-[var(--muted-foreground)]"
          >
            {tpl.label}
          </button>
        ))}
        <div className="ml-auto flex gap-1">
          <button
            type="button"
            onClick={formatJson}
            className="text-xs font-mono px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--muted)] text-[var(--muted-foreground)]"
          >
            Format
          </button>
          <button
            type="button"
            onClick={copyJson}
            className="text-xs font-mono px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--muted)] text-[var(--muted-foreground)] flex items-center gap-1"
          >
            <Copy size={10} /> Copy
          </button>
        </div>
      </div>

      {/* Editor body — JSON textarea or Form view */}
      {tab === "json" ? (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          aria-label="DAG JSON editor"
          className="w-full min-h-[240px] max-h-[480px] font-mono text-xs leading-relaxed p-2 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)] resize-y"
        />
      ) : tab === "form" ? (
        formDag ? (
          <DagFormEditor
            value={formDag}
            onChange={handleFormChange}
            focusRequest={focusRequest}
          />
        ) : (
          <div className="text-xs font-mono p-3 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)]">
            Form view disabled — JSON is not parseable. Fix in the JSON tab first.
          </div>
        )
      ) : (
        // Canvas tab — pass any task-id-bearing validation errors so
        // the canvas can tint offenders red in place.
        <DagCanvas
          dag={formDag}
          errors={errors}
          t3Runner={validation?.t3_runner}
        />
      )}

      {/* Error panel */}
      {errors.length > 0 && (
        <div className="rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 p-2">
          <div className="flex items-center gap-1 text-xs font-mono font-semibold text-[var(--destructive)] mb-1">
            <AlertCircle size={12} /> {errors.length} error{errors.length > 1 ? "s" : ""}
          </div>
          <ul className="text-xs font-mono space-y-1 max-h-32 overflow-y-auto">
            {errors.map((e, i) => (
              <li key={i} className="text-[var(--foreground)]">
                <span className="text-[var(--destructive)] font-semibold">{e.rule}</span>
                {e.task_id && <span className="text-[var(--muted-foreground)]"> · {e.task_id}</span>}
                <span className="text-[var(--muted-foreground)]">: </span>
                <span>{e.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Submit row */}
      <div className="flex items-center justify-between gap-2 pt-1 border-t border-[var(--border)]">
        <label className="flex items-center gap-1 text-xs font-mono text-[var(--muted-foreground)] select-none">
          <input
            type="checkbox"
            checked={mutate}
            onChange={(e) => setMutate(e.target.checked)}
            className="accent-[var(--artifact-purple)]"
          />
          <span>mutate=true (auto-fix via LLM on fail)</span>
        </label>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="text-xs font-mono px-3 py-1 rounded bg-[var(--artifact-purple)] text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
        >
          {submitting ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
          Submit
        </button>
      </div>

      {/* Submit result */}
      {submitMessage && (
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs font-mono text-[var(--artifact-purple)]">{submitMessage}</div>
          {submittedRunId && (
            <button
              type="button"
              onClick={jumpToTimeline}
              className="text-xs font-mono px-2 py-0.5 rounded border border-[var(--artifact-purple)] text-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)] hover:text-white transition-colors flex items-center gap-1 shrink-0"
              title={`View run ${submittedRunId} in Pipeline Timeline`}
            >
              View in Timeline <ArrowRight size={10} />
            </button>
          )}
        </div>
      )}
      {submitError && (
        <div className="text-xs font-mono text-[var(--destructive)]">{submitError}</div>
      )}
    </div>
  )
}

// ─── subcomponent ───────────────────────────────────────────────────

function StatusBadge({
  status,
  validating,
}: {
  status: "ok" | "error" | "unknown"
  validating: boolean
}) {
  if (validating) {
    return (
      <span className="text-xs font-mono text-[var(--muted-foreground)] flex items-center gap-1">
        <Loader2 size={10} className="animate-spin" /> validating…
      </span>
    )
  }
  if (status === "ok") {
    return (
      <span className="text-xs font-mono text-emerald-400 flex items-center gap-1">
        <CheckCircle2 size={10} /> valid
      </span>
    )
  }
  if (status === "error") {
    return (
      <span className="text-xs font-mono text-[var(--destructive)] flex items-center gap-1">
        <AlertCircle size={10} /> invalid
      </span>
    )
  }
  return <span className="text-xs font-mono text-[var(--muted-foreground)]">—</span>
}
