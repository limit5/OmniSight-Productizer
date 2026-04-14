/**
 * Docs landing + client-side search.
 *
 * Reads every .md under docs/operator/<locale>/ at render time, builds a
 * flat { doc, title, headings, body-snippets } index, and hands it to a
 * client component that does case-insensitive keyword filtering. No
 * external search engine dep — the corpus is tiny (six docs × 200 lines)
 * so filtering in-browser is fast and keeps the feature self-contained.
 */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"
import { DocsSearchClient, type DocEntry } from "./docs-search-client"

type Params = { locale: string }

const LOCALES = new Set(["en", "zh-TW", "zh-CN", "ja"])

// (slug, route) — `route` is what we link to; a null `slug` means
// top-level page (only troubleshooting for now).
const DOC_ROUTES: Array<{ slug: string | null; file: string; route: string }> = [
  { slug: "operation-modes",    file: "reference/operation-modes.md",    route: "reference/operation-modes" },
  { slug: "decision-severity",  file: "reference/decision-severity.md",  route: "reference/decision-severity" },
  { slug: "panels-overview",    file: "reference/panels-overview.md",    route: "reference/panels-overview" },
  { slug: "budget-strategies",  file: "reference/budget-strategies.md",  route: "reference/budget-strategies" },
  { slug: "glossary",           file: "reference/glossary.md",           route: "reference/glossary" },
  { slug: null,                 file: "troubleshooting.md",              route: "troubleshooting" },
  { slug: "first-invoke",       file: "tutorial/first-invoke.md",        route: "tutorial/first-invoke" },
  { slug: "handling-a-decision", file: "tutorial/handling-a-decision.md", route: "tutorial/handling-a-decision" },
]

export const dynamic = "force-dynamic"

export default async function DocsIndexPage(
  { params }: { params: Promise<Params> },
) {
  const { locale } = await params
  if (!LOCALES.has(locale)) notFound()

  const entries: DocEntry[] = []
  for (const d of DOC_ROUTES) {
    const docPath = path.join(process.cwd(), "docs", "operator", locale, d.file)
    let raw: string
    try {
      raw = await fs.readFile(docPath, "utf-8")
    } catch {
      continue
    }
    entries.push(buildEntry(raw, `/docs/operator/${locale}/${d.route}`))
  }

  return <DocsSearchClient locale={locale} entries={entries} />
}

/** Extract { title, headings, paragraphs } from markdown so the client
 * can search all three with different weights. */
function buildEntry(md: string, href: string): DocEntry {
  const lines = md.split(/\r?\n/)
  let title = href.split("/").pop() ?? ""
  const headings: string[] = []
  const paragraphs: string[] = []
  for (const line of lines) {
    const h1 = /^#\s+(.*)$/.exec(line)
    const h2 = /^##\s+(.*)$/.exec(line)
    const h3 = /^###\s+(.*)$/.exec(line)
    if (h1 && title === href.split("/").pop()) title = h1[1].trim()
    if (h2) headings.push(h2[1].trim())
    if (h3) headings.push(h3[1].trim())
    if (line.trim() && !line.startsWith("#") && !line.startsWith("|") && !line.startsWith(">")) {
      // Plain prose line, keep for snippet search.
      paragraphs.push(line.trim())
    }
  }
  return { href, title, headings, paragraphs }
}
