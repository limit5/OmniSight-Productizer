/**
 * DocLayout — shared shell for every operator-doc viewer (reference /
 * tutorial / troubleshooting). Adds:
 *   - sticky TOC sidebar extracted from the source markdown,
 *   - breadcrumb nav at top,
 *   - prev / next navigation at the bottom tying the docs into a
 *     single reading flow.
 *
 * The list of docs in reading order is defined here as the single
 * source of truth. Routers hand the slug + rendered HTML + TOC and the
 * layout computes adjacency.
 */

import type { TocEntry } from "@/lib/md-to-html"
import { ArrowLeft, ArrowRight, Home, List } from "lucide-react"

export type DocKind = "reference" | "tutorial" | "troubleshooting"

export interface DocDescriptor {
  kind: DocKind
  slug: string | null          // null for top-level (troubleshooting)
  title: Record<string, string>
  /** Path segment under /docs/operator/<locale>/. */
  route: string
}

// Reading order — what "prev" and "next" navigate between. Keep in
// sync with the search index at app/docs/operator/[locale]/page.tsx.
export const DOC_ORDER: DocDescriptor[] = [
  {
    kind: "tutorial", slug: "first-invoke", route: "tutorial/first-invoke",
    title: { en: "First Invoke", "zh-TW": "第一次 Invoke", "zh-CN": "第一次 Invoke", ja: "はじめての Invoke" },
  },
  {
    kind: "tutorial", slug: "handling-a-decision", route: "tutorial/handling-a-decision",
    title: { en: "Handling a decision", "zh-TW": "處理一個決策", "zh-CN": "处理一个决策", ja: "決定の扱い方" },
  },
  {
    kind: "reference", slug: "operation-modes", route: "reference/operation-modes",
    title: { en: "Operation Modes", "zh-TW": "Operation Modes", "zh-CN": "Operation Modes", ja: "Operation Modes" },
  },
  {
    kind: "reference", slug: "decision-severity", route: "reference/decision-severity",
    title: { en: "Decision Severity", "zh-TW": "Decision Severity", "zh-CN": "Decision Severity", ja: "Decision Severity" },
  },
  {
    kind: "reference", slug: "panels-overview", route: "reference/panels-overview",
    title: { en: "Panels Overview", "zh-TW": "Panels Overview", "zh-CN": "Panels Overview", ja: "Panels Overview" },
  },
  {
    kind: "reference", slug: "budget-strategies", route: "reference/budget-strategies",
    title: { en: "Budget Strategies", "zh-TW": "Budget Strategies", "zh-CN": "Budget Strategies", ja: "Budget Strategies" },
  },
  {
    kind: "reference", slug: "glossary", route: "reference/glossary",
    title: { en: "Glossary", "zh-TW": "Glossary", "zh-CN": "Glossary", ja: "Glossary" },
  },
  {
    kind: "troubleshooting", slug: null, route: "troubleshooting",
    title: { en: "Troubleshooting", "zh-TW": "Troubleshooting", "zh-CN": "Troubleshooting", ja: "Troubleshooting" },
  },
]

const LABELS: Record<string, { onThisPage: string; prev: string; next: string; index: string; dashboard: string }> = {
  en:      { onThisPage: "On this page", prev: "Previous", next: "Next", index: "Docs index",  dashboard: "Dashboard" },
  "zh-TW": { onThisPage: "本頁目錄",      prev: "上一篇",   next: "下一篇", index: "文件索引",  dashboard: "返回 Dashboard" },
  "zh-CN": { onThisPage: "本页目录",      prev: "上一篇",   next: "下一篇", index: "文档索引",  dashboard: "返回 Dashboard" },
  ja:      { onThisPage: "目次",         prev: "前へ",    next: "次へ",  index: "ドキュメント索引", dashboard: "Dashboard へ" },
}

interface Props {
  locale: string
  /** Current doc route segment (e.g. "reference/operation-modes"). */
  route: string
  toc: TocEntry[]
  /** Rendered HTML (the server ran `mdToHtml(raw, { withIds: true })`). */
  html: string
}

export function DocLayout({ locale, route, toc, html }: Props) {
  const L = LABELS[locale] ?? LABELS.en
  const idx = DOC_ORDER.findIndex((d) => d.route === route)
  const prev = idx > 0 ? DOC_ORDER[idx - 1] : null
  const next = idx >= 0 && idx < DOC_ORDER.length - 1 ? DOC_ORDER[idx + 1] : null
  const currentTitle =
    idx >= 0 ? DOC_ORDER[idx].title[locale] ?? DOC_ORDER[idx].title.en : ""

  return (
    <main className="min-h-screen bg-[var(--background,#010409)] text-[var(--foreground,#e2e8f0)]">
      <div className="max-w-5xl mx-auto px-4 py-6 lg:grid lg:grid-cols-[1fr_220px] lg:gap-8">
        <div className="min-w-0">
          {/* Breadcrumb */}
          <nav className="mb-4 font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)] flex items-center gap-3 flex-wrap">
            <a href="/" className="flex items-center gap-1 hover:text-[var(--neural-cyan,#67e8f9)]">
              <Home className="w-3 h-3" aria-hidden />
              {L.dashboard}
            </a>
            <span aria-hidden>·</span>
            <a href={`/docs/operator/${locale}`} className="hover:text-[var(--neural-cyan,#67e8f9)]">
              {L.index}
            </a>
            {currentTitle && (
              <>
                <span aria-hidden>·</span>
                <span className="text-[var(--foreground,#e2e8f0)]">{currentTitle}</span>
              </>
            )}
          </nav>

          {/* Article */}
          <article
            className="doc-article"
            dangerouslySetInnerHTML={{ __html: html }}
          />

          {/* Prev / next */}
          <nav
            className="mt-8 pt-4 border-t border-[var(--neural-border,rgba(148,163,184,0.25))] grid grid-cols-2 gap-3"
            aria-label="doc pagination"
          >
            {prev ? (
              <a
                href={`/docs/operator/${locale}/${prev.route}`}
                className="group rounded-sm border border-white/5 hover:border-[var(--neural-cyan,#67e8f9)]/40 p-3 transition-colors"
              >
                <div className="flex items-center gap-1 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] mb-1">
                  <ArrowLeft className="w-3 h-3" aria-hidden />
                  {L.prev}
                </div>
                <div className="font-mono text-xs text-[var(--foreground,#e2e8f0)] group-hover:text-[var(--neural-cyan,#67e8f9)]">
                  {prev.title[locale] ?? prev.title.en}
                </div>
              </a>
            ) : <span />}
            {next ? (
              <a
                href={`/docs/operator/${locale}/${next.route}`}
                className="group rounded-sm border border-white/5 hover:border-[var(--neural-cyan,#67e8f9)]/40 p-3 text-right transition-colors"
              >
                <div className="flex items-center justify-end gap-1 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] mb-1">
                  {L.next}
                  <ArrowRight className="w-3 h-3" aria-hidden />
                </div>
                <div className="font-mono text-xs text-[var(--foreground,#e2e8f0)] group-hover:text-[var(--neural-cyan,#67e8f9)]">
                  {next.title[locale] ?? next.title.en}
                </div>
              </a>
            ) : <span />}
          </nav>
        </div>

        {/* Sticky TOC */}
        {toc.length > 0 && (
          <aside
            className="hidden lg:block"
            aria-label={L.onThisPage}
          >
            <div className="sticky top-6">
              <div className="flex items-center gap-1.5 mb-2 font-mono text-[10px] tracking-[0.2em] text-[var(--neural-cyan,#67e8f9)] uppercase">
                <List className="w-3 h-3" aria-hidden />
                {L.onThisPage}
              </div>
              <ul className="space-y-1 font-mono text-[11px]">
                {toc.map((h) => (
                  <li key={h.id} className={h.level === 3 ? "pl-3" : ""}>
                    <a
                      href={`#${h.id}`}
                      className="block py-0.5 text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--neural-cyan,#67e8f9)] transition-colors border-l border-transparent hover:border-[var(--neural-cyan,#67e8f9)] pl-2 -ml-2"
                    >
                      {h.text}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          </aside>
        )}
      </div>
    </main>
  )
}
