/** Troubleshooting doc viewer — wraps the shared DocLayout. */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"
import { mdToHtml, extractToc } from "@/lib/md-to-html"
import { DocLayout } from "@/components/docs/doc-layout"

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

  const toc = extractToc(raw)
  const html = mdToHtml(raw, { withIds: true })

  return <DocLayout locale={locale} route="troubleshooting" toc={toc} html={html} />
}
