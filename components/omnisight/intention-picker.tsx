"use client"

/**
 * WP.4 — Onboarding Intention Picker (first-run, ahead of Y6 dashboard tour).
 *
 * First-run modal that asks 「你來這做什麼?」 with 5 mutually-exclusive
 * journeys. The user's pick is persisted as the ``onboarding_intention``
 * user preference (cross-device-synced via the J4 user-preferences
 * router) and a local-storage seen flag. Selection drives the default
 * landing tile by dispatching ``omnisight:navigate`` for the panel
 * matching the chosen journey; the existing FirstRunTour / wizard /
 * sample-data flows pick up afterwards.
 *
 * Gate: shown when the user has neither the per-tenant/per-user local
 * ``omnisight:intention:seen`` flag nor a server-side
 * ``onboarding_intention`` preference. Skip / dismiss / select all
 * mark seen; selection also marks the NewProjectWizard's
 * ``wizard_seen`` flag because the wizard's "how do you want to
 * start" question is subsumed once we know the journey.
 *
 * See ``docs/design/wp-warp-inspired-patterns.md`` §"WP.4" for the
 * pattern source (Warp ``agent_onboarding_view.rs`` intention slide).
 */

import { useEffect, useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"
import { Cpu, Bot, Sparkles, Workflow, Compass } from "lucide-react"
import type { PanelId } from "@/components/omnisight/mobile-nav"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { getUserStorage } from "@/lib/storage"
import { getUserPreference, setUserPreference } from "@/lib/api"
import { useI18n as _useI18n, type Locale } from "@/lib/i18n/context"

export const INTENTION_PREF_KEY = "onboarding_intention"
export const INTENTION_SEEN_LS = "omnisight:intention:seen"
const WIZARD_SEEN_LS = "omnisight:wizard:seen"
const WIZARD_SEEN_PREF_KEY = "wizard_seen"

export type IntentionId =
  | "hd_verification"
  | "multi_agent_dispatch"
  | "web_app_generation"
  | "sandbox_dev"
  | "exploring"

interface IntentionChoice {
  id: IntentionId
  panel: PanelId
  icon: React.ElementType
  copy: Record<Locale, { label: string; description: string }>
}

const CHOICES: IntentionChoice[] = [
  {
    id: "hd_verification",
    panel: "host",
    icon: Cpu,
    copy: {
      en: {
        label: "HD verification",
        description: "Embedded hardware, schematic review, sensor swap.",
      },
      "zh-TW": {
        label: "HD 硬體驗證",
        description: "嵌入式硬體、schematic、sensor 替換。",
      },
      "zh-CN": {
        label: "HD 硬件验证",
        description: "嵌入式硬件、schematic、sensor 替换。",
      },
      ja: {
        label: "HD ハードウェア検証",
        description: "組込みハードウェア、スキマティック、センサー差替。",
      },
    },
  },
  {
    id: "multi_agent_dispatch",
    panel: "agents",
    icon: Bot,
    copy: {
      en: {
        label: "Multi-agent dispatch",
        description: "Coordinate BP / Guild / agent fleet workloads.",
      },
      "zh-TW": {
        label: "Multi-agent dispatch",
        description: "BP / Guild / agent fleet 任務協調。",
      },
      "zh-CN": {
        label: "Multi-agent 调度",
        description: "BP / Guild / agent fleet 任务协调。",
      },
      ja: {
        label: "マルチエージェント割当",
        description: "BP / Guild / agent fleet のタスク統制。",
      },
    },
  },
  {
    id: "web_app_generation",
    panel: "spec",
    icon: Sparkles,
    copy: {
      en: {
        label: "Web app generation",
        description: "Author a spec → DAG → ship (W11-W16 / FS / SC).",
      },
      "zh-TW": {
        label: "Web app 生成",
        description: "Spec → DAG → 出貨（W11-W16 / FS / SC）。",
      },
      "zh-CN": {
        label: "Web app 生成",
        description: "Spec → DAG → 出货（W11-W16 / FS / SC）。",
      },
      ja: {
        label: "Web アプリ生成",
        description: "Spec → DAG → 出荷 (W11-W16 / FS / SC)。",
      },
    },
  },
  {
    id: "sandbox_dev",
    panel: "dag",
    icon: Workflow,
    copy: {
      en: {
        label: "Sandbox dev",
        description: "Live-preview iteration in W14 sandbox / DAG editor.",
      },
      "zh-TW": {
        label: "Sandbox 開發",
        description: "W14 sandbox 即時預覽、DAG editor 迭代。",
      },
      "zh-CN": {
        label: "Sandbox 开发",
        description: "W14 sandbox 即时预览、DAG editor 迭代。",
      },
      ja: {
        label: "Sandbox 開発",
        description: "W14 sandbox ライブプレビュー / DAG エディタで反復。",
      },
    },
  },
  {
    id: "exploring",
    panel: "orchestrator",
    icon: Compass,
    copy: {
      en: {
        label: "Just exploring",
        description: "Open the orchestrator and look around — no presets.",
      },
      "zh-TW": {
        label: "先看看",
        description: "進 orchestrator 自由探索、不套用 preset。",
      },
      "zh-CN": {
        label: "先看看",
        description: "进 orchestrator 自由探索、不套用 preset。",
      },
      ja: {
        label: "ひとまず見るだけ",
        description: "orchestrator を開いて自由に探索（プリセット無し）。",
      },
    },
  },
]

const HEADING: Record<Locale, { title: string; description: string; skip: string }> = {
  en: {
    title: "What brings you here?",
    description:
      "Pick a journey so the dashboard can land you on the right tile. You can change this any time from settings.",
    skip: "Skip",
  },
  "zh-TW": {
    title: "你來這做什麼?",
    description:
      "選一個用途、dashboard 會帶你到對應的 tile。隨時可在設定改。",
    skip: "略過",
  },
  "zh-CN": {
    title: "你来这做什么?",
    description:
      "选一个用途、dashboard 会带你到对应的 tile。随时可在设置改。",
    skip: "跳过",
  },
  ja: {
    title: "今日は何をしますか?",
    description:
      "用途を選ぶと dashboard が最適な tile を表示します。設定からいつでも変更可。",
    skip: "スキップ",
  },
}

function useLocale(): Locale {
  try {
    return _useI18n().locale
  } catch {
    return "en"
  }
}

function navigateToPanel(panel: PanelId) {
  window.dispatchEvent(
    new CustomEvent("omnisight:navigate", { detail: { panel } }),
  )
}

export function IntentionPicker() {
  const [open, setOpen] = useState(false)
  const locale = useLocale()
  const { user } = useAuth()
  const { currentTenantId } = useTenant()
  const userId = user?.id ?? null

  useEffect(() => {
    if (typeof window === "undefined" || !userId) return
    const store = getUserStorage(currentTenantId, userId)

    // ?intention=1 forces replay (parity with FirstRunTour's ?tour=1).
    const params = new URLSearchParams(window.location.search)
    const forceReplay = params.get("intention") === "1"

    if (!forceReplay && store.getItem(INTENTION_SEEN_LS) === "1") return

    let cancelled = false
    if (forceReplay) {
      // Defer through a microtask so setState happens in a follow-up
      // tick rather than synchronously inside the effect body — keeps
      // the react-hooks/set-state-in-effect lint rule happy, parity
      // with NewProjectWizard's promise-callback pattern.
      Promise.resolve().then(() => {
        if (!cancelled) setOpen(true)
      })
      return () => {
        cancelled = true
      }
    }

    getUserPreference(INTENTION_PREF_KEY)
      .then((pref) => {
        if (cancelled) return
        if (pref?.value) {
          // Server says this user already picked. Mirror locally so the
          // next mount short-circuits without an extra fetch.
          store.setItem(INTENTION_SEEN_LS, "1")
          return
        }
        setOpen(true)
      })
      .catch(() => {
        if (!cancelled) setOpen(true)
      })

    return () => {
      cancelled = true
    }
  }, [userId, currentTenantId])

  function markSeen() {
    if (!userId) return
    const store = getUserStorage(currentTenantId, userId)
    store.setItem(INTENTION_SEEN_LS, "1")
  }

  function suppressWizard() {
    // Once an intention is set, NewProjectWizard's "how do you want to
    // start" prompt is redundant — the journey is the answer.
    if (!userId) return
    const store = getUserStorage(currentTenantId, userId)
    store.setItem(WIZARD_SEEN_LS, "1")
    setUserPreference(WIZARD_SEEN_PREF_KEY, "1").catch(() => {})
  }

  function handleChoice(choice: IntentionChoice) {
    markSeen()
    suppressWizard()
    setUserPreference(INTENTION_PREF_KEY, choice.id).catch(() => {})
    setOpen(false)
    if (typeof window !== "undefined") {
      const u = new URL(window.location.href)
      if (u.searchParams.has("intention")) {
        u.searchParams.delete("intention")
        window.history.replaceState(null, "", u.toString())
      }
    }
    navigateToPanel(choice.panel)
  }

  function handleSkip() {
    markSeen()
    // Skip is recorded as the explicit "exploring" choice — it's a
    // valid journey, not an unanswered question. This matches the design
    // doc: "skip 可走、不強制" with the default being orchestrator.
    setUserPreference(INTENTION_PREF_KEY, "exploring").catch(() => {})
    setOpen(false)
    if (typeof window !== "undefined") {
      const u = new URL(window.location.href)
      if (u.searchParams.has("intention")) {
        u.searchParams.delete("intention")
        window.history.replaceState(null, "", u.toString())
      }
    }
  }

  function handleDismiss(openState: boolean) {
    if (!openState) handleSkip()
  }

  const heading = HEADING[locale]

  return (
    <Dialog open={open} onOpenChange={handleDismiss}>
      <DialogContent
        className="sm:max-w-2xl"
        data-testid="intention-picker"
      >
        <DialogHeader>
          <DialogTitle>{heading.title}</DialogTitle>
          <DialogDescription>{heading.description}</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2">
          {CHOICES.map((choice) => {
            const Icon = choice.icon
            const copy = choice.copy[locale]
            return (
              <button
                key={choice.id}
                type="button"
                data-testid={`intention-choice-${choice.id}`}
                onClick={() => handleChoice(choice)}
                className="flex flex-col items-start gap-2 rounded-lg border border-border p-4 text-left transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <div className="text-muted-foreground">
                  <Icon className="size-6" aria-hidden />
                </div>
                <div className="font-medium text-sm">{copy.label}</div>
                <div className="text-xs text-muted-foreground leading-snug">
                  {copy.description}
                </div>
              </button>
            )
          })}
        </div>
        <div className="flex justify-end pt-1">
          <button
            type="button"
            data-testid="intention-skip"
            onClick={handleSkip}
            className="font-mono text-xs text-muted-foreground hover:text-foreground underline-offset-4 hover:underline"
          >
            {heading.skip}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
