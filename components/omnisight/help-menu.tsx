"use client"

/**
 * Global Help dropdown (E3 / D4).
 *
 * Lives in the top-right of the dashboard header. Exposes the things
 * operators ask for most:
 *   - Run the tour again (`?tour=1`)
 *   - Jump to any reference doc in their current UI language
 *   - Open troubleshooting
 *   - Quick-link to Swagger (/docs) and the GitHub repo
 *
 * Deliberately stateless — the dropdown is just anchored links; the
 * docs router does the real work.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import {
  HelpCircle,
  BookOpen,
  Compass,
  AlertTriangle,
  ExternalLink,
  PlayCircle,
} from "lucide-react"
import { useI18n as _useI18n, type Locale } from "@/lib/i18n/context"

function useLocale(): Locale {
  try { return _useI18n().locale } catch { return "en" }
}

type Section = {
  key: string
  title: Record<Locale, string>
  items: Array<{
    key: string
    label: Record<Locale, string>
    href: string | ((locale: Locale) => string)
    icon: typeof BookOpen
    external?: boolean
  }>
}

const SECTIONS: Section[] = [
  {
    key: "reference",
    title: { en: "Reference", "zh-TW": "參考", "zh-CN": "参考", ja: "リファレンス" },
    items: [
      {
        key: "operation-modes",
        label: { en: "Operation Modes", "zh-TW": "操作模式 (MODE)", "zh-CN": "操作模式 (MODE)", ja: "操作モード (MODE)" },
        href: (l) => `/docs/operator/${l}/reference/operation-modes`,
        icon: BookOpen,
      },
      {
        key: "decision-severity",
        label: { en: "Decision Severity", "zh-TW": "決策嚴重度", "zh-CN": "决策严重度", ja: "決定の重要度" },
        href: (l) => `/docs/operator/${l}/reference/decision-severity`,
        icon: BookOpen,
      },
      {
        key: "panels-overview",
        label: { en: "Panels Overview", "zh-TW": "各 panel 一覽", "zh-CN": "各 panel 一览", ja: "Panel 一覧" },
        href: (l) => `/docs/operator/${l}/reference/panels-overview`,
        icon: BookOpen,
      },
      {
        key: "budget-strategies",
        label: { en: "Budget Strategies", "zh-TW": "預算策略", "zh-CN": "预算策略", ja: "バジェット戦略" },
        href: (l) => `/docs/operator/${l}/reference/budget-strategies`,
        icon: BookOpen,
      },
      {
        key: "glossary",
        label: { en: "Glossary", "zh-TW": "名詞解釋", "zh-CN": "术语表", ja: "用語集" },
        href: (l) => `/docs/operator/${l}/reference/glossary`,
        icon: BookOpen,
      },
    ],
  },
  {
    key: "problems",
    title: { en: "Something broken?", "zh-TW": "遇到問題？", "zh-CN": "遇到问题？", ja: "問題発生時" },
    items: [
      {
        key: "troubleshooting",
        label: { en: "Troubleshooting", "zh-TW": "故障排除", "zh-CN": "故障排查", ja: "トラブルシューティング" },
        href: (l) => `/docs/operator/${l}/troubleshooting`,
        icon: AlertTriangle,
      },
    ],
  },
  {
    key: "tour",
    title: { en: "Getting started", "zh-TW": "上手", "zh-CN": "上手", ja: "はじめに" },
    items: [
      {
        key: "run-tour",
        label: { en: "Run the 5-step tour", "zh-TW": "重新跑 5 步導覽", "zh-CN": "重新跑 5 步导览", ja: "5 ステップツアーを再実行" },
        href: "/?tour=1",
        icon: PlayCircle,
      },
      {
        key: "search",
        label: { en: "Search docs", "zh-TW": "搜尋文件", "zh-CN": "搜索文档", ja: "ドキュメント検索" },
        href: (l) => `/docs/operator/${l}`,
        icon: Compass,
      },
    ],
  },
  {
    key: "external",
    title: { en: "For developers", "zh-TW": "給開發者", "zh-CN": "给开发者", ja: "開発者向け" },
    items: [
      {
        key: "swagger",
        label: { en: "API reference (Swagger)", "zh-TW": "API 參考 (Swagger)", "zh-CN": "API 参考 (Swagger)", ja: "API リファレンス (Swagger)" },
        href: "/docs",
        icon: ExternalLink,
        external: true,
      },
    ],
  },
]

const LABEL: Record<Locale, string> = {
  en: "Help", "zh-TW": "說明", "zh-CN": "说明", ja: "ヘルプ",
}

export function HelpMenu() {
  const locale = useLocale()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  const close = useCallback(() => setOpen(false), [])

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close()
    }
    document.addEventListener("mousedown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [open, close])

  return (
    <div ref={rootRef} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={LABEL[locale]}
        title={LABEL[locale]}
        className="p-1.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--neural-cyan,#67e8f9)] hover:bg-white/5 transition-colors"
      >
        <HelpCircle className="w-4 h-4" aria-hidden />
      </button>
      {open && (
        <div
          role="menu"
          aria-label={LABEL[locale]}
          className="absolute right-0 top-full mt-1 z-50 w-[min(300px,calc(100vw-2rem))] holo-glass-simple rounded-sm border border-[var(--neural-cyan,#67e8f9)]/40 shadow-lg p-2 font-mono text-[11px]"
        >
          {SECTIONS.map((section, i) => (
            <div key={section.key} className={i > 0 ? "mt-2 pt-2 border-t border-white/5" : ""}>
              <div className="px-2 pb-1 text-[9px] tracking-[0.2em] text-[var(--muted-foreground,#94a3b8)] uppercase">
                {section.title[locale]}
              </div>
              <ul>
                {section.items.map((item) => {
                  const Icon = item.icon
                  const href = typeof item.href === "function" ? item.href(locale) : item.href
                  return (
                    <li key={item.key}>
                      <a
                        href={href}
                        target={item.external ? "_blank" : undefined}
                        rel={item.external ? "noopener noreferrer" : undefined}
                        onClick={close}
                        role="menuitem"
                        className="flex items-center gap-2 px-2 py-1.5 rounded-sm hover:bg-white/5 hover:text-[var(--neural-cyan,#67e8f9)] transition-colors"
                      >
                        <Icon className="w-3.5 h-3.5 shrink-0" aria-hidden />
                        <span className="flex-1 truncate">{item.label[locale]}</span>
                        {item.external && (
                          <ExternalLink
                            className="w-3 h-3 text-[var(--muted-foreground,#94a3b8)]"
                            aria-hidden
                          />
                        )}
                      </a>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
