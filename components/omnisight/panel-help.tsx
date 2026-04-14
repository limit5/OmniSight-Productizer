"use client"

/**
 * PanelHelp — the `?` icon that lives in every panel header and lets
 * the operator jump to the matching reference doc in their current
 * UI language. Added D2 (2026-04-14) per "what does this button do"
 * feedback — the UI alone could not answer it.
 *
 * The component is intentionally minimal: click opens a popover that
 * shows a short inline TL;DR plus a "Full doc" link that points at
 * `docs/operator/<locale>/reference/<doc>.md`. A richer in-app doc
 * viewer lands in D4.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { HelpCircle, ExternalLink } from "lucide-react"
import { useI18n as _useI18n, type Locale } from "@/lib/i18n/context"

// Tolerant hook — if PanelHelp is rendered outside an I18nProvider
// (e.g. in a unit test that only mounts a single component), fall
// back to English instead of throwing. Production `app/layout.tsx`
// always provides the context.
function useLocale(): Locale {
  try {
    return _useI18n().locale
  } catch {
    return "en"
  }
}

export type DocId =
  | "operation-modes"
  | "decision-severity"
  | "panels-overview"
  | "budget-strategies"
  | "glossary"

// Inline TL;DR — kept in sync with the first TL;DR paragraph of each
// matching .md file. If you change wording there, mirror it here.
const TL_DR: Record<DocId, Record<Locale, { title: string; summary: string }>> = {
  "operation-modes": {
    en: {
      title: "Operation Modes",
      summary:
        "MODE decides how much the AI can do without asking. Four settings from MANUAL (ask everything) to TURBO (auto-run everything with a 60 s escape). Colour = risk level.",
    },
    "zh-TW": {
      title: "Operation Modes",
      summary:
        "MODE 決定 AI 不問您的情況下能做到哪一步。四個等級從 MANUAL（事事問您）到 TURBO（全部自動含破壞性，60 秒撤銷視窗）。顏色對應風險。",
    },
    "zh-CN": {
      title: "Operation Modes",
      summary:
        "MODE 决定 AI 不问您的情况下能做到哪一步。四个等级从 MANUAL（事事问您）到 TURBO（全部自动含破坏性，60 秒撤销窗口）。颜色对应风险。",
    },
    ja: {
      title: "Operation Modes",
      summary:
        "MODE は AI があなたに聞かずにどこまでやっていいかを決めます。4 段階、MANUAL(全て聞く)から TURBO(全自動・60 秒取消猶予)まで。色がリスクレベル。",
    },
  },
  "decision-severity": {
    en: {
      title: "Decision Severity",
      summary:
        "Every AI decision carries a risk label: info / routine / risky / destructive. Label decides icon, colour, countdown, and whether MODE auto-executes. Pay attention to destructive.",
    },
    "zh-TW": {
      title: "Decision Severity",
      summary:
        "每個 AI 決策帶風險標籤：info / routine / risky / destructive。標籤決定圖示、顏色、倒數條、以及 MODE 是否自動執行。最該留意 destructive。",
    },
    "zh-CN": {
      title: "Decision Severity",
      summary:
        "每个 AI 决策带风险标签：info / routine / risky / destructive。标签决定图标、颜色、倒计时条、以及 MODE 是否自动执行。最该留意 destructive。",
    },
    ja: {
      title: "Decision Severity",
      summary:
        "全ての AI 決定はリスクラベル(info / routine / risky / destructive)を持ちます。ラベルがアイコン・色・カウントダウン・MODE 自動実行可否を決定。特に destructive に注意。",
    },
  },
  "panels-overview": {
    en: {
      title: "Panels Overview",
      summary:
        "The dashboard has 12 panels. This reference lists each panel's one-line job, URL deep-link, keyboard shortcuts, and mobile nav behaviour.",
    },
    "zh-TW": {
      title: "Panels Overview",
      summary:
        "Dashboard 共 12 個 panel。此參考列出各 panel 的一句話職責、URL 深鏈、鍵盤快速鍵與手機導航行為。",
    },
    "zh-CN": {
      title: "Panels Overview",
      summary:
        "Dashboard 共 12 个 panel。此参考列出各 panel 的一句话职责、URL 深链、键盘快捷键与手机导航行为。",
    },
    ja: {
      title: "Panels Overview",
      summary:
        "Dashboard は 12 個の panel で構成。各 panel の一言役割、URL ディープリンク、キーボードショートカット、スマホナビ挙動を網羅。",
    },
  },
  "budget-strategies": {
    en: {
      title: "Budget Strategies",
      summary:
        "Four preset bundles of five tuning knobs (tier, retries, downgrade, freeze, parallel) picking how expensive each agent call is allowed to be. QUALITY / BALANCED / COST_SAVER / SPRINT.",
    },
    "zh-TW": {
      title: "Budget Strategies",
      summary:
        "五個 knob（tier/retries/downgrade/freeze/parallel）的四種預設組合，決定每次 agent 呼叫允許多貴。QUALITY / BALANCED / COST_SAVER / SPRINT。",
    },
    "zh-CN": {
      title: "Budget Strategies",
      summary:
        "五个 knob（tier/retries/downgrade/freeze/parallel）的四种预设组合，决定每次 agent 调用允许多贵。QUALITY / BALANCED / COST_SAVER / SPRINT。",
    },
    ja: {
      title: "Budget Strategies",
      summary:
        "5 knob (tier/retries/downgrade/freeze/parallel) の 4 プリセット組合で agent 呼び出しの許容コストを決定。QUALITY / BALANCED / COST_SAVER / SPRINT。",
    },
  },
  glossary: {
    en: {
      title: "Glossary",
      summary:
        "Domain-specific terms the UI and logs use (agent, task, pipeline, NPI, sweep, workspace, SSE, …) with canonical definitions.",
    },
    "zh-TW": {
      title: "Glossary 名詞解釋",
      summary:
        "UI 與 log 使用的專有名詞（agent、task、pipeline、NPI、sweep、workspace、SSE……）及其權威定義。",
    },
    "zh-CN": {
      title: "Glossary 术语表",
      summary:
        "UI 与 log 使用的专有名词（agent、task、pipeline、NPI、sweep、workspace、SSE……）及其权威定义。",
    },
    ja: {
      title: "Glossary 用語集",
      summary:
        "UI と log で使われる専門用語(agent、task、pipeline、NPI、sweep、workspace、SSE…)の正本定義。",
    },
  },
}

const LABELS: Record<Locale, { help: string; fullDoc: string; close: string }> = {
  en:      { help: "Help",      fullDoc: "Full docs →",        close: "Close" },
  "zh-TW": { help: "說明",      fullDoc: "完整文件 →",          close: "關閉" },
  "zh-CN": { help: "说明",      fullDoc: "完整文档 →",          close: "关闭" },
  ja:      { help: "ヘルプ",    fullDoc: "ドキュメント全文 →", close: "閉じる" },
}

interface PanelHelpProps {
  doc: DocId
  /** Optional extra className on the trigger button (e.g. size override). */
  className?: string
  /** Set on the first instance rendered in the header so the first-run
   * tour can anchor its final step to a visible `?` icon. */
  tourAnchor?: boolean
}

const POPOVER_W = 320
const POPOVER_GAP = 4
const VIEWPORT_PAD = 8

export function PanelHelp({ doc, className, tourAnchor }: PanelHelpProps) {
  const locale = useLocale()
  const [open, setOpen] = useState(false)
  const popRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  // Pixel coords for the portaled popover. Null until we've measured
  // the trigger — avoids a one-frame flash at (0,0) on open.
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const [mounted, setMounted] = useState(false)
  useEffect(() => { setMounted(true) }, [])

  // Close on outside click or Escape.
  useEffect(() => {
    if (!open) return
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node
      if (popRef.current?.contains(t) || triggerRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); triggerRef.current?.focus() }
    }
    document.addEventListener("mousedown", onDocClick)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDocClick)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  // Position the portaled popover relative to the trigger button.
  // Right-align to the trigger (matches the old `right-0` anchor) but
  // clamp to the viewport so the left edge never clips — this is the
  // whole reason we portal: escape the clipping `overflow-x-hidden`
  // on the far-right aside that used to cut popovers off at the
  // column's left edge.
  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return
    const recompute = () => {
      const rect = triggerRef.current!.getBoundingClientRect()
      const vw = window.innerWidth
      const top = rect.bottom + POPOVER_GAP
      // Prefer right-aligned to the trigger. Clamp left so popover
      // stays entirely on screen with an 8 px margin.
      let left = rect.right - POPOVER_W
      if (left < VIEWPORT_PAD) left = VIEWPORT_PAD
      if (left + POPOVER_W > vw - VIEWPORT_PAD) {
        left = vw - POPOVER_W - VIEWPORT_PAD
      }
      setPos({ top, left })
    }
    recompute()
    window.addEventListener("resize", recompute)
    window.addEventListener("scroll", recompute, true)
    return () => {
      window.removeEventListener("resize", recompute)
      window.removeEventListener("scroll", recompute, true)
    }
  }, [open])

  const tldr = TL_DR[doc][locale]
  const label = LABELS[locale]
  const docUrl = `/docs/operator/${locale}/reference/${doc}`

  return (
    <div
      className="relative inline-flex"
      data-tour={tourAnchor ? "panel-help" : undefined}
    >
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={`${label.help}: ${tldr.title}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        className={`p-1 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--neural-cyan,#67e8f9)] hover:bg-white/5 transition-colors ${className ?? ""}`}
      >
        <HelpCircle className="w-3.5 h-3.5" aria-hidden />
      </button>
      {open && mounted && pos && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label={tldr.title}
          style={{
            position: "fixed",
            top: pos.top,
            left: pos.left,
            width: POPOVER_W,
          }}
          className="z-[100] holo-glass-simple rounded-sm border border-[var(--neural-cyan,#67e8f9)]/40 shadow-lg p-3 font-mono text-[11px] leading-snug text-[var(--foreground,#e2e8f0)]"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-1.5">
            <span className="tracking-wider text-[var(--neural-cyan,#67e8f9)] font-semibold">
              {tldr.title}
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label={label.close}
              className="text-[var(--muted-foreground,#94a3b8)] hover:text-white"
            >
              ✕
            </button>
          </div>
          <p className="mb-2">{tldr.summary}</p>
          <a
            href={docUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[var(--neural-cyan,#67e8f9)] hover:underline"
          >
            {label.fullDoc} <ExternalLink className="w-3 h-3" aria-hidden />
          </a>
        </div>,
        document.body,
      )}
    </div>
  )
}
