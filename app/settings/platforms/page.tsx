"use client"

/**
 * BS.5.1 — Settings → Platforms page skeleton.
 *
 * Operator entry point for the BS Vertical-Aware Bootstrap surface (the
 * "Platforms" experience defined in `docs/design/bs-bootstrap-vertical-
 * aware.md` §7.4). This row only ships the **route shell** + the **three
 * sub-tab routing** contract; the actual hero / catalog / installed /
 * sources content lands in BS.5.2-BS.5.4 + BS.6.* + BS.7.* + BS.8.*.
 *
 * BS.7.1 — install button wiring (this file owns the integration glue).
 * Clicking ``Install`` on a `<CatalogCard />` (or ``Update`` on the
 * `<CatalogDetailPanel />` primary CTA) calls
 * ``createInstallJob(entry.id)``, which POSTs ``/installer/jobs``. The
 * route runs through the existing R20-A PEP gateway HOLD by virtue of
 * ``tool="install_entry"`` not being on any tier whitelist (see
 * ``backend/routers/installer.py::INSTALL_PEP_TOOL``); the global
 * ``<ToastCenter />`` already surfaces the ``decision_pending`` SSE
 * event with approve / reject buttons. On approve → 201 + queued job;
 * on deny / timeout → 403 surfaced via the global
 * `<ApiErrorToastCenter />`. BS.6.7's `<PendingInstallTooltip />` flips
 * to its passthrough branch the moment ``onInstall`` is wired, so this
 * change activates the install affordance across both card and panel.
 *
 * Sub-tab contract (frozen now so subsequent BS rows can deep-link in):
 *   ?tab=catalog    → catalog browse (BS.6 lands the cards + 5-state)
 *   ?tab=installed  → already-installed list (BS.6.x lands)
 *   ?tab=sources    → catalog feed CRUD (BS.6.x admin only)
 *
 * The default tab when `?tab=` is absent or invalid is `catalog`. The
 * tab is reflected back into the URL via `router.replace` so deep-links
 * stay shareable; switching tabs preserves browser history (push) so
 * back/forward navigates between tabs as users expect.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state introduced. The page reads
 * `useSearchParams()` (per-tab Next.js router state) and the URL is the
 * only source of truth for the active tab — no in-memory cache, no
 * cross-worker concern (browser-side state only).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Tab switches are local URL replaces; `useSearchParams()` re-renders
 * on the next React tick. There is no API round-trip on the skeleton
 * (BS.5.2+ owns the data fetches inside each panel) so no read-after-
 * write race exists at this layer.
 */

import { Suspense, useCallback, useMemo } from "react"
import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import {
  ArrowLeft,
  Boxes,
  CheckCircle2,
  ChevronRight,
  Layers,
  Rss,
} from "lucide-react"

import { CatalogCard } from "@/components/omnisight/catalog-card"
import { CatalogDetailPanel } from "@/components/omnisight/catalog-detail-panel"
import {
  CatalogTab,
  type CatalogEntry,
} from "@/components/omnisight/catalog-tab"
import {
  PLATFORM_COUNTERS_ZERO,
  PlatformHero,
  type PlatformCounters,
} from "@/components/omnisight/platform-hero"
import { useHostMetricsTick } from "@/hooks/use-host-metrics-tick"
import { createInstallJob } from "@/lib/api"

// ─────────────────────────────────────────────────────────────────────
// Sub-tab contract — exported so BS.5.x tests + future deep-link
// builders share the same source of truth.
// ─────────────────────────────────────────────────────────────────────

export const PLATFORMS_TABS = ["catalog", "installed", "sources"] as const
export type PlatformsTabId = (typeof PLATFORMS_TABS)[number]
export const PLATFORMS_DEFAULT_TAB: PlatformsTabId = "catalog"

/** Coerce an arbitrary `?tab=` query string into a known tab, falling
 *  back to the default. Exported for tests and for the embedded shell
 *  to reuse the exact same coercion. */
export function coercePlatformsTab(value: string | null | undefined): PlatformsTabId {
  if (value && (PLATFORMS_TABS as readonly string[]).includes(value)) {
    return value as PlatformsTabId
  }
  return PLATFORMS_DEFAULT_TAB
}

interface TabMeta {
  id: PlatformsTabId
  label: string
  description: string
  icon: typeof Boxes
}

const TAB_META: Record<PlatformsTabId, TabMeta> = {
  catalog: {
    id: "catalog",
    label: "Catalog",
    description: "瀏覽可安裝的 vertical / SDK / runtime / BSP entries。",
    icon: Boxes,
  },
  installed: {
    id: "installed",
    label: "Installed",
    description: "目前已部署在此 tenant 上的 platform entries。",
    icon: CheckCircle2,
  },
  sources: {
    id: "sources",
    label: "Sources",
    description: "管理 catalog feed 訂閱（admin only）。BS.6.x 進駐。",
    icon: Rss,
  },
}

// ─────────────────────────────────────────────────────────────────────
// Inner shell — wrapped in Suspense per Next.js 15 / React 19 rule
// that `useSearchParams()` must be inside a Suspense boundary.
// ─────────────────────────────────────────────────────────────────────

function PlatformsPageInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const tab = useMemo(
    () => coercePlatformsTab(searchParams.get("tab")),
    [searchParams],
  )

  const onSelectTab = useCallback(
    (next: PlatformsTabId) => {
      if (next === tab) return
      const params = new URLSearchParams(searchParams.toString())
      params.set("tab", next)
      // `push` so back/forward navigates between tabs the way operators
      // expect; deep-links remain shareable because URL stays in sync.
      router.push(`/settings/platforms?${params.toString()}`)
    },
    [router, searchParams, tab],
  )

  const panelMeta = TAB_META[tab]
  const PanelIcon = panelMeta.icon

  // Live disk-usage feeds the hero's right-side ENERGY CORE bar via
  // the existing `host.metrics.tick` SSE stream (5s cadence). Catalog /
  // installed / installing counters land in BS.6 / BS.7 — they default
  // to zero here so the hero renders cleanly today.
  const { latest: hostTick, baseline: hostBaseline } = useHostMetricsTick()
  const heroCounters = useMemo<PlatformCounters>(() => {
    const diskUsedGb = hostTick?.host.disk_used_gb ?? 0
    const diskTotalGb =
      hostTick?.host.disk_total_gb ?? hostBaseline?.disk_total_gb ?? 0
    return {
      ...PLATFORM_COUNTERS_ZERO,
      diskUsedGb,
      diskTotalGb,
    }
  }, [hostTick, hostBaseline])

  // BS.7.1 — install button. Click flows directly to the existing
  // ``POST /installer/jobs`` route (backend/routers/installer.py:409),
  // which evaluates the request through the existing R20-A PEP gateway
  // HOLD path: ``tool="install_entry"`` is not on any tier whitelist so
  // ``classify`` returns HOLD via the ``tier_unlisted`` rule, and the
  // request blocks until the operator approves / rejects via the
  // global ToastCenter coaching card. The request resolves with the
  // queued ``install_jobs`` row on approve, throws on deny / timeout —
  // both paths are surfaced to the operator via the existing API
  // error toast / decision_resolved SSE chain. The catalog card flips
  // off its "pending tooltip" affordance the moment ``onInstall`` is
  // wired, so this single line activates the BS.6.7 install + update
  // affordance everywhere it is rendered.
  //
  // The catalog detail panel uses the same handler — BS.7 retry / view-
  // log are deferred to BS.7.6, which will plumb dedicated
  // ``onRetry`` / ``onViewLog`` handlers (and the failed-state log
  // modal) once the install pipeline lands.
  const handleInstall = useCallback(async (entry: CatalogEntry) => {
    try {
      await createInstallJob(entry.id)
    } catch (err) {
      // The request layer already surfaces the failure through the
      // global ApiErrorToastCenter; we still log so dev consoles see
      // the precise rejection reason (PEP deny vs timeout vs idempotency
      // collision) without scraping the toast DOM.
      console.error("[platforms] install job creation failed", err)
    }
  }, [])

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="platforms-settings-page"
      data-active-tab={tab}
    >
      <div className="mx-auto max-w-6xl">
        {/* ── Breadcrumb + heading ─────────────────────────────────── */}
        <header className="mb-6">
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
            <span className="text-[var(--foreground)]">platforms</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <Layers size={20} />
            Platforms
          </h1>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            管理 OmniSight 支援的 vertical / SDK / runtime / BSP entries。
            Hero panel、catalog cards、orbital diagram 將於 BS.5.2-BS.5.4 進駐。
          </p>
        </header>

        {/* ── Hero panel (BS.5.2) ─────────────────────────────────── */}
        <div className="mb-6">
          <PlatformHero counters={heroCounters} />
        </div>

        {/* ── Sub-tab nav ──────────────────────────────────────────── */}
        <nav
          className="mb-4 flex items-center gap-1 border-b border-[var(--border)]"
          aria-label="Platforms sub-tabs"
          role="tablist"
          data-testid="platforms-tabs-nav"
        >
          {PLATFORMS_TABS.map((id) => {
            const t = TAB_META[id]
            const Icon = t.icon
            const active = tab === id
            return (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={active}
                aria-controls={`platforms-panel-${id}`}
                onClick={() => onSelectTab(id)}
                className={[
                  "-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 font-mono text-xs transition-colors",
                  active
                    ? "border-[var(--neural-blue)] text-[var(--foreground)]"
                    : "border-transparent text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
                ].join(" ")}
                data-testid={`platforms-tab-${id}`}
              >
                <Icon size={12} />
                {t.label}
              </button>
            )
          })}
        </nav>

        {/* ── Active panel ────────────────────────────────────────── */}
        <section
          role="tabpanel"
          id={`platforms-panel-${tab}`}
          aria-labelledby={`platforms-tab-${tab}`}
          data-testid={`platforms-panel-${tab}`}
          className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-6"
        >
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <PanelIcon size={14} />
            {panelMeta.label}
          </div>
          {tab === "catalog" ? (
            // BS.6.1 — catalog toolbar shell. Entries default to empty
            // until BS.6.5's `useCatalog()` hook lands; the toolbar
            // (filter / search / sort / density) renders unconditionally
            // so operators see the surface they'll be using once the
            // data hook plumbs real entries through. BS.6.2 wires the
            // polished 5-state `<CatalogCard />` here via `renderCard`
            // so any entries that flow through (BS.6.5 hook, future
            // demo / dev seed) immediately get the polished treatment.
            <CatalogTab
              renderCard={({
                entry,
                density,
                cardPaddingClass,
                floatVariantIndex,
                onSelect,
              }) => (
                <CatalogCard
                  entry={entry}
                  density={density}
                  cardPaddingClass={cardPaddingClass}
                  // BS.6.6 — stable per-position float variant cycling
                  // (a/b/c/d) so adjacent cards land on different idle-
                  // drift keyframe phases without the catalog growing a
                  // shared counter.
                  floatVariantIndex={floatVariantIndex}
                  // BS.6.3 — propagate the tab's selection callback so
                  // a card click flips `<CatalogTab />`'s selection
                  // state and the detail panel slides in. Without
                  // `onSelect` the card is non-interactive (BS.6.2
                  // standalone preview behaviour).
                  onSelect={onSelect ? () => onSelect() : undefined}
                  // BS.7.1 — wire the install button. The card's
                  // BS.6.7 PendingInstallTooltip flips to its
                  // passthrough branch (no wrapper span, no tab stop,
                  // no portal mount) once the handler is non-undefined.
                  onInstall={handleInstall}
                />
              )}
              renderDetail={({ entry, onClose }) => (
                <CatalogDetailPanel
                  entry={entry}
                  onBack={onClose}
                  // BS.7.1 — same handler powers the detail panel's
                  // primary CTA (Install / Update). Retry + view-log
                  // remain pending behind BS.7.6 + BS.7.8.
                  onInstall={handleInstall}
                />
              )}
            />
          ) : (
            <>
              <p className="text-xs text-[var(--muted-foreground)]">
                {panelMeta.description}
              </p>
              <p className="mt-3 font-mono text-[10px] text-[var(--muted-foreground)]">
                （內容由後續 BS.6 / BS.7 row 進駐；本 row 僅 ship route shell
                + tab routing。）
              </p>
            </>
          )}
        </section>
      </div>
    </main>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Page export — Suspense wrapper required by Next.js 15 / React 19 for
// any component that calls `useSearchParams()`. The fallback is brief
// and matches the page surface so layout doesn't jump.
// ─────────────────────────────────────────────────────────────────────

export default function PlatformsSettingsPage() {
  return (
    <Suspense
      fallback={
        <main
          className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
          data-testid="platforms-settings-loading"
        >
          <div className="mx-auto max-w-6xl font-mono text-xs text-[var(--muted-foreground)]">
            Loading platforms…
          </div>
        </main>
      }
    >
      <PlatformsPageInner />
    </Suspense>
  )
}
