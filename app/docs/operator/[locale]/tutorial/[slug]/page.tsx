/** Tutorial doc viewer — mirrors reference/[slug]/page.tsx but reads
 * from docs/operator/<locale>/tutorial/<slug>.md. */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"
import { mdToHtml } from "@/lib/md-to-html"

type Params = { locale: string; slug: string }

const LOCALES = new Set(["en", "zh-TW", "zh-CN", "ja"])
const SLUGS = new Set(["first-invoke", "handling-a-decision"])

export const dynamic = "force-dynamic"

export default async function TutorialPage(
  { params }: { params: Promise<Params> },
) {
  const { locale, slug } = await params
  if (!LOCALES.has(locale) || !SLUGS.has(slug)) notFound()

  const docPath = path.join(
    process.cwd(), "docs", "operator", locale, "tutorial", `${slug}.md`,
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
          <a href={`/docs/operator/${locale}`} className="hover:text-[var(--neural-cyan,#67e8f9)]">index</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/tutorial/first-invoke`} className="hover:text-[var(--neural-cyan,#67e8f9)]">first-invoke</a>
          {"   "}·{"   "}
          <a href={`/docs/operator/${locale}/tutorial/handling-a-decision`} className="hover:text-[var(--neural-cyan,#67e8f9)]">handling-a-decision</a>
        </nav>
        <article className="doc-article" dangerouslySetInnerHTML={{ __html: html }} />
      </div>
    </main>
  )
}
