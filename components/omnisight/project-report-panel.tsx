"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import {
  FileText, ChevronDown, ChevronRight, Download, Copy, Share2,
  Loader2, AlertCircle, Check, Link2,
} from "lucide-react"
import {
  generateReport, getReport, shareReport,
  type ReportResponse,
} from "@/lib/api"

interface ProjectReportPanelProps {
  runId?: string
  reportId?: string
  title?: string
}

type SectionKey = "spec" | "execution" | "outcome"

const SECTION_HEADERS: Record<SectionKey, { label: string; pattern: RegExp }> = {
  spec:      { label: "1. Specification", pattern: /^## 1\. Specification$/m },
  execution: { label: "2. Execution",     pattern: /^## 2\. Execution$/m },
  outcome:   { label: "3. Outcome",       pattern: /^## 3\. Outcome$/m },
}

function extractSection(markdown: string, key: SectionKey): string {
  const keys: SectionKey[] = ["spec", "execution", "outcome"]
  const idx = keys.indexOf(key)
  const start = markdown.search(SECTION_HEADERS[key].pattern)
  if (start === -1) return ""
  const nextKey = keys[idx + 1]
  let end = markdown.length
  if (nextKey) {
    const nextStart = markdown.search(SECTION_HEADERS[nextKey].pattern)
    if (nextStart !== -1) end = nextStart
  } else {
    const footer = markdown.indexOf("\n---\n", start)
    if (footer !== -1) end = footer
  }
  return markdown.slice(start, end).trim()
}

function markdownToHtml(md: string): string {
  let html = md
    .replace(/^### (.+)$/gm, '<h4 class="font-mono text-[11px] font-bold text-[var(--neural-cyan,#67e8f9)] mt-3 mb-1">$1</h4>')
    .replace(/^## (.+)$/gm, "")
    .replace(/^\| (.+) \|$/gm, (match) => {
      const cells = match.slice(1, -1).split("|").map((c) => c.trim())
      return `<tr>${cells.map((c) => `<td class="px-2 py-0.5 border border-[var(--neural-border,rgba(148,163,184,0.2))]">${c}</td>`).join("")}</tr>`
    })
    .replace(/^\|[-| ]+\|$/gm, "")
    .replace(/^- \*\*(.+?)\*\*(.*)$/gm, '<li class="ml-3"><strong>$1</strong>$2</li>')
    .replace(/^- (.+)$/gm, '<li class="ml-3">$1</li>')
    .replace(/```\n([\s\S]*?)\n```/g, '<pre class="bg-white/5 rounded p-2 text-[10px] overflow-x-auto my-1">$1</pre>')
    .replace(/`([^`]+)`/g, '<code class="bg-white/10 px-1 rounded text-[10px]">$1</code>')
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")

  html = html.replace(/(<tr>[\s\S]*?<\/tr>\s*)+/g, (block) => {
    return `<table class="w-full text-[10px] font-mono border-collapse my-1">${block}</table>`
  })
  html = html.replace(/(<li[\s\S]*?<\/li>\s*)+/g, (block) => {
    return `<ul class="list-none space-y-0.5 my-1">${block}</ul>`
  })
  return html
}

export function ProjectReportPanel({ runId, reportId, title }: ProjectReportPanelProps) {
  const [report, setReport] = useState<ReportResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Record<SectionKey, boolean>>({
    spec: false, execution: false, outcome: false,
  })
  const [copied, setCopied] = useState(false)
  const [shareUrl, setShareUrl] = useState<string | null>(null)
  const [sharing, setSharing] = useState(false)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const loadReport = useCallback(async () => {
    if (!runId && !reportId) return
    setLoading(true)
    setError(null)
    try {
      let resp: ReportResponse
      if (reportId) {
        resp = await getReport(reportId)
      } else {
        resp = await generateReport(runId!, title)
      }
      if (!mountedRef.current) return
      setReport(resp)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [runId, reportId, title])

  useEffect(() => {
    void loadReport()
  }, [loadReport])

  const toggleSection = useCallback((key: SectionKey) => {
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }))
  }, [])

  const handleCopy = useCallback(async () => {
    if (!report) return
    await navigator.clipboard.writeText(report.markdown)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [report])

  const handleDownload = useCallback(() => {
    if (!report) return
    const blob = new Blob([report.markdown], { type: "text/markdown;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `${report.report_id}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }, [report])

  const handleShare = useCallback(async () => {
    if (!report) return
    setSharing(true)
    try {
      const base = typeof window !== "undefined" ? window.location.origin : ""
      const resp = await shareReport(report.report_id, base)
      if (!mountedRef.current) return
      setShareUrl(resp.url)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      if (mountedRef.current) setSharing(false)
    }
  }, [report])

  const handleCopyShareUrl = useCallback(async () => {
    if (!shareUrl) return
    await navigator.clipboard.writeText(shareUrl)
  }, [shareUrl])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Project Report"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <FileText className="w-4 h-4 text-[var(--artifact-purple,#a78bfa)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--artifact-purple,#a78bfa)]">
            PROJECT REPORT
          </h2>
        </div>
        {report && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={handleCopy}
              className="p-1 rounded hover:bg-white/10 text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
              aria-label="Copy to clipboard"
              title="Copy markdown to clipboard"
            >
              {copied ? <Check size={14} className="text-[var(--validation-emerald,#10b981)]" /> : <Copy size={14} />}
            </button>
            <button
              type="button"
              onClick={handleDownload}
              className="p-1 rounded hover:bg-white/10 text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
              aria-label="Download markdown"
              title="Download as .md file"
            >
              <Download size={14} />
            </button>
            <button
              type="button"
              onClick={handleShare}
              disabled={sharing}
              className="p-1 rounded hover:bg-white/10 text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors disabled:opacity-50"
              aria-label="Share report"
              title="Create share link"
            >
              {sharing ? <Loader2 size={14} className="animate-spin" /> : <Share2 size={14} />}
            </button>
          </div>
        )}
      </header>

      {shareUrl && (
        <div className="px-3 py-1.5 flex items-center gap-2 bg-[var(--artifact-purple,#a78bfa)]/10 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
          <Link2 size={12} className="text-[var(--artifact-purple,#a78bfa)] shrink-0" aria-hidden />
          <span className="font-mono text-[10px] text-[var(--foreground)] truncate flex-1" title={shareUrl}>
            {shareUrl}
          </span>
          <button
            type="button"
            onClick={handleCopyShareUrl}
            className="text-[10px] font-mono text-[var(--artifact-purple,#a78bfa)] hover:underline shrink-0"
            aria-label="Copy share URL"
          >
            COPY
          </button>
        </div>
      )}

      {loading && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground)] flex items-center justify-center gap-2">
          <Loader2 size={14} className="animate-spin" /> Generating report…
        </div>
      )}

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--destructive)] flex items-center gap-1">
          <AlertCircle size={12} aria-hidden /> {error}
        </div>
      )}

      {!loading && !report && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground)]">
          No report loaded — select a workflow run to generate a report.
        </div>
      )}

      {report && !loading && (
        <div className="divide-y divide-[var(--neural-border,rgba(148,163,184,0.2))]">
          <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--muted-foreground)]">
            <span title={report.report_id}>ID: {report.report_id}</span>
            <span className="mx-2">·</span>
            <span>{report.generated_at}</span>
          </div>
          {(["spec", "execution", "outcome"] as SectionKey[]).map((key) => {
            const sectionMd = extractSection(report.markdown, key)
            if (!sectionMd) return null
            const isCollapsed = collapsed[key]
            const Chevron = isCollapsed ? ChevronRight : ChevronDown
            return (
              <div key={key} data-section={key}>
                <button
                  type="button"
                  className="w-full px-3 py-2 flex items-center gap-2 hover:bg-white/5 text-left"
                  onClick={() => toggleSection(key)}
                  aria-expanded={!isCollapsed}
                  aria-label={`Section ${SECTION_HEADERS[key].label}`}
                >
                  <Chevron size={12} className="text-[var(--muted-foreground)] shrink-0" aria-hidden />
                  <span className="font-mono text-[11px] font-semibold text-[var(--foreground)]">
                    {SECTION_HEADERS[key].label}
                  </span>
                </button>
                {!isCollapsed && (
                  <div
                    className="px-5 pb-3 font-mono text-[10px] text-[var(--foreground)] leading-relaxed"
                    dangerouslySetInnerHTML={{ __html: markdownToHtml(sectionMd) }}
                  />
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
