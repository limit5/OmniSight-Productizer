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

type Params = { locale: string; slug: string }

const LOCALES = new Set(["en", "zh-TW", "zh-CN", "ja"])
const SLUGS = new Set([
  "operation-modes", "decision-severity", "panels-overview", "glossary",
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
          <a href={`/docs/operator/${locale}/reference/glossary`} className="hover:text-[var(--neural-cyan,#67e8f9)]">glossary</a>
        </nav>
        <article
          className="doc-article"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    </main>
  )
}

/** Minimal, safe-ish markdown → HTML. Supports: headings, bold, italic,
 *  inline code, code blocks, lists, tables, blockquotes, links.
 *  Intentionally small — no HTML passthrough, all text is escaped first. */
function mdToHtml(src: string): string {
  const esc = (s: string) => s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")

  const lines = src.split(/\r?\n/)
  const out: string[] = []
  let inCode = false
  let inTable = false
  let inList: "ul" | "ol" | null = null

  const closeList = () => { if (inList) { out.push(`</${inList}>`); inList = null } }
  const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false } }

  const inline = (s: string): string => {
    // Escape first, then apply inline rules to the escaped string.
    let t = esc(s)
    // inline code
    t = t.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`)
    // bold **x**
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    // italic *x* (avoid matching inside <strong>…</strong>)
    t = t.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    // links [text](url)
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text, url) => {
      const safeUrl = /^(https?:|\/|\.{0,2}\/)/.test(url)
        ? url.replace(/"/g, "")
        : "#"
      // Rewrite relative .md links to /docs route
      const fixed = safeUrl.endsWith(".md")
        ? safeUrl.replace(/\.md$/, "")
        : safeUrl
      return `<a href="${fixed}">${text}</a>`
    })
    return t
  }

  for (const rawLine of lines) {
    const line = rawLine
    // Fenced code block
    if (line.startsWith("```")) {
      if (!inCode) {
        closeList(); closeTable()
        out.push("<pre><code>")
        inCode = true
      } else {
        out.push("</code></pre>")
        inCode = false
      }
      continue
    }
    if (inCode) { out.push(esc(line)); continue }

    // Headings
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      closeList(); closeTable()
      const n = h[1].length
      out.push(`<h${n}>${inline(h[2])}</h${n}>`)
      continue
    }

    // Blockquote
    if (line.startsWith("> ")) {
      closeList(); closeTable()
      out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`)
      continue
    }

    // Table (pipe-style). A header row is followed by a separator row
    // `| --- | --- |`; subsequent piped rows are data.
    if (line.startsWith("|")) {
      const cells = line.slice(1, line.endsWith("|") ? -1 : undefined).split("|").map(s => s.trim())
      const sep = cells.every(c => /^:?-{2,}:?$/.test(c))
      if (sep) continue
      if (!inTable) {
        closeList()
        out.push("<table><thead><tr>")
        for (const c of cells) out.push(`<th>${inline(c)}</th>`)
        out.push("</tr></thead><tbody>")
        inTable = true
      } else {
        out.push("<tr>")
        for (const c of cells) out.push(`<td>${inline(c)}</td>`)
        out.push("</tr>")
      }
      continue
    } else if (inTable) {
      closeTable()
    }

    // List
    const ul = /^[-*]\s+(.*)$/.exec(line)
    const ol = /^\d+\.\s+(.*)$/.exec(line)
    if (ul) {
      if (inList !== "ul") { closeList(); out.push("<ul>"); inList = "ul" }
      out.push(`<li>${inline(ul[1])}</li>`)
      continue
    }
    if (ol) {
      if (inList !== "ol") { closeList(); out.push("<ol>"); inList = "ol" }
      out.push(`<li>${inline(ol[1])}</li>`)
      continue
    } else {
      closeList()
    }

    // Blank line
    if (!line.trim()) { out.push(""); continue }

    // Paragraph
    out.push(`<p>${inline(line)}</p>`)
  }
  closeList(); closeTable()
  if (inCode) out.push("</code></pre>")
  return out.join("\n")
}
