"use client"

/**
 * Global command palette — ⌘K on macOS, Ctrl+K elsewhere. (F2)
 *
 * Two sources of commands:
 *   1. Actions — jump to a panel, switch mode, run the tour, open
 *      Swagger. Executed in-app via callbacks (URL change or
 *      event).
 *   2. Docs — every operator .md the current locale has (title +
 *      headings, indexed once on mount via `/api/docs-index` route
 *      exposed alongside the docs landing page).
 *
 * Matcher is a self-contained scorer (WP.11, OP-1505):
 *   - smart-case: all-lowercase query → case-insensitive,
 *     any uppercase → case-sensitive
 *   - wildcard: `*` becomes glob (`*.tsx`, `agent*config`)
 *   - matched-indices: returned per-label for highlight rendering
 *
 * Multi-term queries (whitespace-separated) are AND-combined; each
 * term is matched against label / hint / tags independently and
 * scores are summed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Search,
  ArrowRight,
  BookOpen,
  Compass,
  AlertTriangle,
  PlayCircle,
  LayoutDashboard,
  Zap,
  Gauge,
  ScrollText,
  ExternalLink,
  Command,
  Wand2,
  Users,
} from "lucide-react"
import { useI18n as _useI18n, type Locale } from "@/lib/i18n/context"
import { listEffectiveSkills, type EffectiveSkill } from "@/lib/api"

function useLocale(): Locale {
  try { return _useI18n().locale } catch { return "en" }
}

type CommandItem = {
  id: string
  icon: typeof BookOpen
  label: Record<Locale, string>
  hint?: Record<Locale, string>
  run: () => void
  /** Optional extra keywords for fuzzy matching (tags). */
  tags?: string[]
}

type TermMatcher =
  | { kind: "glob"; re: RegExp }
  | { kind: "literal"; needle: string }

type CompiledQuery = {
  caseSensitive: boolean
  terms: TermMatcher[]
}

type TermHit = { score: number; indices: number[] }

const GLOB_ESCAPE_RE = /[.+?^${}()|[\]\\]/g

/**
 * Compile a query string into a smart-case, wildcard-aware matcher.
 * Returns null for empty queries (caller should skip filtering).
 *
 * Exported for unit testing — see test/components/command-palette-matcher.test.ts.
 */
export function compileQuery(query: string): CompiledQuery | null {
  const trimmed = query.trim()
  if (!trimmed) return null
  const caseSensitive = /[A-Z]/.test(trimmed)
  const terms = trimmed.split(/\s+/).filter(Boolean).map((raw) => compileTerm(raw, caseSensitive))
  if (terms.length === 0) return null
  return { caseSensitive, terms }
}

function compileTerm(raw: string, caseSensitive: boolean): TermMatcher {
  if (raw.includes("*")) {
    const escaped = raw.replace(GLOB_ESCAPE_RE, "\\$&").replace(/\*/g, ".*")
    const flags = caseSensitive ? "" : "i"
    return { kind: "glob", re: new RegExp(escaped, flags) }
  }
  return { kind: "literal", needle: caseSensitive ? raw : raw.toLowerCase() }
}

function matchTerm(term: TermMatcher, haystack: string, caseSensitive: boolean): TermHit | null {
  if (term.kind === "glob") {
    const m = term.re.exec(haystack)
    if (!m) return null
    const start = m.index
    const len = m[0].length
    if (len === 0) return null
    return {
      score: len - start / 50,
      indices: Array.from({ length: len }, (_, i) => start + i),
    }
  }
  const h = caseSensitive ? haystack : haystack.toLowerCase()
  const i = h.indexOf(term.needle)
  if (i < 0) return null
  return {
    score: term.needle.length - i / 50,
    indices: Array.from({ length: term.needle.length }, (_, k) => i + k),
  }
}

/**
 * Match a query against a `label` (highlightable) and an `aux` blob
 * (hint + tags concatenated; not highlighted). Every term must match
 * either the label or the aux blob; only label hits contribute to
 * the returned `indices`.
 *
 * Exported for unit testing.
 */
export function matchItem(
  compiled: CompiledQuery,
  label: string,
  aux: string,
): { score: number; indices: number[] } | null {
  let total = 0
  const labelHits: number[] = []
  for (const term of compiled.terms) {
    const onLabel = matchTerm(term, label, compiled.caseSensitive)
    if (onLabel) {
      total += onLabel.score
      labelHits.push(...onLabel.indices)
      continue
    }
    const onAux = matchTerm(term, aux, compiled.caseSensitive)
    if (!onAux) return null
    total += onAux.score
  }
  // Dedupe indices (multiple terms can overlap on the same label chars).
  const indices = labelHits.length === 0
    ? []
    : Array.from(new Set(labelHits)).sort((a, b) => a - b)
  return { score: total, indices }
}

/**
 * Split a label string into plain / highlighted segments so the row
 * renderer can wrap matched chars in <mark>. Indices outside the
 * label range are clamped silently.
 *
 * Exported for unit testing.
 */
export function highlightSegments(text: string, indices: number[]): { text: string; hit: boolean }[] {
  if (indices.length === 0) return [{ text, hit: false }]
  const sorted = [...indices].filter((i) => i >= 0 && i < text.length).sort((a, b) => a - b)
  if (sorted.length === 0) return [{ text, hit: false }]
  const segments: { text: string; hit: boolean }[] = []
  let cursor = 0
  let i = 0
  while (i < sorted.length) {
    const start = sorted[i]
    let end = start
    while (i + 1 < sorted.length && sorted[i + 1] === end + 1) {
      end = sorted[++i]
    }
    if (cursor < start) segments.push({ text: text.slice(cursor, start), hit: false })
    segments.push({ text: text.slice(start, end + 1), hit: true })
    cursor = end + 1
    i++
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), hit: false })
  return segments
}

interface Props {
  /** Called when user picks a panel in the palette. */
  onNavigatePanel?: (panelId: string) => void
}

const COPY: Record<Locale, { placeholder: string; empty: string; hint: string; title: string }> = {
  en:      { placeholder: "Type a command or search docs…", empty: "No matches.", hint: "↑↓ navigate · ↵ open · esc close", title: "Command Palette" },
  "zh-TW": { placeholder: "輸入指令或搜尋文件……",           empty: "無符合。",   hint: "↑↓ 移動 · ↵ 開啟 · esc 關閉", title: "指令面板" },
  "zh-CN": { placeholder: "输入命令或搜索文档……",           empty: "无匹配。",   hint: "↑↓ 移动 · ↵ 打开 · esc 关闭", title: "命令面板" },
  ja:      { placeholder: "コマンド入力 / ドキュメント検索…",  empty: "該当なし。", hint: "↑↓ 移動 · ↵ 開く · esc 閉じる", title: "コマンドパレット" },
}

export function CommandPalette({ onNavigatePanel }: Props) {
  const locale = useLocale()
  const L = COPY[locale]
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState("")
  const [cursor, setCursor] = useState(0)
  const [skills, setSkills] = useState<EffectiveSkill[]>([])
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)

  const navigatePanel = useCallback((id: string) => {
    if (onNavigatePanel) onNavigatePanel(id)
    else if (typeof window !== "undefined") {
      const u = new URL(window.location.href)
      u.searchParams.set("panel", id)
      window.history.replaceState(null, "", u.toString())
      // Fire a popstate so the home page's URL-watch useEffect picks it up.
      window.dispatchEvent(new PopStateEvent("popstate"))
    }
  }, [onNavigatePanel])

  const go = useCallback((href: string, external = false) => {
    if (typeof window === "undefined") return
    if (external) window.open(href, "_blank", "noopener,noreferrer")
    else window.location.href = href
  }, [])

  const invokeSkillMention = useCallback((name: string) => {
    if (typeof window === "undefined") return
    navigatePanel("orchestrator")
    setTimeout(() => {
      window.dispatchEvent(
        new CustomEvent("omnisight:chat-insert-text", {
          detail: { text: `@${name} ` },
        }),
      )
    }, 0)
  }, [navigatePanel])

  useEffect(() => {
    let cancelled = false
    listEffectiveSkills()
      .then((res) => {
        if (!cancelled) setSkills(res.items)
      })
      .catch(() => {
        if (!cancelled) setSkills([])
      })
    return () => { cancelled = true }
  }, [])

  const skillCommands = useMemo<CommandItem[]>(
    () => skills.map((skill) => ({
      id: `skill-${skill.name}`,
      icon: Wand2,
      label: {
        en: `Invoke skill: @${skill.name}`,
        "zh-TW": `叫用 skill：@${skill.name}`,
        "zh-CN": `调用 skill：@${skill.name}`,
        ja: `skill を呼び出す: @${skill.name}`,
      },
      hint: {
        en: skill.description,
        "zh-TW": skill.description,
        "zh-CN": skill.description,
        ja: skill.description,
      },
      run: () => invokeSkillMention(skill.name),
      tags: ["skill", "skills", skill.name, skill.description, skill.scope, ...skill.keywords],
    })),
    [skills, invokeSkillMention],
  )

  const commands = useMemo<CommandItem[]>(() => [
    // Panels — navigate via ?panel=…
    { id: "panel-orchestrator", icon: LayoutDashboard, label: { en: "Go to Orchestrator", "zh-TW": "前往 Orchestrator", "zh-CN": "前往 Orchestrator", ja: "Orchestrator へ" }, run: () => navigatePanel("orchestrator"), tags: ["chat", "command"] },
    { id: "panel-decisions",    icon: Zap,             label: { en: "Go to Decision Queue", "zh-TW": "前往決策佇列", "zh-CN": "前往决策队列", ja: "Decision Queue へ" }, run: () => navigatePanel("decisions"), tags: ["pending", "approve", "reject"] },
    { id: "panel-budget",       icon: Gauge,           label: { en: "Go to Budget Strategy", "zh-TW": "前往預算策略", "zh-CN": "前往预算策略", ja: "Budget Strategy へ" }, run: () => navigatePanel("budget"), tags: ["cost", "tier", "retries"] },
    { id: "panel-timeline",     icon: LayoutDashboard, label: { en: "Go to Pipeline Timeline", "zh-TW": "前往 Pipeline Timeline", "zh-CN": "前往 Pipeline Timeline", ja: "Pipeline Timeline へ" }, run: () => navigatePanel("timeline") },
    { id: "panel-rules",        icon: ScrollText,      label: { en: "Go to Decision Rules", "zh-TW": "前往決策規則", "zh-CN": "前往决策规则", ja: "Decision Rules へ" }, run: () => navigatePanel("rules"), tags: ["auto", "override"] },
    { id: "panel-agents",       icon: Users,           label: { en: "Go to Agent Roster", "zh-TW": "前往 Agent 名冊", "zh-CN": "前往 Agent 名册", ja: "Agent 名簿へ" }, run: () => go("/agents"), tags: ["guild", "character card", "party"] },
    // Tour
    { id: "tour-run",    icon: PlayCircle, label: { en: "Run the 5-step tour", "zh-TW": "重新跑 5 步導覽", "zh-CN": "重新跑 5 步导览", ja: "5 ステップツアーを再実行" }, run: () => go("/?tour=1") },
    { id: "tour-decisions", icon: PlayCircle, label: { en: "Tour: Decision Queue step", "zh-TW": "導覽：Decision Queue 步驟", "zh-CN": "导览：Decision Queue 步骤", ja: "ツアー: Decision Queue" }, run: () => go("/?tour=decision-queue") },
    // Docs
    { id: "doc-modes",       icon: BookOpen, label: { en: "Doc: Operation Modes", "zh-TW": "文件：Operation Modes", "zh-CN": "文档：Operation Modes", ja: "ドキュメント: Operation Modes" }, run: () => go(`/docs/operator/${locale}/reference/operation-modes`), tags: ["mode", "manual", "supervised", "full auto", "turbo"] },
    { id: "doc-severity",    icon: BookOpen, label: { en: "Doc: Decision Severity", "zh-TW": "文件：Decision Severity", "zh-CN": "文档：Decision Severity", ja: "ドキュメント: Decision Severity" }, run: () => go(`/docs/operator/${locale}/reference/decision-severity`), tags: ["info", "routine", "risky", "destructive"] },
    { id: "doc-panels",      icon: BookOpen, label: { en: "Doc: Panels Overview", "zh-TW": "文件：Panels Overview", "zh-CN": "文档：Panels Overview", ja: "ドキュメント: Panels Overview" }, run: () => go(`/docs/operator/${locale}/reference/panels-overview`) },
    { id: "doc-budget",      icon: BookOpen, label: { en: "Doc: Budget Strategies", "zh-TW": "文件：Budget Strategies", "zh-CN": "文档：Budget Strategies", ja: "ドキュメント: Budget Strategies" }, run: () => go(`/docs/operator/${locale}/reference/budget-strategies`), tags: ["tier", "quality", "balanced", "cost_saver", "sprint"] },
    { id: "doc-glossary",    icon: BookOpen, label: { en: "Doc: Glossary", "zh-TW": "文件：Glossary", "zh-CN": "文档：Glossary", ja: "ドキュメント: Glossary" }, run: () => go(`/docs/operator/${locale}/reference/glossary`) },
    { id: "doc-troubleshooting", icon: AlertTriangle, label: { en: "Doc: Troubleshooting", "zh-TW": "文件：故障排除", "zh-CN": "文档：故障排查", ja: "ドキュメント: トラブルシューティング" }, run: () => go(`/docs/operator/${locale}/troubleshooting`) },
    { id: "doc-tutorial-1",  icon: PlayCircle, label: { en: "Tutorial: First Invoke", "zh-TW": "教學：第一次 Invoke", "zh-CN": "教程：第一次 Invoke", ja: "チュートリアル: はじめての Invoke" }, run: () => go(`/docs/operator/${locale}/tutorial/first-invoke`) },
    { id: "doc-tutorial-2",  icon: PlayCircle, label: { en: "Tutorial: Handling a decision", "zh-TW": "教學：處理一個決策", "zh-CN": "教程：处理一个决策", ja: "チュートリアル: 決定の扱い方" }, run: () => go(`/docs/operator/${locale}/tutorial/handling-a-decision`) },
    { id: "doc-search",      icon: Compass,  label: { en: "Search all docs", "zh-TW": "搜尋所有文件", "zh-CN": "搜索所有文档", ja: "全ドキュメント検索" }, run: () => go(`/docs/operator/${locale}`) },
    { id: "swagger",         icon: ExternalLink, label: { en: "API reference (Swagger)", "zh-TW": "API 參考 (Swagger)", "zh-CN": "API 参考 (Swagger)", ja: "API リファレンス (Swagger)" }, run: () => go("/docs", true) },
    ...skillCommands,
  ], [locale, navigatePanel, go, skillCommands])

  const filtered = useMemo<{ c: CommandItem; indices: number[] }[]>(() => {
    const compiled = compileQuery(q)
    if (!compiled) return commands.map((c) => ({ c, indices: [] }))
    return commands
      .map((c) => {
        const label = c.label[locale]
        const aux = [c.hint?.[locale] ?? "", ...(c.tags ?? [])].join(" ")
        const hit = matchItem(compiled, label, aux)
        return hit ? { c, indices: hit.indices, score: hit.score } : null
      })
      .filter((x): x is { c: CommandItem; indices: number[]; score: number } => x !== null)
      .sort((a, b) => b.score - a.score)
      .map(({ c, indices }) => ({ c, indices }))
  }, [commands, q, locale])

  // Hotkey: Cmd/Ctrl+K toggles; Cmd/Ctrl+/ also (common in editors).
  useEffect(() => {
    if (typeof window === "undefined") return
    const onKey = (e: KeyboardEvent) => {
      const isHotkey = (e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K" || e.key === "/")
      if (!isHotkey) return
      // Don't hijack when the user is already typing in a form element —
      // only if the hotkey is fired outside an input, OR if they hit it
      // while our palette is already open.
      const active = document.activeElement as HTMLElement | null
      const inForm = active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)
      if (inForm && !open) return
      e.preventDefault()
      setOpen((v) => !v)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open])

  // Reset cursor when query changes so arrows feel right.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing derived state from query change
  useEffect(() => { setCursor(0) }, [q])

  // Autofocus input when opening.
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset query/cursor on palette open
      setQ("")
      setCursor(0)
      const t = setTimeout(() => inputRef.current?.focus(), 0)
      return () => clearTimeout(t)
    }
  }, [open])

  const onInputKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") { e.preventDefault(); setOpen(false); return }
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, Math.max(0, filtered.length - 1))); return }
    if (e.key === "ArrowUp")   { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); return }
    if (e.key === "Enter") {
      e.preventDefault()
      const entry = filtered[cursor]
      if (entry) { setOpen(false); entry.c.run() }
      return
    }
  }

  if (!open) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={L.title}
      className="fixed inset-0 z-[80] flex items-start justify-center pt-[10vh]"
    >
      <div
        className="absolute inset-0 bg-[var(--deep-space-start,#010409)]/70 backdrop-blur-[2px]"
        onClick={() => setOpen(false)}
      />
      <div className="relative w-[min(600px,calc(100vw-2rem))] holo-glass-simple rounded-sm border border-[var(--neural-cyan,#67e8f9)]/40 shadow-2xl overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-white/5">
          <Command className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <Search className="w-4 h-4 text-[var(--muted-foreground,#94a3b8)]" aria-hidden />
          <input
            ref={inputRef}
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onInputKey}
            placeholder={L.placeholder}
            aria-label={L.placeholder}
            className="flex-1 bg-transparent outline-none font-mono text-sm text-[var(--foreground,#e2e8f0)] placeholder-[var(--muted-foreground,#94a3b8)]"
          />
          <span className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] hidden sm:inline">
            {L.hint}
          </span>
        </div>
        <ul
          ref={listRef}
          role="listbox"
          aria-label={L.title}
          className="max-h-[60vh] overflow-y-auto"
        >
          {filtered.length === 0 && (
            <li className="px-3 py-6 text-center font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)]">
              {L.empty}
            </li>
          )}
          {filtered.map(({ c: item, indices }, i) => {
            const active = i === cursor
            const Icon = item.icon
            const label = item.label[locale]
            const segments = highlightSegments(label, indices)
            return (
              <li
                key={item.id}
                role="option"
                aria-selected={active}
                onMouseEnter={() => setCursor(i)}
                onMouseDown={(e) => { e.preventDefault(); setOpen(false); item.run() }}
                className={`flex items-center gap-2 px-3 py-2 cursor-pointer font-mono text-[12px] ${
                  active
                    ? "bg-[var(--neural-cyan,#67e8f9)]/10 text-[var(--foreground,#e2e8f0)]"
                    : "text-[var(--muted-foreground,#cbd5e1)]"
                }`}
              >
                <Icon
                  className={`w-3.5 h-3.5 shrink-0 ${active ? "text-[var(--neural-cyan,#67e8f9)]" : ""}`}
                  aria-hidden
                />
                <span className="flex-1 truncate">
                  {segments.map((seg, k) => seg.hit ? (
                    <mark
                      key={k}
                      data-testid="cp-hit"
                      className="bg-transparent text-[var(--neural-cyan,#67e8f9)] font-semibold"
                    >
                      {seg.text}
                    </mark>
                  ) : (
                    <span key={k}>{seg.text}</span>
                  ))}
                </span>
                {active && (
                  <ArrowRight
                    className="w-3.5 h-3.5 text-[var(--neural-cyan,#67e8f9)]"
                    aria-hidden
                  />
                )}
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
