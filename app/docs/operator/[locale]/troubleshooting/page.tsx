/** Troubleshooting doc viewer — mirrors reference/[slug]/page.tsx but
 * reads from docs/operator/<locale>/troubleshooting.md (top level). */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"
import { mdToHtml } from "@/lib/md-to-html"

type Params = { locale: string }

const LOCALES = new Set(["en", "zh-TW", "zh-CN", "ja"])

export const dynamic = "force-dynamic"

export default async function TroubleshootingPage(
  { params }: { params: Promise<Params> },
) {
  const { locale } = await params
  if (!LOCALES.has(locale)) notFound()

  const docPath = path.join(
    process.cwd(), "docs", "operator", locale, "troubleshooting.md",
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
      <div className="max-w-3xl mx-auto px-4 py-8">
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
        </nav>
        <article className="doc-article" dangerouslySetInnerHTML={{ __html: html }} />
      </div>
    </main>
  )
}

