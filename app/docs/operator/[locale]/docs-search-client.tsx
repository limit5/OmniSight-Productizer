"use client"

import { useMemo, useState } from "react"
import { Search, FileText, Home } from "lucide-react"

export interface DocEntry {
  href: string
  title: string
  headings: string[]
  paragraphs: string[]
}

const COPY = {
  en: { placeholder: "Search the docs…", heading: "Operator docs", empty: "No matches.", backToApp: "Back to dashboard" },
  "zh-TW": { placeholder: "搜尋文件……", heading: "操作員文件", empty: "無符合結果。", backToApp: "返回 dashboard" },
  "zh-CN": { placeholder: "搜索文档……", heading: "操作员文档", empty: "无匹配结果。", backToApp: "返回 dashboard" },
  ja: { placeholder: "ドキュメントを検索…", heading: "オペレーターマニュアル", empty: "該当なし。", backToApp: "ダッシュボードへ戻る" },
} as const

type Locale = keyof typeof COPY

export function DocsSearchClient({
  locale,
  entries,
}: {
  locale: string
  entries: DocEntry[]
}) {
  // TS narrows COPY's literal shape too tightly to be compatible with
  // a generic string index. Go through `unknown` per the compiler
  // hint — we fall back to `COPY.en` anyway when the locale misses.
  const L = (COPY as unknown as Record<string, typeof COPY.en>)[locale] ?? COPY.en
  const [q, setQ] = useState("")
  const needle = q.trim().toLowerCase()

  const results = useMemo(() => {
    if (!needle) {
      return entries.map((e) => ({ entry: e, score: 0, snippet: "" }))
    }
    const terms = needle.split(/\s+/).filter(Boolean)
    return entries
      .map((e) => {
        let score = 0
        let snippet = ""
        for (const term of terms) {
          if (e.title.toLowerCase().includes(term)) score += 5
          for (const h of e.headings) {
            if (h.toLowerCase().includes(term)) score += 3
          }
          for (const p of e.paragraphs) {
            const i = p.toLowerCase().indexOf(term)
            if (i >= 0) {
              score += 1
              if (!snippet) {
                const start = Math.max(0, i - 40)
                const end = Math.min(p.length, i + term.length + 60)
                snippet =
                  (start > 0 ? "…" : "") +
                  p.slice(start, end) +
                  (end < p.length ? "…" : "")
              }
            }
          }
        }
        return { entry: e, score, snippet }
      })
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score)
  }, [entries, needle])

  return (
    <main className="min-h-screen bg-[var(--background,#010409)] text-[var(--foreground,#e2e8f0)]">
      <div className="max-w-3xl mx-auto px-4 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="font-mono text-lg tracking-[0.15em] text-[var(--neural-cyan,#67e8f9)]">
            {L.heading}
          </h1>
          <a
            href="/"
            className="flex items-center gap-1 font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--neural-cyan,#67e8f9)]"
          >
            <Home className="w-3.5 h-3.5" aria-hidden />
            {L.backToApp}
          </a>
        </div>
        <div className="relative mb-6">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--muted-foreground,#94a3b8)]"
            aria-hidden
          />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={L.placeholder}
            autoFocus
            aria-label={L.placeholder}
            className="w-full pl-9 pr-3 py-2 rounded-sm bg-[var(--holo-glass,rgba(0,242,255,0.03))] border border-[var(--neural-cyan,#67e8f9)]/30 focus:border-[var(--neural-cyan,#67e8f9)] focus:outline-none font-mono text-sm text-[var(--foreground,#e2e8f0)] placeholder-[var(--muted-foreground,#94a3b8)]"
          />
        </div>
        <ul className="space-y-2">
          {results.length === 0 && (
            <li className="font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)] text-center py-6">
              {L.empty}
            </li>
          )}
          {results.map(({ entry, snippet }) => (
            <li key={entry.href}>
              <a
                href={entry.href}
                className="block px-3 py-2 rounded-sm border border-white/5 hover:border-[var(--neural-cyan,#67e8f9)]/40 hover:bg-white/5 transition-colors"
              >
                <div className="flex items-center gap-2 mb-0.5">
                  <FileText className="w-3.5 h-3.5 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
                  <span className="font-mono text-sm text-[var(--foreground,#e2e8f0)]">
                    {entry.title}
                  </span>
                </div>
                {snippet && (
                  <div className="font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)] leading-snug pl-5">
                    {snippet}
                  </div>
                )}
                {!snippet && entry.headings.length > 0 && (
                  <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] pl-5">
                    {entry.headings.slice(0, 4).join(" · ")}
                  </div>
                )}
              </a>
            </li>
          ))}
        </ul>
      </div>
    </main>
  )
}
