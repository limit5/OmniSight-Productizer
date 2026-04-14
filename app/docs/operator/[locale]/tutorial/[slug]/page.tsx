/** Tutorial doc viewer — wraps the shared DocLayout. */

import { promises as fs } from "node:fs"
import path from "node:path"
import { notFound } from "next/navigation"
import { mdToHtml, extractToc } from "@/lib/md-to-html"
import { DocLayout } from "@/components/docs/doc-layout"

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

  const toc = extractToc(raw)
  const html = mdToHtml(raw, { withIds: true })

  return (
    <DocLayout
      locale={locale}
      route={`tutorial/${slug}`}
      toc={toc}
      html={html}
    />
  )
}
