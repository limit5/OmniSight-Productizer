/**
 * In-app operator-doc viewer (D2 scaffold).
 *
 * Reads docs/operator/<locale>/reference/<slug>.md from disk and
 * renders it with a tiny inline markdown→HTML converter. No external
 * deps — we avoid pulling in react-markdown / remark just for headers,
 * tables and links.
 *
 * D4 will replace this with a proper static MDX build + search. For
 * now the aim is: `?` icon in a panel → full doc visible in the same
 * browser, in the current UI language.
 */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"
import { mdToHtml } from "@/lib/md-to-html"

type Params = { locale: string; slug: string }

const LOCALES = new Set(["en", "zh-TW", "zh-CN", "ja"])
const SLUGS = new Set([
  "operation-modes", "decision-severity", "panels-overview",
  "budget-strategies", "glossary",
])

export const dynamic = "force-dynamic"

export default async function DocPage({ params }: { params: Promise<Params> }) {
  const { locale, slug } = await params
  if (!LOCALES.has(locale) || !SLUGS.has(slug)) notFound()

  const docPath = path.join(
    process.cwd(), "docs", "operator", locale, "reference", `${slug}.md`,
  )
  let raw: string
  try {
    raw = await fs.readFile(docPath, "utf-8")
  } catch {
    notFound()
  }

  const html = mdToHtml(raw)

  return (
    <main className="min-h-screen bg-[var(--background,#010409)] text-[var(--foreground,#e2e8f0)]">
      <div className="max-w-3xl mx-auto px-4 py-8 prose-invert">
        <nav className="mb-4 font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)]">
          <a href="/" className="hover:text-[var(--neural-cyan,#67e8f9)]">← dashboard</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/reference/operation-modes`} className="hover:text-[var(--neural-cyan,#67e8f9)]">modes</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/reference/decision-severity`} className="hover:text-[var(--neural-cyan,#67e8f9)]">severity</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/reference/panels-overview`} className="hover:text-[var(--neural-cyan,#67e8f9)]">panels</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/reference/budget-strategies`} className="hover:text-[var(--neural-cyan,#67e8f9)]">budget</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/reference/glossary`} className="hover:text-[var(--neural-cyan,#67e8f9)]">glossary</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/troubleshooting`} className="hover:text-[var(--neural-cyan,#67e8f9)]">troubleshooting</a>
        </nav>
        <article
          className="doc-article"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    </main>
  )
}

