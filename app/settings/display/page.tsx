"use client"

/**
 * BS.3.6 — Display Settings page.
 *
 * Operator-facing surface that lets a user pick their preferred
 * `MotionLevel` (BS.3.3 persisted via the J4 user-preferences API),
 * see the choice rendered live in a `<MotionPreview />` demo card,
 * toggle the per-session "強制全開" battery override (BS.3.4), and
 * read off the current OS `prefers-reduced-motion` flag (BS.3.5
 * `usePrefersReducedMotion` re-export — read-only because R25.2
 * hard-overrides every other signal).
 *
 * Wiring summary
 * ──────────────
 *   - `getMotionPreference()` / `setMotionPreference()` (BS.3.3)
 *     for persistence; the BS.3.5 event bus (`subscribeMotionPreference`)
 *     means the embedded `<MotionPreview />` re-renders the moment
 *     this page writes a new value, no full remount needed.
 *   - `useBatteryAwareMotion(userPref)` (BS.3.4) for the live
 *     battery readout (level / charging / tier / didDegrade) and the
 *     `forceFullOverride` toggle. We call it with the *currently
 *     loaded* `userPref` so the displayed `effective` matches what
 *     the rest of the app would resolve to for the same pref.
 *   - `usePrefersReducedMotion()` (BS.3.5) for the OS read-only
 *     badge — surfaced as informational only because the resolver
 *     forces `"off"` whenever this returns `true`, regardless of
 *     anything the user picks here.
 *   - `<Toaster />` mounted at the bottom of the page so the
 *     "saved" / "save failed" / BS.3.4 battery-degrade toasts
 *     actually render (the `Providers` tree mounts custom toast
 *     centres but not the standard `<Toaster />`).
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state introduced by this page. Reads:
 *   - `useAuth()` — per-tab React context (already audited).
 *   - `getMotionPreference()` — HTTP GET against J4 user-preferences
 *     (server-of-record; per-tenant, per-user row).
 *   - `useBatteryAwareMotion()` — per-component-instance hook
 *     (BS.3.4 already audited).
 *   - `usePrefersReducedMotion()` — per-component-instance MQL
 *     listener (BS.3.5 already audited).
 * Writes:
 *   - `setMotionPreference()` — HTTP PUT to user-preferences;
 *     dispatches `omnisight:motion-pref-changed` on success which
 *     the BS.3.5 event bus picks up in this same tab.
 * Per-worker consistency: not applicable — all state is browser-side
 * or persisted via PG (J4 idempotent upsert handles cross-worker).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * `setMotionPreference()` awaits the HTTP PUT *and then* dispatches
 * the same-tab event before resolving. The local `userPref` state
 * is updated optimistically before the PUT (so the demo card
 * reflects the choice immediately); the event bus fires after the
 * PUT for any *other* mounted hook in this tab. There is no read
 * that must observe the write — both observers (this page's local
 * state + any other `useUserMotionPreference()` mount) read from
 * different sources and converge by design.
 */

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  Battery,
  BatteryCharging,
  BatteryFull,
  BatteryLow,
  BatteryWarning,
  ChevronRight,
  Eye,
  Info,
  Loader2,
  MonitorSmartphone,
  Save,
  Sparkles,
  Zap,
} from "lucide-react"

import { MotionPreview } from "@/components/omnisight/motion-preview"
import { Switch } from "@/components/ui/switch"
import { Toaster } from "@/components/ui/toaster"
import { toast } from "@/hooks/use-toast"
import { usePrefersReducedMotion } from "@/hooks/use-effective-motion-level"
import {
  type BatteryTier,
  useBatteryAwareMotion,
} from "@/lib/battery-aware-motion"
import {
  DEFAULT_MOTION_LEVEL,
  MOTION_LEVELS,
  type MotionLevel,
  getMotionPreference,
  setMotionPreference,
} from "@/lib/motion-preferences"

// ─────────────────────────────────────────────────────────────────────
// Static copy / lookup tables
// ─────────────────────────────────────────────────────────────────────

const LEVEL_DESCRIPTIONS: Record<MotionLevel, { title: string; body: string }> = {
  off: {
    title: "Off — 完全靜止",
    body: "停用所有動畫。前庭敏感、容易頭暈、或追求極簡的使用者建議選用。",
  },
  subtle: {
    title: "Subtle — 輕微浮動",
    body: "保留最低限度的漂浮 / 視差 / 距離光暈，幅度約為 dramatic 的 1/3。",
  },
  normal: {
    title: "Normal — 含磁吸傾斜",
    body: "啟用層 1-6（含 hover 磁吸傾斜）；層 7 玻璃反射、層 3 軌道仍關閉。",
  },
  dramatic: {
    title: "Dramatic — 全 8 層全開（預設）",
    body: "啟用全 8 層動畫含玻璃反射、軌道旋轉、群組呼吸；最完整的 OmniSight 視覺體驗。",
  },
}

const TIER_LABEL: Record<BatteryTier, { label: string; tone: string }> = {
  plenty: { label: "Plenty (≥50%)", tone: "text-[var(--neural-green)]" },
  moderate: { label: "Moderate (30-50%)", tone: "text-[var(--neural-blue)]" },
  low: { label: "Low (15-30%)", tone: "text-[var(--neural-orange)]" },
  critical: { label: "Critical (<15%)", tone: "text-[var(--destructive)]" },
}

function batteryIcon(tier: BatteryTier, charging: boolean) {
  if (charging) return BatteryCharging
  switch (tier) {
    case "plenty":
      return BatteryFull
    case "moderate":
      return Battery
    case "low":
      return BatteryLow
    case "critical":
      return BatteryWarning
  }
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────

export default function DisplaySettingsPage() {
  const [userPref, setUserPref] = useState<MotionLevel>(DEFAULT_MOTION_LEVEL)
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reducedMotion = usePrefersReducedMotion()
  const battery = useBatteryAwareMotion(userPref)

  // Initial load. The BS.3.5 event bus also picks up other-tab /
  // same-tab writes, but on this page we *are* the writer, so the
  // optimistic state below is authoritative — we still subscribe in
  // the embedded `<MotionPreview />` via the BS.3.5 hook chain.
  useEffect(() => {
    let cancelled = false
    void getMotionPreference()
      .then((value) => {
        if (cancelled) return
        setUserPref(value)
      })
      .finally(() => {
        if (!cancelled) setLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const onSelect = useCallback(async (next: MotionLevel) => {
    if (next === userPref || saving) return
    const previous = userPref
    setUserPref(next)
    setSaving(true)
    setError(null)
    try {
      await setMotionPreference(next)
      toast({
        title: "已儲存動態偏好",
        description: `Motion level → ${next}`,
      })
    } catch (exc) {
      setUserPref(previous)
      const message = exc instanceof Error ? exc.message : String(exc)
      setError(message)
      toast({
        title: "儲存失敗",
        description: message,
        variant: "destructive",
      })
    } finally {
      setSaving(false)
    }
  }, [saving, userPref])

  const BatteryIcon = batteryIcon(battery.tier, battery.status.charging)
  const tierMeta = TIER_LABEL[battery.tier]

  // BS.11.1 — surface the resolved motion-suppress signal on the
  // page root so contract tests can lock the wiring without scraping
  // CSS / Tailwind class strings. `motion-suppressed` is true when
  // either the OS asks for reduce or the user has explicitly picked
  // ``off`` here on this page (the latter is JS-only and the page
  // owns its own state ahead of the BS.3.5 round-trip).
  const motionSuppressed = reducedMotion || userPref === "off"

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="display-settings-page"
      data-motion-suppressed={motionSuppressed ? "true" : "false"}
      data-os-reduced-motion={reducedMotion ? "true" : "false"}
      data-user-pref={userPref}
    >
      <div className="mx-auto max-w-4xl">
        {/* ── Breadcrumb + heading ─────────────────────────────────── */}
        <header className="mb-8">
          <div className="mb-1 flex items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground)]">
            <Link
              href="/"
              className="inline-flex items-center gap-1 hover:text-[var(--foreground)]"
            >
              <ArrowLeft size={10} /> dashboard
            </Link>
            <ChevronRight size={10} />
            <span>settings</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">display</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <Eye size={20} />
            Display Settings
          </h1>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            控制 OmniSight UI 的動態效果強度。系統會根據你的選擇、目前電量、
            以及 OS prefers-reduced-motion 偏好自動套用最終層級。
          </p>
        </header>

        {/* ── Live preview ─────────────────────────────────────────── */}
        <section className="mb-8">
          <h2 className="mb-3 flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--muted-foreground)]">
            <Sparkles size={12} /> Live Preview
          </h2>
          <MotionPreview />
        </section>

        {/* ── Motion level radios ──────────────────────────────────── */}
        <section className="mb-8">
          <h2 className="mb-3 flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--muted-foreground)]">
            <MonitorSmartphone size={12} /> Motion Level
          </h2>
          <fieldset
            data-testid="motion-level-radios"
            className="space-y-2"
            disabled={!loaded || saving}
          >
            <legend className="sr-only">選擇動態效果層級</legend>
            {MOTION_LEVELS.map((level) => {
              const meta = LEVEL_DESCRIPTIONS[level]
              const checked = userPref === level
              return (
                <label
                  key={level}
                  data-testid={`motion-level-radio-${level}`}
                  className={[
                    "flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors",
                    checked
                      ? "border-[var(--neural-blue)] bg-[var(--neural-blue)]/5"
                      : "border-[var(--border)] hover:bg-[var(--secondary)]/30",
                    !loaded || saving ? "cursor-wait opacity-60" : "",
                  ].join(" ")}
                >
                  <input
                    type="radio"
                    name="motion-level"
                    value={level}
                    checked={checked}
                    onChange={() => void onSelect(level)}
                    className="mt-1 h-4 w-4 accent-[var(--neural-blue)]"
                    aria-describedby={`motion-level-desc-${level}`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{meta.title}</div>
                    <div
                      id={`motion-level-desc-${level}`}
                      className="mt-0.5 text-xs text-[var(--muted-foreground)]"
                    >
                      {meta.body}
                    </div>
                  </div>
                  {checked && saving && (
                    <Loader2
                      size={14}
                      data-testid={`motion-level-saving-spinner-${level}`}
                      data-motion-spin={
                        reducedMotion || userPref === "off" ? "off" : "on"
                      }
                      className={[
                        "mt-1 shrink-0 text-[var(--muted-foreground)]",
                        // BS.11.1 — drop the spin class when the user has
                        // chosen `motion: off` (or the OS asks for reduce).
                        // The R25.2 global CSS fallback only catches the
                        // OS flag; the in-app off pref is JS-only and
                        // would otherwise still spin even though every
                        // *other* surface honours it.
                        reducedMotion || userPref === "off" ? "" : "animate-spin",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    />
                  )}
                  {checked && !saving && loaded && (
                    <Save
                      size={14}
                      className="mt-1 shrink-0 text-[var(--neural-green)]"
                    />
                  )}
                </label>
              )
            })}
          </fieldset>
          {error && (
            <p
              data-testid="motion-level-error"
              className="mt-2 text-xs text-[var(--destructive)]"
            >
              {error}
            </p>
          )}
        </section>

        {/* ── Battery rule + override ─────────────────────────────── */}
        <section className="mb-8">
          <h2 className="mb-3 flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--muted-foreground)]">
            <Zap size={12} /> Battery Rule
          </h2>
          <div
            data-testid="battery-rule-panel"
            className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm">
                  <BatteryIcon size={16} className={tierMeta.tone} />
                  {battery.status.unsupported ? (
                    <span data-testid="battery-status">
                      Battery API unavailable — 視為已接電源、不套用降級。
                    </span>
                  ) : (
                    <span data-testid="battery-status">
                      電量{" "}
                      <span className="font-mono">
                        {(battery.status.level * 100).toFixed(0)}%
                      </span>
                      {battery.status.charging && (
                        <span className="text-[var(--muted-foreground)]"> （充電中）</span>
                      )}
                      <span className={`ml-2 font-mono text-[10px] ${tierMeta.tone}`}>
                        [{tierMeta.label}]
                      </span>
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                  低電量時自動降低動態強度（&gt;50% → 你的偏好 / 30-50% → 降一級 /
                  15-30% → 強制 subtle / &lt;15% → 強制 off）。Firefox / Safari 沒有
                  Battery API、視為已接電源。
                </p>
                {battery.didDegrade && !battery.forceFullOverride && (
                  <p
                    data-testid="battery-degrade-notice"
                    className="mt-2 inline-flex items-center gap-1 rounded bg-[var(--neural-orange)]/10 px-2 py-1 text-xs text-[var(--neural-orange)]"
                  >
                    <Info size={11} />
                    目前電池規則已將層級從 <code className="px-1 font-mono">{userPref}</code>{" "}
                    降至 <code className="px-1 font-mono">{battery.effective}</code>。
                  </p>
                )}
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <label className="flex cursor-pointer items-center gap-2 text-xs">
                  <span>強制全開</span>
                  <Switch
                    data-testid="force-full-override-switch"
                    checked={battery.forceFullOverride}
                    onCheckedChange={battery.setForceFullOverride}
                  />
                </label>
                <span className="text-[10px] text-[var(--muted-foreground)]">
                  此分頁有效；重開恢復
                </span>
              </div>
            </div>
          </div>
        </section>

        {/* ── OS prefers-reduced-motion (read-only) ────────────────── */}
        <section className="mb-8">
          <h2 className="mb-3 flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-[var(--muted-foreground)]">
            <Info size={12} /> OS prefers-reduced-motion
          </h2>
          <div
            data-testid="reduced-motion-panel"
            className="flex items-start gap-3 rounded-lg border border-[var(--border)] bg-[var(--card)] p-4"
          >
            <span
              data-testid="reduced-motion-state"
              data-reduced={reducedMotion ? "true" : "false"}
              className={[
                "mt-0.5 inline-block rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
                reducedMotion
                  ? "bg-[var(--destructive)]/15 text-[var(--destructive)]"
                  : "bg-[var(--secondary)]/40 text-[var(--muted-foreground)]",
              ].join(" ")}
            >
              {reducedMotion ? "reduce" : "no-preference"}
            </span>
            <div className="min-w-0 flex-1 text-xs text-[var(--muted-foreground)]">
              {reducedMotion ? (
                <>
                  你的 OS 已開啟 <code className="font-mono">prefers-reduced-motion</code>。
                  為符合 WCAG 2.3.3，OmniSight 會強制將動態層級設為{" "}
                  <code className="font-mono">off</code>，不論你在上方選擇何者。
                  關掉 OS 偏好後本頁會自動更新。
                </>
              ) : (
                <>
                  你的 OS 未啟用 <code className="font-mono">prefers-reduced-motion</code>。
                  此狀態為唯讀；要切換請至 OS 系統設定（macOS：輔助使用 → 顯示 → 減少動態效果；
                  Windows：設定 → 協助工具 → 視覺效果 → 動畫效果）。
                </>
              )}
            </div>
          </div>
        </section>

        {/* ── Footer note ──────────────────────────────────────────── */}
        <footer className="text-[10px] font-mono text-[var(--muted-foreground)]">
          解析順序：prefers-reduced-motion ▸ motion: off ▸ 電池規則 ▸ 使用者偏好。
          目前實際套用層級：
          <code
            data-testid="effective-level-readout"
            className="ml-1 px-1 text-[var(--foreground)]"
          >
            {reducedMotion ? "off (forced by OS)" : battery.effective}
          </code>
        </footer>
      </div>

      {/* `<Toaster />` mount — `Providers` doesn't ship the standard
          shadcn toaster (only the custom OmniSight centres), so we
          mount it here for the BS.3.4 battery-degrade toast and the
          save-success / save-failed toasts above to actually appear. */}
      <Toaster />
    </main>
  )
}
