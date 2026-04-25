/**
 * V9 #3 (TODO row 2711, #325) — Workspace onboarding flow.
 *
 * Six-step guided tour shown on first entry into one of the three
 * `/workspace/<type>` surfaces.  Walks the operator through the
 * conversational iteration loop:
 *
 *     1. 選擇框架     (Pick a framework)
 *     2. 描述你要什麼  (Describe what you want)
 *     3. AI 開始工作   (AI starts working)
 *     4. preview 出現  (Preview appears)
 *     5. 標註修改      (Annotate modifications)
 *     6. 部署          (Deploy)
 *
 * Trigger rules — same shape as `first-run-tour.tsx` (E2 / D5) so
 * operators get a consistent re-run affordance:
 *   - automatic on first visit (no `omnisight:workspace:<type>:onboarding-seen`
 *     key in scoped storage), OR
 *   - explicit via `?tour=1` URL param (sharable / re-runnable).
 *
 * Per-workspace flavouring:
 *   - The "framework" step lists the actual framework vocabulary the
 *     selected workspace exposes (web → shadcn / Next.js, mobile →
 *     iOS / Android / Flutter / RN, software → Python / TS / Go / Rust /
 *     Java / C++).  Keeps the copy concrete instead of hand-waving.
 *
 * Storage scope:
 *   - One key per workspace type so seeing the Web tour doesn't
 *     suppress the Mobile / Software tour.  Stored via the user-scoped
 *     `getUserStorage()` helper so the flag is per-(tenant, user) and
 *     follows the operator across browser tabs.  Falls back to plain
 *     `localStorage` if auth/tenant context is unavailable so the
 *     component remains drop-in usable on bare routes.
 *
 * Why a Dialog (not a hole-punch overlay like `first-run-tour.tsx`):
 *   The workspace shell is a three-pane grid; punching a hole over a
 *   single anchor would either obscure two columns or fight with the
 *   grid template.  A centred modal walks the operator through the
 *   conceptual flow first, then dismisses cleanly so they can engage
 *   the actual surfaces.  The command-center tour can keep its
 *   anchored callouts because it points at *individual* widgets.
 */
"use client"

import * as React from "react"
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  MousePointerSquareDashed,
  Rocket,
  Sparkles,
  Wand2,
  Eye,
  Layers,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useI18n as _useI18n, type Locale } from "@/lib/i18n/context"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { getUserStorage } from "@/lib/storage"
import type { WorkspaceType } from "@/app/workspace/[type]/types"

// ─── Public shapes (exported for unit tests + storybook) ──────────────────

export type OnboardingStepId =
  | "framework"
  | "describe"
  | "ai-work"
  | "preview"
  | "annotate"
  | "deploy"

export interface OnboardingStepCopy {
  /** Localised step heading (e.g. "1 / 6 · Pick a framework"). */
  title: string
  /** Body copy — typically two short sentences. */
  body: string
}

export interface OnboardingStep {
  id: OnboardingStepId
  /** Lucide icon for the step's visual marker. */
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>
  /** Per-locale title + body copy. */
  copy: Record<Locale, OnboardingStepCopy>
}

export interface OnboardingActionLabels {
  next: string
  back: string
  skip: string
  done: string
}

export interface WorkspaceOnboardingTourProps {
  /** Workspace this tour is mounted in — drives copy + storage key. */
  type: WorkspaceType
  /**
   * Test seam: skip auto-trigger logic + storage probe and force the
   * tour open immediately.  When set, dismissal still calls onClose
   * but does NOT persist a "seen" flag (tests assert on dismissal
   * behaviour without leaving state behind).
   */
  forceOpen?: boolean
  /**
   * Test seam: opt out of the localStorage probe entirely.  When false,
   * the tour will not auto-show even on first visit.  Defaults to true.
   */
  autoShow?: boolean
  /**
   * Test seam: invoked when the tour closes (Skip, Done, Esc, X).
   * `reason` distinguishes "completed" (last step Done / explicit
   * Finish) from "skipped" (any earlier dismissal).  Both code paths
   * still persist the seen flag — operators only see the tour again
   * if they explicitly re-trigger via `?tour=1`.
   */
  onClose?: (reason: "completed" | "skipped") => void
  /**
   * Test seam: replace the storage backend.  Defaults to the user-
   * scoped `getUserStorage()` helper, falling back to plain
   * `localStorage` when no auth/tenant context is mounted.
   */
  storageImpl?: {
    getItem: (key: string) => string | null
    setItem: (key: string, value: string) => void
    removeItem?: (key: string) => void
  }
  /**
   * Test seam: replace the URL search-param accessor.  Defaults to
   * `new URLSearchParams(window.location.search)`.  Tests pass a
   * stub so `?tour=1` flows can be exercised without manipulating
   * `window.location`.
   */
  searchParamsImpl?: { get: (key: string) => string | null }
  /** Force a specific locale instead of the I18n context (test seam). */
  localeOverride?: Locale
  className?: string
}

// ─── Constants ─────────────────────────────────────────────────────────────

/** Storage key for the per-(tenant, user, workspace-type) "seen" flag. */
export function onboardingStorageKey(type: WorkspaceType): string {
  return `omnisight:workspace:${type}:onboarding-seen`
}

/** Six-step canonical sequence — order is contractually frozen. */
export const ONBOARDING_STEPS: readonly OnboardingStep[] = Object.freeze([
  {
    id: "framework",
    icon: Layers,
    copy: {
      en: {
        title: "1 / 6 · Pick a framework",
        body: "Open the left sidebar and pick the framework you want to build with. The agent inherits this choice on every prompt — no need to repeat it.",
      },
      "zh-TW": {
        title: "1 / 6 · 選擇框架",
        body: "打開左側 sidebar 並選一個你要用的框架。Agent 會在每次 prompt 自動帶入此選擇，不用重複輸入。",
      },
      "zh-CN": {
        title: "1 / 6 · 选择框架",
        body: "打开左侧 sidebar 并选一个你要用的框架。Agent 会在每次 prompt 自动带入此选择，不用重复输入。",
      },
      ja: {
        title: "1 / 6 · フレームワークを選ぶ",
        body: "左サイドバーで使いたいフレームワークを選びます。エージェントは以降のプロンプトでこの選択を自動的に引き継ぎます。",
      },
    },
  },
  {
    id: "describe",
    icon: Wand2,
    copy: {
      en: {
        title: "2 / 6 · Describe what you want",
        body: "Type the result in plain language in the chat panel on the right. Be specific about pages, behaviours, and any reference content you want included.",
      },
      "zh-TW": {
        title: "2 / 6 · 描述你要什麼",
        body: "在右側 chat panel 用自然語言描述你要的成果。明確說出頁面、行為、與要參考的內容。",
      },
      "zh-CN": {
        title: "2 / 6 · 描述你要什么",
        body: "在右侧 chat panel 用自然语言描述你要的成果。明确说出页面、行为、以及要参考的内容。",
      },
      ja: {
        title: "2 / 6 · やりたいことを書く",
        body: "右の chat パネルに自然言語で具体的な要件を書きます。画面、振る舞い、参照したい資料まで明示するのがコツ。",
      },
    },
  },
  {
    id: "ai-work",
    icon: Sparkles,
    copy: {
      en: {
        title: "3 / 6 · AI starts working",
        body: "Submit the prompt — the agent reads, plans, then writes code into the workspace. Watch its progress live in the right pane.",
      },
      "zh-TW": {
        title: "3 / 6 · AI 開始工作",
        body: "送出 prompt 後，agent 讀取、規劃，然後把程式碼寫進工作區。在右側面板可即時看見進度。",
      },
      "zh-CN": {
        title: "3 / 6 · AI 开始工作",
        body: "提交 prompt 后，agent 读取、规划，然后把代码写入工作区。在右侧面板实时观察进度。",
      },
      ja: {
        title: "3 / 6 · AI が動き出す",
        body: "プロンプトを送信すると、エージェントが読解→計画→コード生成までを実行します。右ペインで進捗をライブで確認できます。",
      },
    },
  },
  {
    id: "preview",
    icon: Eye,
    copy: {
      en: {
        title: "4 / 6 · Preview appears",
        body: "Once the build succeeds, the centre pane renders the live result — iframe for web, device frame for mobile, runtime output for software.",
      },
      "zh-TW": {
        title: "4 / 6 · Preview 出現",
        body: "Build 完成後，中央 pane 會渲染成果——web 是 iframe、mobile 是 device frame、software 是 runtime output。",
      },
      "zh-CN": {
        title: "4 / 6 · Preview 出现",
        body: "Build 完成后，中央 pane 会渲染成果——web 是 iframe、mobile 是 device frame、software 是 runtime output。",
      },
      ja: {
        title: "4 / 6 · プレビューが表示される",
        body: "ビルド成功後、中央ペインに結果が出ます — Web は iframe、Mobile は device frame、Software は runtime 出力です。",
      },
    },
  },
  {
    id: "annotate",
    icon: MousePointerSquareDashed,
    copy: {
      en: {
        title: "5 / 6 · Annotate modifications",
        body: "Click anywhere on the preview to drop a pin or draw a region, type the change you want, and submit. The agent gets the structured payload — no copy-paste back into chat.",
      },
      "zh-TW": {
        title: "5 / 6 · 標註修改",
        body: "在 preview 上點一下下 pin 或框一塊區域，寫下你要的修改後送出。Agent 會收到結構化 payload，不必再貼回 chat。",
      },
      "zh-CN": {
        title: "5 / 6 · 标注修改",
        body: "在 preview 上点一下下 pin 或框一块区域，写下你要的修改后提交。Agent 会收到结构化 payload，不必再贴回 chat。",
      },
      ja: {
        title: "5 / 6 · 修正をアノテーション",
        body: "プレビュー上でクリックして pin を打つ、または領域を囲み、修正内容を書いて送信。エージェントは構造化された payload を受け取るので、チャットへの貼り直しは不要です。",
      },
    },
  },
  {
    id: "deploy",
    icon: Rocket,
    copy: {
      en: {
        title: "6 / 6 · Deploy",
        body: "Happy with the result? Trigger Deploy from the workspace controls — web ships to your hosting target, mobile builds an installable artifact, software emits a release container.",
      },
      "zh-TW": {
        title: "6 / 6 · 部署",
        body: "成果滿意？從工作區的控制列觸發 Deploy——web 推到指定 hosting、mobile 產出 installable artifact、software 產出 release container。",
      },
      "zh-CN": {
        title: "6 / 6 · 部署",
        body: "成果满意？从工作区的控制栏触发 Deploy——web 推到指定 hosting、mobile 产出 installable artifact、software 产出 release container。",
      },
      ja: {
        title: "6 / 6 · デプロイ",
        body: "結果に満足したら、ワークスペースのコントロールから Deploy を実行 — Web はホスティング先へ、Mobile は配布用 artifact、Software は release コンテナを生成します。",
      },
    },
  },
])

/** Per-workspace framework hint shown alongside step 1 ("Pick a framework"). */
export const FRAMEWORK_HINTS: Record<WorkspaceType, Record<Locale, string>> = Object.freeze({
  web: {
    en: "Web — Next.js + React + shadcn/ui (look for the shadcn palette in the sidebar).",
    "zh-TW": "Web — Next.js + React + shadcn/ui（在 sidebar 找 shadcn palette）。",
    "zh-CN": "Web — Next.js + React + shadcn/ui（在 sidebar 找 shadcn palette）。",
    ja: "Web — Next.js + React + shadcn/ui（サイドバーの shadcn palette を確認）。",
  },
  mobile: {
    en: "Mobile — iOS · Android · Flutter · React Native (platform selector lives in the sidebar).",
    "zh-TW": "Mobile — iOS · Android · Flutter · React Native（platform selector 在 sidebar）。",
    "zh-CN": "Mobile — iOS · Android · Flutter · React Native（platform selector 在 sidebar）。",
    ja: "Mobile — iOS · Android · Flutter · React Native（プラットフォームセレクタはサイドバー）。",
  },
  software: {
    en: "Software — Python · TypeScript · Go · Rust · Java · C++ (each language has its own framework dropdown).",
    "zh-TW": "Software — Python · TypeScript · Go · Rust · Java · C++（每個語言有自己的 framework dropdown）。",
    "zh-CN": "Software — Python · TypeScript · Go · Rust · Java · C++（每个语言有自己的 framework dropdown）。",
    ja: "Software — Python · TypeScript · Go · Rust · Java · C++（言語ごとにフレームワーク用 dropdown あり）。",
  },
})

/** Per-locale CTA labels — kept short to fit within the dialog footer. */
export const CTA_LABELS: Record<Locale, OnboardingActionLabels> = Object.freeze({
  en: { next: "Next", back: "Back", skip: "Skip tour", done: "Get started" },
  "zh-TW": { next: "下一步", back: "上一步", skip: "跳過導覽", done: "開始使用" },
  "zh-CN": { next: "下一步", back: "上一步", skip: "跳过导览", done: "开始使用" },
  ja: { next: "次へ", back: "戻る", skip: "ツアーをスキップ", done: "はじめる" },
})

/** Per-locale dialog chrome strings (header label, framework prefix, etc.). */
export const DIALOG_LABELS: Record<Locale, { ariaLabel: string; subtitle: string; progressLabel: string }> =
  Object.freeze({
    en: {
      ariaLabel: "Workspace onboarding tour",
      subtitle: "A six-step walkthrough of the workspace iteration loop.",
      progressLabel: "Tour progress",
    },
    "zh-TW": {
      ariaLabel: "工作區導覽",
      subtitle: "六個步驟帶你走完工作區迭代流程。",
      progressLabel: "導覽進度",
    },
    "zh-CN": {
      ariaLabel: "工作区导览",
      subtitle: "六个步骤带你走完工作区迭代流程。",
      progressLabel: "导览进度",
    },
    ja: {
      ariaLabel: "ワークスペースのチュートリアル",
      subtitle: "6 ステップでワークスペースの反復フローを案内します。",
      progressLabel: "進行状況",
    },
  })

// ─── Pure helpers (exported for unit tests) ───────────────────────────────

/** Clamp the step index into the valid range [0, ONBOARDING_STEPS.length - 1]. */
export function clampStepIndex(idx: number): number {
  if (!Number.isFinite(idx)) return 0
  if (idx < 0) return 0
  if (idx >= ONBOARDING_STEPS.length) return ONBOARDING_STEPS.length - 1
  return Math.floor(idx)
}

/** Compute the integer percentage for the progress bar — 1-indexed. */
export function progressPercent(idx: number): number {
  const clamped = clampStepIndex(idx)
  return Math.round(((clamped + 1) / ONBOARDING_STEPS.length) * 100)
}

/**
 * Resolve `?tour` URL param to a step index (0-based) or null when no
 * tour trigger is present.  Accepts either a 1-based integer (`?tour=3`)
 * or a step id (`?tour=preview`).  Out-of-range values resolve to 0
 * (start at the beginning) instead of returning null so the operator
 * never types a bad URL into a no-op.
 */
export function resolveTourParam(value: string | null | undefined): number | null {
  if (typeof value !== "string" || value.length === 0) return null
  // Match against the canonical step ids.
  const byId = ONBOARDING_STEPS.findIndex((s) => s.id === value)
  if (byId >= 0) return byId
  // Numeric 1-based index.
  const asNum = parseInt(value, 10)
  if (Number.isFinite(asNum) && asNum >= 1 && asNum <= ONBOARDING_STEPS.length) {
    return asNum - 1
  }
  // Any other non-empty value (e.g. "1", "true") — start at step 0.
  return 0
}

// ─── Internal hooks ────────────────────────────────────────────────────────

/** Try the I18n context; fall back to "en" if no provider is mounted. */
function useLocale(override?: Locale): Locale {
  let ctx: Locale = "en"
  try {
    ctx = _useI18n().locale
  } catch {
    ctx = "en"
  }
  return override ?? ctx
}

/**
 * Resolve the persistence backend.  Prefers user-scoped storage (so the
 * "seen" flag follows the operator across browsers signed-in to the
 * same account); falls back to plain localStorage when no auth/tenant
 * context is mounted (e.g. unit tests for this component in isolation).
 *
 * Returns null on the SSR pass — the caller must short-circuit to the
 * "not yet visible" branch.
 */
function useTourStorage(impl?: WorkspaceOnboardingTourProps["storageImpl"]) {
  const auth = (() => {
    try {
      return useAuth()
    } catch {
      return null
    }
  })()
  const tenant = (() => {
    try {
      return useTenant()
    } catch {
      return null
    }
  })()

  return React.useMemo(() => {
    if (impl) return impl
    if (typeof window === "undefined") return null
    const userId = auth?.user?.id ?? null
    const tenantId = tenant?.currentTenantId ?? null
    if (userId) {
      return getUserStorage(tenantId, userId)
    }
    // Pre-auth / public-mode fallback — bare localStorage with the same key.
    return {
      getItem: (k: string) => {
        try { return localStorage.getItem(`omnisight:_anonymous:${k.replace(/^omnisight:/, "")}`) } catch { return null }
      },
      setItem: (k: string, v: string) => {
        try { localStorage.setItem(`omnisight:_anonymous:${k.replace(/^omnisight:/, "")}`, v) } catch { /* quota */ }
      },
      removeItem: (k: string) => {
        try { localStorage.removeItem(`omnisight:_anonymous:${k.replace(/^omnisight:/, "")}`) } catch { /* */ }
      },
    }
    // We deliberately keep the deps shallow: auth/tenant context values
    // are themselves memoised in their providers, and the user / tenant
    // ids are the only fields we read.
  }, [impl, auth?.user?.id, tenant?.currentTenantId])
}

// ─── Component ────────────────────────────────────────────────────────────

export function WorkspaceOnboardingTour(props: WorkspaceOnboardingTourProps) {
  const {
    type,
    forceOpen,
    autoShow = true,
    onClose,
    storageImpl,
    searchParamsImpl,
    localeOverride,
    className,
  } = props

  const locale = useLocale(localeOverride)
  const storage = useTourStorage(storageImpl)
  const storageKey = onboardingStorageKey(type)

  const [open, setOpen] = React.useState<boolean>(Boolean(forceOpen))
  const [stepIdx, setStepIdx] = React.useState<number>(0)
  // Track whether we've performed the auto-trigger probe to avoid
  // double-firing in React strict-mode dev double-effects.
  const probedRef = React.useRef<boolean>(false)

  // Auto-trigger logic: run once on mount.
  React.useEffect(() => {
    if (forceOpen) return
    if (probedRef.current) return
    probedRef.current = true
    if (typeof window === "undefined") return

    const params =
      searchParamsImpl ?? new URLSearchParams(window.location.search)
    const fromParam = resolveTourParam(params.get("tour"))
    if (fromParam !== null) {
      setStepIdx(clampStepIndex(fromParam))
      setOpen(true)
      return
    }
    if (!autoShow) return
    if (!storage) return
    const seen = storage.getItem(storageKey)
    if (seen !== "1") {
      setStepIdx(0)
      setOpen(true)
    }
    // Run-once on mount: deps deliberately empty so the probe doesn't
    // re-fire on storage / locale changes mid-tour.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const persistSeen = React.useCallback(() => {
    if (forceOpen) return
    if (!storage) return
    storage.setItem(storageKey, "1")
  }, [forceOpen, storage, storageKey])

  const closeTour = React.useCallback(
    (reason: "completed" | "skipped") => {
      setOpen(false)
      persistSeen()
      // Strip the `?tour=` URL param so a refresh doesn't re-trigger
      // the manual override.  Keep other params intact.
      if (typeof window !== "undefined") {
        try {
          const u = new URL(window.location.href)
          if (u.searchParams.has("tour")) {
            u.searchParams.delete("tour")
            window.history.replaceState(null, "", u.toString())
          }
        } catch {
          /* SecurityError in some sandboxes — non-fatal */
        }
      }
      onClose?.(reason)
    },
    [onClose, persistSeen],
  )

  const advance = React.useCallback(
    (delta: number) => {
      setStepIdx((current) => {
        const next = current + delta
        if (next < 0) return 0
        if (next >= ONBOARDING_STEPS.length) {
          closeTour("completed")
          return current
        }
        return next
      })
    },
    [closeTour],
  )

  const handleOpenChange = React.useCallback(
    (nextOpen: boolean) => {
      if (nextOpen) {
        setOpen(true)
        return
      }
      // Radix calls onOpenChange(false) on Esc / overlay click / X — treat
      // any of those as "skipped" unless we're at the final step (then
      // the explicit Done button has already routed through "completed").
      closeTour("skipped")
    },
    [closeTour],
  )

  const step = ONBOARDING_STEPS[clampStepIndex(stepIdx)]
  const copy = step.copy[locale] ?? step.copy.en
  const cta = CTA_LABELS[locale] ?? CTA_LABELS.en
  const dialogChrome = DIALOG_LABELS[locale] ?? DIALOG_LABELS.en
  const isFinalStep = stepIdx === ONBOARDING_STEPS.length - 1
  const StepIcon = step.icon

  // Step-1 ("framework") gets the per-workspace framework hint inline.
  const frameworkHint =
    step.id === "framework" ? FRAMEWORK_HINTS[type][locale] ?? FRAMEWORK_HINTS[type].en : null

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="workspace-onboarding-dialog"
        data-workspace-type={type}
        data-step-id={step.id}
        data-step-index={String(stepIdx)}
        aria-label={dialogChrome.ariaLabel}
        className={cn("max-w-lg", className)}
      >
        <DialogHeader>
          <div className="flex items-center gap-2">
            <StepIcon
              className="size-5 text-primary"
              aria-hidden="true"
            />
            <DialogTitle data-testid="workspace-onboarding-step-title">
              {copy.title}
            </DialogTitle>
          </div>
          <DialogDescription
            data-testid="workspace-onboarding-step-body"
            className="leading-relaxed"
          >
            {copy.body}
          </DialogDescription>
        </DialogHeader>

        {frameworkHint && (
          <div
            data-testid="workspace-onboarding-framework-hint"
            className="rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
          >
            {frameworkHint}
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between text-[11px] uppercase tracking-wider text-muted-foreground">
            <span>{dialogChrome.progressLabel}</span>
            <span data-testid="workspace-onboarding-progress-readout">
              {clampStepIndex(stepIdx) + 1} / {ONBOARDING_STEPS.length}
            </span>
          </div>
          <Progress
            data-testid="workspace-onboarding-progress"
            value={progressPercent(stepIdx)}
            aria-label={dialogChrome.progressLabel}
          />
          <ol
            data-testid="workspace-onboarding-step-dots"
            className="mt-1 flex items-center justify-center gap-1.5"
            aria-hidden="true"
          >
            {ONBOARDING_STEPS.map((s, i) => {
              const active = i === clampStepIndex(stepIdx)
              const done = i < clampStepIndex(stepIdx)
              return (
                <li
                  key={s.id}
                  data-testid={`workspace-onboarding-step-dot-${s.id}`}
                  data-active={active ? "true" : "false"}
                  data-done={done ? "true" : "false"}
                  className={cn(
                    "size-1.5 rounded-full transition-colors",
                    active
                      ? "bg-primary"
                      : done
                        ? "bg-primary/60"
                        : "bg-muted-foreground/30",
                  )}
                />
              )
            })}
          </ol>
        </div>

        <DialogFooter className="flex !flex-row items-center justify-between gap-2">
          <Button
            type="button"
            data-testid="workspace-onboarding-skip"
            variant="ghost"
            size="sm"
            onClick={() => closeTour("skipped")}
          >
            {cta.skip}
          </Button>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              data-testid="workspace-onboarding-back"
              variant="outline"
              size="sm"
              onClick={() => advance(-1)}
              disabled={stepIdx === 0}
            >
              <ChevronLeft className="mr-1 size-3.5" aria-hidden="true" />
              {cta.back}
            </Button>
            <Button
              type="button"
              data-testid="workspace-onboarding-next"
              size="sm"
              onClick={() => advance(1)}
            >
              {isFinalStep ? cta.done : cta.next}
              {isFinalStep ? (
                <CheckCircle2 className="ml-1 size-3.5" aria-hidden="true" />
              ) : (
                <ChevronRight className="ml-1 size-3.5" aria-hidden="true" />
              )}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default WorkspaceOnboardingTour
