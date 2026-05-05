"use client"

// BP.M.4 -- operator review UI for auto-distilled skills.
// Module-global state audit: this component keeps only immutable display
// maps at module scope. Draft/review/promote state is fetched from the
// backend auto-skills API, whose router coordinates lifecycle writes via
// PG row locks.

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  RefreshCw,
  ShieldCheck,
  UploadCloud,
} from "lucide-react"
import {
  listAutoSkills,
  promoteAutoSkill,
  reviewAutoSkill,
  type AutoSkillItem,
  type AutoSkillPromoteResponse,
  type AutoSkillsListResponse,
  type AutoSkillStatus,
} from "@/lib/api"

const STATUS_STEPS: AutoSkillStatus[] = ["draft", "reviewed", "promoted"]

const STATUS_LABEL: Record<AutoSkillStatus, string> = {
  draft: "Draft",
  reviewed: "Reviewed",
  promoted: "Promoted",
}

const STATUS_COLOR: Record<AutoSkillStatus, string> = {
  draft: "var(--neural-amber, #fbbf24)",
  reviewed: "var(--neural-blue, #60a5fa)",
  promoted: "var(--validation-emerald, #34d399)",
}

function formatCreatedAt(iso: string): string {
  if (!iso) return "--"
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso)
  if (!m) return iso
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`
}

function sourceLabel(sourceTaskId: string | null): string {
  return sourceTaskId && sourceTaskId.trim() ? sourceTaskId : "manual"
}

export interface SkillReviewPanelProps {
  /** Test seam / alternate tenant source. */
  listSkills?: () => Promise<AutoSkillsListResponse>
  /** Test seam for the review action. */
  reviewSkill?: (
    skillId: string,
    body: {
      skill_name: string
      source_task_id: string | null
      markdown_content: string
      expected_version: number
    },
  ) => Promise<AutoSkillItem>
  /** Test seam for the promote action. */
  promoteSkill?: (skillId: string) => Promise<AutoSkillPromoteResponse>
}

export function SkillReviewPanel({
  listSkills = () => listAutoSkills({ limit: 200 }),
  reviewSkill = reviewAutoSkill,
  promoteSkill = promoteAutoSkill,
}: SkillReviewPanelProps) {
  const [items, setItems] = useState<AutoSkillItem[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [skillName, setSkillName] = useState("")
  const [sourceTaskId, setSourceTaskId] = useState("")
  const [markdownContent, setMarkdownContent] = useState("")
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<"review" | "promote" | null>(null)
  const [error, setError] = useState("")
  const [promotedPath, setPromotedPath] = useState("")

  const selected = useMemo(
    () => items.find((item) => item.id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  )

  const counts = useMemo(() => {
    return STATUS_STEPS.reduce<Record<AutoSkillStatus, number>>((acc, status) => {
      acc[status] = items.filter((item) => item.status === status).length
      return acc
    }, { draft: 0, reviewed: 0, promoted: 0 })
  }, [items])

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const res = await listSkills()
      setItems(res.items)
      setSelectedId((prev) => {
        if (prev && res.items.some((item) => item.id === prev)) return prev
        return res.items[0]?.id ?? null
      })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load auto skills")
    } finally {
      setLoading(false)
    }
  }, [listSkills])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!selected) {
      setSkillName("")
      setSourceTaskId("")
      setMarkdownContent("")
      return
    }
    setSkillName(selected.skill_name)
    setSourceTaskId(selected.source_task_id ?? "")
    setMarkdownContent(selected.markdown_content)
  }, [selected])

  const updateSelected = useCallback((next: AutoSkillItem) => {
    setItems((prev) => prev.map((item) => item.id === next.id ? next : item))
    setSelectedId(next.id)
  }, [])

  const handleReview = useCallback(async () => {
    if (!selected || selected.status !== "draft" || !skillName.trim() || !markdownContent.trim()) return
    setSaving("review")
    setError("")
    setPromotedPath("")
    try {
      const next = await reviewSkill(selected.id, {
        skill_name: skillName.trim(),
        source_task_id: sourceTaskId.trim() || null,
        markdown_content: markdownContent,
        expected_version: selected.version,
      })
      updateSelected(next)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to mark skill reviewed")
    } finally {
      setSaving(null)
    }
  }, [
    load,
    markdownContent,
    reviewSkill,
    selected,
    skillName,
    sourceTaskId,
    updateSelected,
  ])

  const handlePromote = useCallback(async () => {
    if (!selected || selected.status !== "reviewed") return
    setSaving("promote")
    setError("")
    setPromotedPath("")
    try {
      const res = await promoteSkill(selected.id)
      updateSelected(res.skill)
      setPromotedPath(res.path)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to promote skill")
    } finally {
      setSaving(null)
    }
  }, [load, promoteSkill, selected, updateSelected])

  const editorDisabled = !selected || selected.status === "promoted"
  const reviewDisabled =
    !selected
    || selected.status !== "draft"
    || !skillName.trim()
    || !markdownContent.trim()
    || saving !== null
  const promoteDisabled = !selected || selected.status !== "reviewed" || saving !== null

  return (
    <section className="flex h-full min-h-[560px] flex-col gap-4 font-mono text-xs">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <FileText size={18} style={{ color: "var(--neural-cyan, #67e8f9)" }} />
          <h2 className="text-sm font-semibold uppercase tracking-fui text-[var(--neural-cyan,#67e8f9)]">
            Skill Review
          </h2>
          <span className="text-[var(--muted-foreground)]">({items.length})</span>
        </div>
        <button
          type="button"
          onClick={load}
          className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] px-2 py-1 text-[var(--muted-foreground)] transition-colors hover:border-[var(--neural-cyan,#67e8f9)] hover:text-[var(--neural-cyan,#67e8f9)] disabled:opacity-50"
          disabled={loading || saving !== null}
          title="Refresh"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </div>

      <div className="grid gap-2 sm:grid-cols-3" data-testid="skill-review-panel-status-steps">
        {STATUS_STEPS.map((status, index) => (
          <div
            key={status}
            className="rounded-md border border-[var(--border)] bg-[var(--card)] p-3"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] uppercase text-[var(--muted-foreground)]">
                {index + 1}. {STATUS_LABEL[status]}
              </span>
              <span
                className="rounded border px-1.5 py-0.5 text-[10px]"
                style={{
                  borderColor: STATUS_COLOR[status],
                  color: STATUS_COLOR[status],
                  background: "color-mix(in srgb, currentColor 10%, transparent)",
                }}
              >
                {counts[status]}
              </span>
            </div>
          </div>
        ))}
      </div>

      {error && (
        <div
          className="flex items-start gap-2 rounded-md border border-[var(--critical-red,#f87171)]/40 bg-[var(--critical-red,#f87171)]/10 p-3 text-[var(--critical-red,#f87171)]"
          data-testid="skill-review-panel-error"
        >
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {promotedPath && (
        <div
          className="flex items-start gap-2 rounded-md border border-[var(--validation-emerald,#34d399)]/40 bg-[var(--validation-emerald,#34d399)]/10 p-3 text-[var(--validation-emerald,#34d399)]"
          data-testid="skill-review-panel-promoted-path"
        >
          <CheckCircle2 size={14} className="mt-0.5 shrink-0" />
          <span>Promoted to {promotedPath}</span>
        </div>
      )}

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(260px,360px)_1fr]">
        <div className="min-h-0 overflow-auto rounded-md border border-[var(--border)]">
          {loading && items.length === 0 && (
            <div className="p-6 text-center text-[var(--muted-foreground)]">
              Loading auto-distilled skills...
            </div>
          )}
          {!loading && items.length === 0 && (
            <div
              className="p-6 text-center text-[var(--muted-foreground)]"
              data-testid="skill-review-panel-empty"
            >
              No auto-distilled skill drafts.
            </div>
          )}
          {items.map((item) => {
            const active = item.id === selected?.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSelectedId(item.id)}
                className="block w-full border-b border-[var(--border)] p-3 text-left transition-colors last:border-b-0 hover:bg-[var(--secondary)]"
                style={{
                  background: active ? "rgba(103,232,249,0.08)" : undefined,
                }}
                data-testid={`skill-review-panel-row-${item.id}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate font-semibold text-[var(--foreground)]">
                      {item.skill_name}
                    </div>
                    <div className="mt-1 truncate text-[10px] text-[var(--muted-foreground)]">
                      {sourceLabel(item.source_task_id)} - v{item.version} - {formatCreatedAt(item.created_at)}
                    </div>
                  </div>
                  <span
                    className="shrink-0 rounded border px-1.5 py-0.5 text-[10px]"
                    style={{
                      borderColor: STATUS_COLOR[item.status],
                      color: STATUS_COLOR[item.status],
                    }}
                  >
                    {STATUS_LABEL[item.status]}
                  </span>
                </div>
              </button>
            )
          })}
        </div>

        <div className="min-h-0 rounded-md border border-[var(--border)] bg-[var(--card)] p-4">
          {selected ? (
            <div className="flex h-full min-h-0 flex-col gap-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[10px] uppercase text-[var(--muted-foreground)]">
                    Selected skill
                  </div>
                  <div className="truncate text-sm font-semibold text-[var(--foreground)]">
                    {selected.skill_name}
                  </div>
                </div>
                <div
                  className="rounded border px-2 py-1 text-[10px]"
                  style={{
                    borderColor: STATUS_COLOR[selected.status],
                    color: STATUS_COLOR[selected.status],
                  }}
                  data-testid="skill-review-panel-selected-status"
                >
                  {STATUS_LABEL[selected.status]}
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <label className="space-y-1">
                  <span className="text-[10px] uppercase text-[var(--muted-foreground)]">
                    Skill name
                  </span>
                  <input
                    value={skillName}
                    onChange={(e) => setSkillName(e.target.value)}
                    disabled={editorDisabled}
                    className="w-full rounded-md border border-[var(--border)] bg-[var(--secondary)] px-2 py-1.5 text-[var(--foreground)] disabled:opacity-60"
                    aria-label="Skill name"
                  />
                </label>
                <label className="space-y-1">
                  <span className="text-[10px] uppercase text-[var(--muted-foreground)]">
                    Source task
                  </span>
                  <input
                    value={sourceTaskId}
                    onChange={(e) => setSourceTaskId(e.target.value)}
                    disabled={editorDisabled}
                    className="w-full rounded-md border border-[var(--border)] bg-[var(--secondary)] px-2 py-1.5 text-[var(--foreground)] disabled:opacity-60"
                    aria-label="Source task"
                    placeholder="optional"
                  />
                </label>
              </div>

              <label className="flex min-h-0 flex-1 flex-col gap-1">
                <span className="text-[10px] uppercase text-[var(--muted-foreground)]">
                  Skill markdown
                </span>
                <textarea
                  value={markdownContent}
                  onChange={(e) => setMarkdownContent(e.target.value)}
                  disabled={editorDisabled}
                  className="min-h-[260px] flex-1 resize-none rounded-md border border-[var(--border)] bg-[var(--secondary)] px-3 py-2 font-mono text-xs leading-relaxed text-[var(--foreground)] disabled:opacity-60"
                  aria-label="Skill markdown"
                />
              </label>

              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] pt-3">
                <div className="text-[10px] text-[var(--muted-foreground)]">
                  Drafts can be edited while marking reviewed; promoted skills are immutable.
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={handleReview}
                    disabled={reviewDisabled}
                    className="inline-flex items-center gap-1 rounded-md bg-[var(--neural-blue,#60a5fa)] px-3 py-1.5 text-white transition-opacity disabled:opacity-40"
                  >
                    <ShieldCheck size={13} />
                    {saving === "review" ? "Reviewing..." : "Mark reviewed"}
                  </button>
                  <button
                    type="button"
                    onClick={handlePromote}
                    disabled={promoteDisabled}
                    className="inline-flex items-center gap-1 rounded-md bg-[var(--validation-emerald,#34d399)] px-3 py-1.5 text-black transition-opacity disabled:opacity-40"
                  >
                    <UploadCloud size={13} />
                    {saving === "promote" ? "Promoting..." : "Promote"}
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center text-[var(--muted-foreground)]">
              Select a skill draft to review.
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
