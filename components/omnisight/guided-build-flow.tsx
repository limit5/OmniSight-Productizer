"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  AlertCircle, ArrowLeft, ArrowRight, CheckCircle2, FileText,
  Loader2, Play, Sparkles, Workflow,
} from "lucide-react"
import {
  submitDag,
  validateDag,
  type DAGValidateResponse,
  type DAGValidationError,
  type ParsedSpec,
} from "@/lib/api"

type Template = {
  id: string
  label: string
  description: string
  body: BuildDag
}

type BuildDag = {
  schema_version: number
  dag_id: string
  tasks: Array<{
    task_id: string
    description: string
    required_tier: string
    toolchain: string
    inputs: string[]
    expected_output: string
    depends_on: string[]
  }>
}

const TEMPLATES: Template[] = [
  {
    id: "minimal",
    label: "Minimal build",
    description: "Single compile task for a small local build.",
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
    label: "Compile and flash",
    description: "Compile first, then flash the target board.",
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
    id: "cross-compile",
    label: "Cross-compile",
    description: "Configure, compile, then run checkpatch for a target platform.",
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
    label: "Data pipeline",
    description: "Export, submit, and evaluate a batch workflow.",
    body: {
      schema_version: 1,
      dag_id: "SAMPLE-finetune",
      tasks: [
        {
          task_id: "export_jsonl",
          description: "Build the training-set JSONL from completed workflow runs",
          required_tier: "t1",
          toolchain: "finetune_export",
          inputs: [],
          expected_output: "artifacts/train.jsonl",
          depends_on: [],
        },
        {
          task_id: "submit_job",
          description: "Hand the JSONL to the configured backend",
          required_tier: "networked",
          toolchain: "finetune_submit",
          inputs: ["artifacts/train.jsonl"],
          expected_output: "git:finetune-job-id",
          depends_on: ["export_jsonl"],
        },
        {
          task_id: "eval_holdout",
          description: "Compare candidate vs baseline against the holdout benchmark",
          required_tier: "networked",
          toolchain: "finetune_eval",
          inputs: ["git:finetune-job-id"],
          expected_output: "reports/finetune-eval.json",
          depends_on: ["submit_job"],
        },
      ],
    },
  },
]

function chooseTemplate(spec: ParsedSpec): Template {
  const pt = spec.project_type?.value
  const rm = spec.runtime_model?.value
  const fw = spec.framework?.value
  let pickId = "minimal"
  if (pt === "embedded_firmware") pickId = "cross-compile"
  else if (rm === "ssg" || fw === "nextjs" || fw === "react") pickId = "compile-flash"
  else if (rm === "batch" || pt === "data_pipeline") pickId = "fine-tune"
  else if (fw === "rust" || pt === "cli_tool") pickId = "cross-compile"
  return TEMPLATES.find((tpl) => tpl.id === pickId) || TEMPLATES[0]
}

function targetPlatformFromSpec(spec: ParsedSpec): string | null {
  const arch = spec.target_arch?.value
  const hw = spec.hardware_required?.value
  const archMap: Record<string, string> = {
    x86_64: "host_native",
    arm64: "aarch64",
    arm32: "armv7",
    riscv64: "riscv64",
  }
  if (!arch || arch === "unknown" || !archMap[arch]) return null
  if (hw === "yes" && archMap[arch] === "host_native") return "aarch64"
  return archMap[arch]
}

function planExecutionLabel(status: string): string {
  const normalized = status.toLowerCase()
  if (normalized === "executing" || normalized === "running") return "executing"
  if (normalized === "validated" || normalized === "submitted" || normalized === "pending") {
    return "submitted (not executing)"
  }
  return status
}

function fieldValue(spec: ParsedSpec, key: keyof ParsedSpec): string {
  const value = spec[key]
  if (value && typeof value === "object" && "value" in value) {
    return String(value.value || "unknown")
  }
  return "unknown"
}

interface Props {
  spec: ParsedSpec
  onOpenEditor: () => void
}

export function GuidedBuildFlow({ spec, onOpenEditor }: Props) {
  const template = useMemo(() => chooseTemplate(spec), [spec])
  const targetPlatform = useMemo(() => targetPlatformFromSpec(spec), [spec])
  const [validation, setValidation] = useState<DAGValidateResponse | null>(null)
  const [validating, setValidating] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitMessage, setSubmitMessage] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submittedRunId, setSubmittedRunId] = useState<string | null>(null)
  const [submittedPlanStatus, setSubmittedPlanStatus] = useState<string | null>(null)
  const inflight = useRef<AbortController | null>(null)

  useEffect(() => {
    inflight.current?.abort()
    const ac = new AbortController()
    inflight.current = ac
    queueMicrotask(() => {
      if (ac.signal.aborted) return
      setValidating(true)
      setValidation(null)
      setSubmitMessage(null)
      setSubmitError(null)
      setSubmittedRunId(null)
      setSubmittedPlanStatus(null)
    })
    void validateDag(template.body, targetPlatform || undefined)
      .then((res) => {
        if (!ac.signal.aborted) setValidation(res)
      })
      .catch((exc) => {
        if (ac.signal.aborted) return
        setValidation({
          ok: false,
          stage: "semantic",
          errors: [{
            rule: "network",
            task_id: null,
            message: exc instanceof Error ? exc.message : String(exc),
          }],
        })
      })
      .finally(() => {
        if (!ac.signal.aborted) setValidating(false)
      })
    return () => ac.abort()
  }, [template, targetPlatform])

  const errors: DAGValidationError[] = validation?.errors ?? []
  const canStart = !!validation?.ok && !validating && !submitting

  const startBuild = useCallback(async () => {
    if (!canStart) return
    setSubmitting(true)
    setSubmitMessage(null)
    setSubmitError(null)
    setSubmittedRunId(null)
    setSubmittedPlanStatus(null)
    try {
      const res = await submitDag(template.body, {
        targetPlatform: targetPlatform || undefined,
        metadata: {
          source: "guided-build-flow",
          template_id: template.id,
          spec_project_type: spec.project_type?.value,
          spec_runtime_model: spec.runtime_model?.value,
        },
      })
      setSubmittedPlanStatus(res.status)
      setSubmittedRunId(res.run_id)
      setSubmitMessage(
        `Submitted - run ${res.run_id}, plan ${res.plan_id ?? "?"} (${planExecutionLabel(res.status)})` +
          (targetPlatform ? ` · target=${targetPlatform}` : "") + ".",
      )
    } catch (exc) {
      setSubmitError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSubmitting(false)
    }
  }, [canStart, spec, targetPlatform, template])

  const jumpToTimeline = () => {
    if (typeof window === "undefined") return
    window.dispatchEvent(
      new CustomEvent("omnisight:navigate", { detail: { panel: "timeline" } }),
    )
  }

  return (
    <div className="flex flex-col gap-3 p-3 rounded-lg bg-[var(--card)] border border-[var(--border)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-mono text-sm font-semibold text-[var(--foreground)] flex items-center gap-2">
            <Sparkles size={14} className="text-[var(--artifact-purple)]" />
            Build kickoff
          </h3>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            The spec is ready. OmniSight prepared a build plan and can start it now.
          </p>
        </div>
        <button
          type="button"
          onClick={onOpenEditor}
          className="text-xs font-mono px-2 py-1 rounded border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)] flex items-center gap-1"
        >
          <FileText size={12} /> Open DAG editor
        </button>
      </div>

      <div className="grid gap-2 md:grid-cols-3">
        <div className="rounded border border-[var(--border)] p-2">
          <div className="text-[10px] font-mono uppercase tracking-wide text-[var(--muted-foreground)]">Project</div>
          <div className="mt-1 text-xs font-mono text-[var(--foreground)]">{fieldValue(spec, "project_type")}</div>
        </div>
        <div className="rounded border border-[var(--border)] p-2">
          <div className="text-[10px] font-mono uppercase tracking-wide text-[var(--muted-foreground)]">Runtime</div>
          <div className="mt-1 text-xs font-mono text-[var(--foreground)]">{fieldValue(spec, "runtime_model")}</div>
        </div>
        <div className="rounded border border-[var(--border)] p-2">
          <div className="text-[10px] font-mono uppercase tracking-wide text-[var(--muted-foreground)]">Target</div>
          <div className="mt-1 text-xs font-mono text-[var(--foreground)]">{targetPlatform || "hardware manifest default"}</div>
        </div>
      </div>

      <div className="rounded border border-[var(--border)] p-3 bg-[var(--background)]">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Workflow size={14} className="text-[var(--artifact-purple)] shrink-0" />
            <div className="min-w-0">
              <div className="text-sm font-mono font-semibold text-[var(--foreground)]">{template.label}</div>
              <div className="text-xs text-[var(--muted-foreground)]">{template.description}</div>
            </div>
          </div>
          <div className="text-xs font-mono text-[var(--muted-foreground)]">
            {template.body.tasks.length} task{template.body.tasks.length === 1 ? "" : "s"}
          </div>
        </div>
        <ol className="mt-3 grid gap-2">
          {template.body.tasks.map((task, index) => (
            <li key={task.task_id} className="flex gap-2 text-xs">
              <span className="w-6 h-6 rounded border border-[var(--border)] flex items-center justify-center font-mono text-[10px] text-[var(--muted-foreground)] shrink-0">
                {index + 1}
              </span>
              <div className="min-w-0">
                <div className="font-mono text-[var(--foreground)]">{task.task_id}</div>
                <div className="text-[var(--muted-foreground)]">{task.description}</div>
              </div>
            </li>
          ))}
        </ol>
      </div>

      {validating && (
        <div className="text-xs font-mono text-[var(--muted-foreground)] flex items-center gap-1">
          <Loader2 size={12} className="animate-spin" /> validating build plan...
        </div>
      )}
      {validation?.ok && (
        <div className="text-xs font-mono text-emerald-400 flex items-center gap-1">
          <CheckCircle2 size={12} /> build plan validated
        </div>
      )}
      {errors.length > 0 && (
        <div className="rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 p-2">
          <div className="flex items-center gap-1 text-xs font-mono font-semibold text-[var(--destructive)] mb-1">
            <AlertCircle size={12} /> build plan needs attention
          </div>
          <ul className="text-xs font-mono space-y-1">
            {errors.map((e, i) => (
              <li key={`${e.rule}-${i}`} className="text-[var(--foreground)]">
                <span className="text-[var(--destructive)] font-semibold">{e.rule}</span>
                <span className="text-[var(--muted-foreground)]">: </span>
                <span>{e.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-[var(--border)]">
        <button
          type="button"
          onClick={() => {
            if (typeof window === "undefined") return
            window.dispatchEvent(
              new CustomEvent("omnisight:navigate", { detail: { panel: "intent" } }),
            )
          }}
          className="text-xs font-mono px-2 py-1 rounded border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)] flex items-center gap-1"
        >
          <ArrowLeft size={12} /> Back to spec
        </button>
        <button
          type="button"
          onClick={startBuild}
          disabled={!canStart}
          className="text-xs font-mono px-3 py-1 rounded bg-[var(--artifact-purple)] text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
        >
          {submitting ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
          Start
        </button>
      </div>

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
      {submittedPlanStatus && (
        <div className="text-xs font-mono rounded border px-2 py-1 border-[var(--neural-cyan,#67e8f9)]/40 bg-[var(--neural-cyan,#67e8f9)]/10 text-[var(--neural-cyan,#67e8f9)]">
          Plan status: {planExecutionLabel(submittedPlanStatus)}.
        </div>
      )}
      {submitError && (
        <div className="text-xs font-mono text-[var(--destructive)] break-words">
          {submitError}
        </div>
      )}
    </div>
  )
}
