/**
 * Tiny inline markdown → HTML renderer for the operator doc viewer.
 *
 * Intentionally small — no external dependency (we avoid pulling
 * react-markdown / remark just for headings, tables, lists, blockquotes,
 * inline/block code and links). Factored out of the per-route page.tsx
 * files during the D4 / E3 docs consolidation.
 *
 * Security model: every line is HTML-escaped first, then pattern-matched.
 * No raw HTML passthrough, so docs authors can't accidentally (or on
 * purpose) inject script. Links are href-filtered to http/https/relative.
 *
 * Supported syntax:
 *   # / ## / ### / ####       headings (h1..h4)
 *   **bold**                  <strong>
 *   *italic*                  <em>
 *   `inline code`             <code>
 *   ```fenced code```         <pre><code>
 *   > blockquote              <blockquote>
 *   - list  /  1. list        <ul>/<ol>
 *   | table | rows |          <table>, with `|---|` separator row
 *   [text](url)               <a> — .md suffix stripped for Next.js routes
 */

export function mdToHtml(src: string): string {
  const esc = (s: string) =>
    s
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")

  const lines = src.split(/\r?\n/)
  const out: string[] = []
  let inCode = false
  let inTable = false
  let inList: "ul" | "ol" | null = null

  const closeList = () => {
    if (inList) {
      out.push(`</${inList}>`)
      inList = null
    }
  }
  const closeTable = () => {
    if (inTable) {
      out.push("</tbody></table>")
      inTable = false
    }
  }

  const inline = (s: string): string => {
    let t = esc(s)
    t = t.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`)
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    t = t.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text, url) => {
      const safeUrl = /^(https?:|\/|\.{0,2}\/)/.test(url)
        ? url.replace(/"/g, "")
        : "#"
      // Strip .md so relative links map to Next.js routes.
      const fixed = safeUrl.endsWith(".md")
        ? safeUrl.replace(/\.md$/, "")
        : safeUrl
      return `<a href="${fixed}">${text}</a>`
    })
    return t
  }

  for (const line of lines) {
    // Fenced code block
    if (line.startsWith("```")) {
      if (!inCode) {
        closeList()
        closeTable()
        out.push("<pre><code>")
        inCode = true
      } else {
        out.push("</code></pre>")
        inCode = false
      }
      continue
    }
    if (inCode) {
      out.push(esc(line))
      continue
    }

    // Headings
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      closeList()
      closeTable()
      const n = h[1].length
      out.push(`<h${n}>${inline(h[2])}</h${n}>`)
      continue
    }

    // Blockquote
    if (line.startsWith("> ")) {
      closeList()
      closeTable()
      out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`)
      continue
    }

    // Table
    if (line.startsWith("|")) {
      const cells = line
        .slice(1, line.endsWith("|") ? -1 : undefined)
        .split("|")
        .map((s) => s.trim())
      const sep = cells.every((c) => /^:?-{2,}:?$/.test(c))
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

    // Lists
    const ul = /^[-*]\s+(.*)$/.exec(line)
    const ol = /^\d+\.\s+(.*)$/.exec(line)
    if (ul) {
      if (inList !== "ul") {
        closeList()
        out.push("<ul>")
        inList = "ul"
      }
      out.push(`<li>${inline(ul[1])}</li>`)
      continue
    }
    if (ol) {
      if (inList !== "ol") {
        closeList()
        out.push("<ol>")
        inList = "ol"
      }
      out.push(`<li>${inline(ol[1])}</li>`)
      continue
    } else {
      closeList()
    }

    // Blank line
    if (!line.trim()) {
      out.push("")
      continue
    }

    // Paragraph
    out.push(`<p>${inline(line)}</p>`)
  }

  closeList()
  closeTable()
  if (inCode) out.push("</code></pre>")
  return out.join("\n")
}
