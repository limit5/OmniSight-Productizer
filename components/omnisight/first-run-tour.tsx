"use client"

/**
 * First-run tour (E2 / D5).
 *
 * Shown when:
 *   - ?tour=1 in URL (explicit trigger — sharable / re-runnable), OR
 *   - no `omnisight-tour-seen` entry in localStorage (automatic for
 *     true first launch; dismiss stores the flag).
 *
 * Intentionally self-contained — no tour framework dep. Five steps
 * anchored to real DOM nodes via `data-tour="…"`; if a node is
 * missing (responsive breakpoint, panel hidden) the step is skipped
 * silently so we never strand the user.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { X, ChevronLeft, ChevronRight, Sparkles } from "lucide-react"
import { useI18n as _useI18n, type Locale } from "@/lib/i18n/context"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { getUserStorage, onStorageChange } from "@/lib/storage"
import { getUserPreference, setUserPreference } from "@/lib/api"

function useLocale(): Locale {
  try { return _useI18n().locale } catch { return "en" }
}

type StepCopy = { title: string; body: string }

interface Step {
  anchor: string           // data-tour selector (without the attribute name)
  placement: "bottom" | "top" | "right" | "left"
  copy: Record<Locale, StepCopy>
}

const STEPS: Step[] = [
  {
    anchor: "mode",
    placement: "bottom",
    copy: {
      en: {
        title: "1 / 5 · Operation Mode",
        body: "Four pills decide how much the AI can do without asking. MANUAL pauses every decision; TURBO runs even destructive work after a 60 s countdown. Click the `?` for details.",
      },
      "zh-TW": {
        title: "1 / 5 · Operation Mode",
        body: "四個 pill 決定 AI 能自主到哪一步。MANUAL 每件事都等您；TURBO 連破壞性操作都會在 60 秒倒數後自動執行。點 `?` 看完整說明。",
      },
      "zh-CN": {
        title: "1 / 5 · Operation Mode",
        body: "四个 pill 决定 AI 能自主到哪一步。MANUAL 每件事都等您；TURBO 连破坏性操作都会在 60 秒倒计时后自动执行。点 `?` 看完整说明。",
      },
      ja: {
        title: "1 / 5 · Operation Mode",
        body: "4 つのピルが AI の自律度を決めます。MANUAL は全承認、TURBO は破壊的操作も 60 秒カウントダウン後に自動実行。`?` で詳細。",
      },
    },
  },
  {
    anchor: "decision-queue",
    placement: "left",
    copy: {
      en: {
        title: "2 / 5 · Decision Queue",
        body: "When the AI hits something it cannot auto-execute under the current mode, it lands here. Approve, reject, or let it time out to a safe default.",
      },
      "zh-TW": {
        title: "2 / 5 · Decision Queue",
        body: "AI 遇到當前 mode 不允許自動執行的決定時會落到這裡。您可批准、拒絕、或讓它 timeout 使用預設安全選項。",
      },
      "zh-CN": {
        title: "2 / 5 · Decision Queue",
        body: "AI 遇到当前 mode 不允许自动执行的决策时会落到这里。您可批准、拒绝、或让它 timeout 使用默认安全选项。",
      },
      ja: {
        title: "2 / 5 · Decision Queue",
        body: "現在の mode で自動実行できない事項はここに集まります。承認・拒否・タイムアウトで安全デフォルトに倒す — お好みで。",
      },
    },
  },
  {
    anchor: "budget",
    placement: "left",
    copy: {
      en: {
        title: "3 / 5 · Budget Strategy",
        body: "Pick how expensive each AI call is allowed to be. QUALITY for releases, COST_SAVER for exploration, SPRINT for deadline crunch.",
      },
      "zh-TW": {
        title: "3 / 5 · Budget Strategy",
        body: "決定 AI 呼叫允許多貴。QUALITY 給 release、COST_SAVER 給探索、SPRINT 給死線衝刺。",
      },
      "zh-CN": {
        title: "3 / 5 · Budget Strategy",
        body: "决定 AI 调用允许多贵。QUALITY 给 release、COST_SAVER 给探索、SPRINT 给死线冲刺。",
      },
      ja: {
        title: "3 / 5 · Budget Strategy",
        body: "AI 呼び出しのコスト枠を選びます。QUALITY はリリース、COST_SAVER は探索、SPRINT は納期追込み。",
      },
    },
  },
  {
    anchor: "orchestrator",
    placement: "right",
    copy: {
      en: {
        title: "4 / 5 · Orchestrator AI",
        body: "Your main interface to the system. Slash commands work here: `/invoke`, `/halt`, `/commit`, `/review-pr`. Free text is a prompt to the supervisor agent.",
      },
      "zh-TW": {
        title: "4 / 5 · Orchestrator AI",
        body: "您與系統的主要互動介面。Slash 指令在此可用：`/invoke`、`/halt`、`/commit`、`/review-pr`。自由文字會送到 supervisor agent。",
      },
      "zh-CN": {
        title: "4 / 5 · Orchestrator AI",
        body: "您与系统的主要交互界面。Slash 指令在此可用：`/invoke`、`/halt`、`/commit`、`/review-pr`。自由文字会发到 supervisor agent。",
      },
      ja: {
        title: "4 / 5 · Orchestrator AI",
        body: "システムとの主な対話窓口。スラッシュコマンド (`/invoke`、`/halt`、`/commit`、`/review-pr`) が使えます。自由入力は supervisor agent へのプロンプト。",
      },
    },
  },
  {
    anchor: "panel-help",
    placement: "bottom",
    copy: {
      en: {
        title: "5 / 5 · Help is everywhere",
        body: "Every panel has a `?` icon in its header. Click for a one-paragraph TL;DR + a link to the full doc in your language. That's the tour — happy shipping.",
      },
      "zh-TW": {
        title: "5 / 5 · 處處有說明",
        body: "每個 panel 的 header 都有 `?` 圖示。點開看一段 TL;DR 與對應您語言的完整文件連結。導覽結束，祝您順利出貨。",
      },
      "zh-CN": {
        title: "5 / 5 · 处处有说明",
        body: "每个 panel 的 header 都有 `?` 图标。点开看一段 TL;DR 与对应您语言的完整文档链接。导览结束，祝您顺利出货。",
      },
      ja: {
        title: "5 / 5 · 説明はいつでも",
        body: "各 panel のヘッダーに `?` アイコンがあります。一段落の TL;DR と言語別の完全ドキュメントリンクへ飛べます。ツアー完了 — 良い出荷を。",
      },
    },
  },
]

const STORAGE_KEY = "omnisight-tour-seen"

const CTA: Record<Locale, { next: string; back: string; skip: string; done: string }> = {
  en:      { next: "Next",  back: "Back",  skip: "Skip tour", done: "Done" },
  "zh-TW": { next: "下一步", back: "上一步", skip: "跳過導覽",  done: "完成" },
  "zh-CN": { next: "下一步", back: "上一步", skip: "跳过导览",  done: "完成" },
  ja:      { next: "次へ",  back: "戻る",  skip: "ツアーをスキップ", done: "完了" },
}

export function FirstRunTour() {
  const locale = useLocale()
  const [active, setActive] = useState(false)
  const [idx, setIdx] = useState(0)
  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const { user } = useAuth()
  const { currentTenantId } = useTenant()
  const userId = user?.id ?? null

  useEffect(() => {
    if (typeof window === "undefined" || !userId) return
    const store = getUserStorage(currentTenantId, userId)
    const params = new URLSearchParams(window.location.search)
    const tourParam = params.get("tour")
    const seen = store.getItem(STORAGE_KEY) === "1"
    if (tourParam) {
      const asNum = parseInt(tourParam, 10)
      if (Number.isFinite(asNum) && asNum >= 1 && asNum <= STEPS.length) {
        setIdx(asNum - 1)
        setActive(true)
        return
      }
      const anchorIdx = STEPS.findIndex((s) => s.anchor === tourParam)
      if (anchorIdx >= 0) {
        setIdx(anchorIdx)
        setActive(true)
        return
      }
      setActive(true)
      return
    }
    if (seen) return
    let cancelled = false
    getUserPreference("tour_seen").then((pref) => {
      if (cancelled) return
      if (pref?.value === "1") {
        store.setItem(STORAGE_KEY, "1")
      } else {
        setActive(true)
      }
    }).catch(() => {
      if (!cancelled) setActive(true)
    })
    return () => { cancelled = true }
  }, [userId, currentTenantId])

  const closeTour = useCallback((remember = true) => {
    setActive(false)
    if (remember && userId) {
      const store = getUserStorage(currentTenantId, userId)
      store.setItem(STORAGE_KEY, "1")
      setUserPreference("tour_seen", "1").catch(() => {})
    }
    if (typeof window !== "undefined") {
      const u = new URL(window.location.href)
      if (u.searchParams.has("tour")) {
        u.searchParams.delete("tour")
        window.history.replaceState(null, "", u.toString())
      }
    }
  }, [userId, currentTenantId])

  // Find the anchor for the current step; skip missing anchors.
  useLayoutEffect(() => {
    if (!active) return
    const tryMeasure = () => {
      const step = STEPS[idx]
      if (!step) return
      const el = document.querySelector(`[data-tour="${step.anchor}"]`)
      if (!el) {
        setAnchorRect(null)
        return
      }
      const rect = (el as HTMLElement).getBoundingClientRect()
      setAnchorRect(rect)
      el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" })
    }
    // Measure now + again after scrollIntoView settles.
    tryMeasure()
    const t = setTimeout(tryMeasure, 350)
    return () => clearTimeout(t)
  }, [active, idx])

  // Re-measure on resize.
  useEffect(() => {
    if (!active) return
    const onResize = () => {
      const step = STEPS[idx]
      const el = document.querySelector(`[data-tour="${step.anchor}"]`)
      setAnchorRect(el ? (el as HTMLElement).getBoundingClientRect() : null)
    }
    window.addEventListener("resize", onResize)
    window.addEventListener("scroll", onResize, { capture: true })
    return () => {
      window.removeEventListener("resize", onResize)
      window.removeEventListener("scroll", onResize, { capture: true })
    }
  }, [active, idx])

  // Keyboard: Esc = skip, ← = back, → = next.
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); closeTour(true) }
      else if (e.key === "ArrowRight") { e.preventDefault(); advance(1) }
      else if (e.key === "ArrowLeft")  { e.preventDefault(); advance(-1) }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
     // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, idx])

  const advance = useCallback((delta: number) => {
    setIdx((i) => {
      const next = i + delta
      if (next < 0) return 0
      if (next >= STEPS.length) { closeTour(true); return i }
      return next
    })
  }, [closeTour])

  const step = STEPS[idx]
  const copy = step?.copy[locale]

  // Card placement: fixed-positioned below / above / beside the anchor.
  // Falls back to screen centre if anchor not found.
  const cardStyle = useMemo<React.CSSProperties>(() => {
    if (!anchorRect) {
      return { top: "50%", left: "50%", transform: "translate(-50%, -50%)" }
    }
    const gap = 12
    const cardW = 360
    const cardH = 160
    const vw = typeof window !== "undefined" ? window.innerWidth : 1920
    const vh = typeof window !== "undefined" ? window.innerHeight : 1080
    let top = 0, left = 0
    switch (step.placement) {
      case "bottom":
        top = Math.min(anchorRect.bottom + gap, vh - cardH - 12)
        left = Math.max(12, Math.min(anchorRect.left + anchorRect.width / 2 - cardW / 2, vw - cardW - 12))
        break
      case "top":
        top = Math.max(12, anchorRect.top - cardH - gap)
        left = Math.max(12, Math.min(anchorRect.left + anchorRect.width / 2 - cardW / 2, vw - cardW - 12))
        break
      case "right":
        top = Math.max(12, Math.min(anchorRect.top, vh - cardH - 12))
        left = Math.min(anchorRect.right + gap, vw - cardW - 12)
        break
      case "left":
        top = Math.max(12, Math.min(anchorRect.top, vh - cardH - 12))
        left = Math.max(12, anchorRect.left - cardW - gap)
        break
    }
    return { top, left, width: cardW }
  }, [anchorRect, step])

  if (!active || !step || !copy) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={copy.title}
      className="fixed inset-0 z-[100] pointer-events-auto"
    >
      {/* Dim backdrop with a hole punched over the anchor. The hole is a
       * clip-path rectangle so the user still sees the element they are
       * being told about, at full opacity. */}
      <Backdrop rect={anchorRect} onClick={() => closeTour(true)} />
      {/* Cyan outline around the anchor. */}
      {anchorRect && (
        <div
          aria-hidden
          className="fixed pointer-events-none border-2 border-[var(--neural-cyan,#67e8f9)] rounded-sm"
          style={{
            top: anchorRect.top - 4,
            left: anchorRect.left - 4,
            width: anchorRect.width + 8,
            height: anchorRect.height + 8,
            boxShadow: "0 0 24px rgba(103, 232, 249, 0.55), 0 0 4px rgba(103, 232, 249, 0.8) inset",
            animation: "toast-urgent-pulse 1.8s ease-in-out infinite",
          }}
        />
      )}
      {/* Tour card. */}
      <div
        ref={cardRef}
        className="fixed holo-glass-simple border border-[var(--neural-cyan,#67e8f9)]/60 rounded-sm p-4 shadow-2xl"
        style={cardStyle}
      >
        <div className="flex items-start gap-2 mb-2">
          <Sparkles className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)] shrink-0 mt-0.5" aria-hidden />
          <h2 className="flex-1 font-mono text-[12px] tracking-[0.2em] text-[var(--neural-cyan,#67e8f9)]">
            {copy.title}
          </h2>
          <button
            type="button"
            onClick={() => closeTour(true)}
            aria-label={CTA[locale].skip}
            className="text-[var(--muted-foreground,#94a3b8)] hover:text-white"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <p className="font-sans text-[13px] leading-relaxed text-[var(--foreground,#e2e8f0)] mb-3">
          {copy.body}
        </p>
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => closeTour(true)}
            className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground,#94a3b8)] hover:text-white"
          >
            {CTA[locale].skip}
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => advance(-1)}
              disabled={idx === 0}
              className="flex items-center gap-1 font-mono text-[11px] px-2 py-1 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] text-[var(--muted-foreground,#94a3b8)] hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ChevronLeft className="w-3 h-3" aria-hidden /> {CTA[locale].back}
            </button>
            <button
              type="button"
              onClick={() => advance(1)}
              className="flex items-center gap-1 font-mono text-[11px] px-2.5 py-1 rounded-sm bg-[var(--neural-cyan,#67e8f9)] text-black font-semibold hover:brightness-110"
            >
              {idx === STEPS.length - 1 ? CTA[locale].done : CTA[locale].next}
              <ChevronRight className="w-3 h-3" aria-hidden />
            </button>
          </div>
        </div>
        {/* Progress dots */}
        <div className="flex items-center justify-center gap-1.5 mt-3">
          {STEPS.map((_, i) => (
            <span
              key={i}
              aria-hidden
              className="w-1.5 h-1.5 rounded-full"
              style={{
                background: i === idx
                  ? "var(--neural-cyan,#67e8f9)"
                  : "rgba(148,163,184,0.35)",
                boxShadow: i === idx ? "0 0 8px var(--neural-cyan,#67e8f9)" : undefined,
              }}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

/** Backdrop that punches a hole over the anchor using a single-path SVG
 * mask. Uses `evenodd` so the anchor rectangle carves out transparency. */
function Backdrop({ rect, onClick }: { rect: DOMRect | null; onClick: () => void }) {
  if (!rect) {
    return (
      <div
        className="fixed inset-0 bg-[var(--deep-space-start,#010409)]/80 backdrop-blur-[2px]"
        onClick={onClick}
      />
    )
  }
  const vw = typeof window !== "undefined" ? window.innerWidth : 1920
  const vh = typeof window !== "undefined" ? window.innerHeight : 1080
  // Inflate hole slightly so the cyan outline sits on the dark edge.
  const pad = 6
  const x = Math.max(0, rect.left - pad)
  const y = Math.max(0, rect.top - pad)
  const w = Math.min(vw - x, rect.width + pad * 2)
  const h = Math.min(vh - y, rect.height + pad * 2)
  const path = `M0 0 H${vw} V${vh} H0 Z M${x} ${y} H${x + w} V${y + h} H${x} Z`
  return (
    <svg
      className="fixed inset-0 pointer-events-auto"
      width={vw}
      height={vh}
      onClick={onClick}
    >
      <path d={path} fill="rgba(1,4,9,0.78)" fillRule="evenodd" />
    </svg>
  )
}
