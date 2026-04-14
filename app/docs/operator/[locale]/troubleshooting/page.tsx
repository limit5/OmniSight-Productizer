/** Troubleshooting doc viewer — mirrors reference/[slug]/page.tsx but
 * reads from docs/operator/<locale>/troubleshooting.md (top level). */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"

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

/** Identical inline markdown renderer used by reference/[slug]/page.tsx.
 * Kept duplicated rather than factored out so each route file is a
 * single self-contained server component. */
function mdToHtml(src: string): string {
  const esc = (s: string) => s
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;")
  const lines = src.split(/\r?\n/)
  const out: string[] = []
  let inCode = false, inTable = false
  let inList: "ul" | "ol" | null = null
  const closeList = () => { if (inList) { out.push(`</${inList}>`); inList = null } }
  const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false } }
  const inline = (s: string): string => {
    let t = esc(s)
    t = t.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`)
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    t = t.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text, url) => {
      const safeUrl = /^(https?:|\/|\.{0,2}\/)/.test(url) ? url.replace(/"/g, "") : "#"
      const fixed = safeUrl.endsWith(".md") ? safeUrl.replace(/\.md$/, "") : safeUrl
      return `<a href="${fixed}">${text}</a>`
    })
    return t
  }
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (!inCode) { closeList(); closeTable(); out.push("<pre><code>"); inCode = true }
      else { out.push("</code></pre>"); inCode = false }
      continue
    }
    if (inCode) { out.push(esc(line)); continue }
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) { closeList(); closeTable(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue }
    if (line.startsWith("> ")) { closeList(); closeTable(); out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`); continue }
    if (line.startsWith("|")) {
      const cells = line.slice(1, line.endsWith("|") ? -1 : undefined).split("|").map(s => s.trim())
      const sep = cells.every(c => /^:?-{2,}:?$/.test(c))
      if (sep) continue
      if (!inTable) {
        closeList(); out.push("<table><thead><tr>")
        for (const c of cells) out.push(`<th>${inline(c)}</th>`)
        out.push("</tr></thead><tbody>"); inTable = true
      } else {
        out.push("<tr>")
        for (const c of cells) out.push(`<td>${inline(c)}</td>`)
        out.push("</tr>")
      }
      continue
    } else if (inTable) { closeTable() }
    const ul = /^[-*]\s+(.*)$/.exec(line)
    const ol = /^\d+\.\s+(.*)$/.exec(line)
    if (ul) {
      if (inList !== "ul") { closeList(); out.push("<ul>"); inList = "ul" }
      out.push(`<li>${inline(ul[1])}</li>`); continue
    }
    if (ol) {
      if (inList !== "ol") { closeList(); out.push("<ol>"); inList = "ol" }
      out.push(`<li>${inline(ol[1])}</li>`); continue
    } else { closeList() }
    if (!line.trim()) { out.push(""); continue }
    out.push(`<p>${inline(line)}</p>`)
  }
  closeList(); closeTable()
  if (inCode) out.push("</code></pre>")
  return out.join("\n")
}
