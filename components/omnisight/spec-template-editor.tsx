"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  AlertCircle, AlertTriangle, Code, FileUp, GitBranch, Loader2,
  ListChecks, Sparkles, Upload, CheckCircle2, XCircle,
} from "lucide-react"
import {
  parseIntent, clarifyIntent, ingestRepo, uploadDocs,
  type ParsedSpec, type IntentField, type IntentConflict,
  type IngestRepoResponse, type DocFileResult,
} from "@/lib/api"

// ─── Starter prose templates ───────────────────────────────────

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
  onSpecReady?: (spec: ParsedSpec) => void
}

const LS_LAST_SPEC = "omnisight:intent:last_spec"

type SourceTab = "prose" | "repo" | "docs" | "form"

interface DagFailureContext {
  reason: string
  rules: string[]
  target_platform: string | null
}

// ─── Merge helper: ingested spec fields fill gaps but don't
//     override user-set (confidence 1.0) values ────────────────

function mergeIntoSpec(
  base: ParsedSpec | null,
  incoming: ParsedSpec,
): ParsedSpec {
  if (!base) return incoming
  const merged = { ...incoming }
  const fields: FieldName[] = [
    "project_type", "runtime_model", "target_arch", "target_os",
    "framework", "persistence", "deploy_target", "hardware_required",
  ]
  for (const f of fields) {
    const baseField = (base as any)[f] as IntentField
    const incField = (incoming as any)[f] as IntentField
    if (baseField.confidence >= 1.0) {
      ;(merged as any)[f] = baseField
    } else if (incField.confidence > baseField.confidence) {
      ;(merged as any)[f] = incField
    } else {
      ;(merged as any)[f] = baseField
    }
  }
  if (base.raw_text && base.raw_text !== incoming.raw_text) {
    merged.raw_text = base.raw_text
  }
  merged.conflicts = [...(incoming.conflicts || [])]
  return merged
}

export function SpecTemplateEditor({ onSpecReady }: Props) {
  const [tab, setTab] = useState<SourceTab>("prose")
  const [text, setText] = useState("")
  const [spec, setSpec] = useState<ParsedSpec | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failure, setFailure] = useState<DagFailureContext | null>(null)
  const [hydrated, setHydrated] = useState(false)

  useEffect(() => {
    if (typeof window === "undefined" || !spec) return
    window.dispatchEvent(
      new CustomEvent("omnisight:spec-updated", { detail: { spec } }),
    )
  }, [spec])

  // Repo tab state
  const [repoUrl, setRepoUrl] = useState("")
  const [repoLoading, setRepoLoading] = useState(false)
  const [repoError, setRepoError] = useState<string | null>(null)
  const [repoDetectedFiles, setRepoDetectedFiles] = useState<string[]>([])

  // Docs tab state
  const [, setDocFiles] = useState<File[]>([])
  const [docResults, setDocResults] = useState<DocFileResult[]>([])
  const [docsLoading, setDocsLoading] = useState(false)
  const [docsError, setDocsError] = useState<string | null>(null)

  const inflight = useRef<AbortController | null>(null)
  const dropRef = useRef<HTMLDivElement>(null)

  // ─── DAG → Spec back-jump context ──────────────────────────────
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

  useEffect(() => {
    if (tab !== "prose") return
    const t = setTimeout(() => void runParse(text), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [text, tab, runParse])

  const patchField = (name: FieldName, value: string) => {
    if (failure) setFailure(null)
    if (!spec) {
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

  // ─── Repo ingest handler ─────────────────────────────────────
  const handleRepoIngest = async () => {
    if (!repoUrl.trim()) return
    setRepoLoading(true)
    setRepoError(null)
    setRepoDetectedFiles([])
    try {
      const result = await ingestRepo(repoUrl.trim())
      const meta = (result as IngestRepoResponse)._ingest_meta
      if (meta) setRepoDetectedFiles(meta.detected_files)
      const cleaned = { ...result } as any
      delete cleaned._ingest_meta
      const merged = mergeIntoSpec(spec, cleaned as ParsedSpec)
      setSpec(merged)
      setError(null)
    } catch (exc) {
      setRepoError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setRepoLoading(false)
    }
  }

  // ─── Docs upload handler ─────────────────────────────────────
  const handleDocsUpload = async (filesToUpload: File[]) => {
    if (filesToUpload.length === 0) return
    setDocsLoading(true)
    setDocsError(null)
    try {
      const result = await uploadDocs(filesToUpload)
      setDocResults(result.files)
      if (result.spec) {
        const merged = mergeIntoSpec(spec, result.spec)
        setSpec(merged)
      }
      setError(null)
    } catch (exc) {
      setDocsError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setDocsLoading(false)
    }
  }

  const handleFileDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const droppedFiles = Array.from(e.dataTransfer.files)
    if (droppedFiles.length === 0) return
    setDocFiles((prev) => [...prev, ...droppedFiles])
    void handleDocsUpload(droppedFiles)
  }

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files || [])
    if (selected.length === 0) return
    setDocFiles((prev) => [...prev, ...selected])
    void handleDocsUpload(selected)
  }

  const canContinue = useMemo(() => {
    if (!spec) return false
    if (spec.conflicts.length > 0) return false
    for (const n of [
      "project_type", "runtime_model", "target_arch", "target_os",
      "framework", "persistence", "deploy_target",
    ] as FieldName[]) {
      if ((spec as any)[n].confidence < 0.7) return false
    }
    return true
  }, [spec])

  const tabDefs: { id: SourceTab; label: string; icon: React.ReactNode }[] = [
    { id: "prose", label: "Prose", icon: <Code size={10} /> },
    { id: "repo",  label: "From Repo", icon: <GitBranch size={10} /> },
    { id: "docs",  label: "From Docs", icon: <FileUp size={10} /> },
    { id: "form",  label: "Form", icon: <ListChecks size={10} /> },
  ]

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
            {tabDefs.map((t) => (
              <button
                key={t.id}
                type="button" role="tab"
                aria-selected={tab === t.id}
                onClick={() => setTab(t.id)}
                className={`text-xs font-mono px-2 py-0.5 flex items-center gap-1 ${
                  tab === t.id
                    ? "bg-[var(--artifact-purple)] text-white"
                    : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]"
                }`}
              >
                {t.icon} {t.label}
              </button>
            ))}
          </div>
          {(loading || repoLoading || docsLoading) && (
            <Loader2 size={12} className="animate-spin text-[var(--muted-foreground)]" />
          )}
        </div>
      </div>

      {/* ─── Prose tab ─── */}
      {tab === "prose" && (
        <div className="flex flex-col gap-2">
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

      {/* ─── Repo tab ─── */}
      {tab === "repo" && (
        <div className="flex flex-col gap-2">
          <div className="flex gap-2">
            <input
              type="url"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://github.com/user/repo.git"
              aria-label="Repository URL"
              className="flex-1 text-xs font-mono px-2 py-1.5 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
            />
            <button
              type="button"
              onClick={() => void handleRepoIngest()}
              disabled={repoLoading || !repoUrl.trim()}
              aria-label="Clone and analyze"
              className="text-xs font-mono px-3 py-1.5 rounded bg-[var(--artifact-purple)] text-white hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
            >
              {repoLoading ? (
                <><Loader2 size={10} className="animate-spin" /> Cloning…</>
              ) : (
                <><GitBranch size={10} /> Analyze</>
              )}
            </button>
          </div>
          {repoLoading && (
            <div className="flex items-center gap-2 text-xs font-mono text-[var(--muted-foreground)] p-2 rounded bg-[var(--muted)]/50" role="status" aria-label="Clone progress">
              <Loader2 size={12} className="animate-spin" />
              Cloning repository and analyzing manifests…
            </div>
          )}
          {repoError && (
            <div className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs">
              <AlertCircle size={12} className="shrink-0 mt-0.5" />
              <span>{repoError}</span>
            </div>
          )}
          {repoDetectedFiles.length > 0 && (
            <div className="flex flex-col gap-1 p-2 rounded bg-[var(--muted)]/30 border border-[var(--border)]">
              <span className="text-[10px] font-mono uppercase tracking-wider text-[var(--muted-foreground)]">
                Detected files
              </span>
              <div className="flex flex-wrap gap-1">
                {repoDetectedFiles.map((f) => (
                  <span key={f} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[var(--validation-emerald,#10b981)]/10 text-[var(--validation-emerald,#10b981)] border border-[var(--validation-emerald,#10b981)]/30">
                    {f}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* ─── Docs tab ─── */}
      {tab === "docs" && (
        <div className="flex flex-col gap-2">
          <div
            ref={dropRef}
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleFileDrop}
            className="flex flex-col items-center justify-center gap-2 p-6 rounded border-2 border-dashed border-[var(--border)] hover:border-[var(--artifact-purple)] bg-[var(--background)] cursor-pointer transition-colors"
            role="region"
            aria-label="Drop zone"
            onClick={() => {
              const input = dropRef.current?.querySelector("input[type=file]") as HTMLInputElement | null
              input?.click()
            }}
          >
            <Upload size={20} className="text-[var(--muted-foreground)]" />
            <span className="text-xs font-mono text-[var(--muted-foreground)]">
              Drop files here or click to browse
            </span>
            <span className="text-[10px] font-mono text-[var(--muted-foreground)]">
              .txt, .md, .json, .yaml, .yml, .toml, .cfg, .ini, .csv
            </span>
            <input
              type="file"
              multiple
              accept=".txt,.md,.json,.yaml,.yml,.toml,.cfg,.ini,.csv"
              onChange={handleFileSelect}
              className="hidden"
              aria-label="File upload"
            />
          </div>
          {docsLoading && (
            <div className="flex items-center gap-2 text-xs font-mono text-[var(--muted-foreground)]" role="status" aria-label="Upload progress">
              <Loader2 size={12} className="animate-spin" />
              Parsing uploaded files…
            </div>
          )}
          {docsError && (
            <div className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs">
              <AlertCircle size={12} className="shrink-0 mt-0.5" />
              <span>{docsError}</span>
            </div>
          )}
          {docResults.length > 0 && (
            <div className="flex flex-col gap-1" role="list" aria-label="Uploaded files">
              {docResults.map((fr, i) => (
                <div key={`${fr.name}-${i}`} className="flex items-center gap-2 text-xs font-mono px-2 py-1 rounded bg-[var(--muted)]/30 border border-[var(--border)]" role="listitem">
                  {fr.status === "parsed" ? (
                    <CheckCircle2 size={12} className="text-[var(--validation-emerald,#10b981)]" />
                  ) : (
                    <XCircle size={12} className="text-[var(--destructive)]" />
                  )}
                  <span className="flex-1 truncate">{fr.name}</span>
                  <span className={`text-[10px] px-1 rounded ${
                    fr.status === "parsed"
                      ? "bg-[var(--validation-emerald,#10b981)]/10 text-[var(--validation-emerald,#10b981)]"
                      : "bg-[var(--destructive)]/10 text-[var(--destructive)]"
                  }`}>
                    {fr.status}
                  </span>
                  {fr.reason && (
                    <span className="text-[10px] text-[var(--muted-foreground)]">{fr.reason}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ─── Form tab ─── */}
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

      {/* DAG failure context banner */}
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

      {/* Continue button */}
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
    label = "✓"
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
