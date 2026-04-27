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
 * BS.7.5 — live install state on the catalog card (this file owns the
 * SSE → card state-3 wiring). Once the PEP gate clears and the sidecar
 * starts the download, ``installer_progress`` SSE events flow into
 * ``useInstallJobs()``; the card's ``installState`` is overwritten by
 * the derived value (queued / running → ``installing``, completed →
 * ``installed``, failed → ``failed``) and ``installProgressPercent`` is
 * driven by ``bytes_done / bytes_total``. The card's BS.6.2
 * conic-gradient ring + ``ring-spin`` icon + bytes-counter live read
 * out activate without any additional plumbing — the card already
 * paints state 3 from these props. Cancelled jobs revert to the
 * entry's static ``installState`` so an aborted install does not
 * clobber an ``update-available`` chip.
 *
 * BS.7.6 — failed-state retry + view-log handlers. When the install
 * pipeline lands an ``installer_progress`` SSE event with
 * ``state="failed"``, the catalog card flips to its critical-red state
 * 5 visual and exposes two operator affordances: retry (clones the
 * source row through the same R20-A PEP gateway HOLD via ``POST
 * /installer/jobs/{id}/retry``) and view-log (opens the local
 * `<InstallLogModal />` showing the row's ``log_tail`` + ``error_-
 * reason``). The modal is mounted at the page root so it overlays the
 * detail panel + drawer; the operator can read the post-mortem and
 * re-trigger the install in one place without navigating away.
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

import { Suspense, useCallback, useMemo, useState } from "react"
import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import {
  ArrowLeft,
  Boxes,
  CheckCircle2,
  ChevronRight,
  Layers,
  Rss,
  Trash2,
} from "lucide-react"

import { CatalogCard } from "@/components/omnisight/catalog-card"
import { CatalogDetailPanel } from "@/components/omnisight/catalog-detail-panel"
import {
  CatalogTab,
  type CatalogEntry,
} from "@/components/omnisight/catalog-tab"
import { CleanupUnusedModal, pickCleanupCandidates } from "@/components/omnisight/cleanup-unused-modal"
import { InstallLogModal } from "@/components/omnisight/install-log-modal"
import {
  InstalledTab,
} from "@/components/omnisight/installed-tab"
import {
  PLATFORM_COUNTERS_ZERO,
  PlatformHero,
  type PlatformCounters,
} from "@/components/omnisight/platform-hero"
import { useHostMetricsTick } from "@/hooks/use-host-metrics-tick"
import {
  deriveCatalogProgressPercent,
  deriveCatalogStateFromInstallJob,
  pickInstallJobForEntry,
  useInstallJobs,
} from "@/hooks/use-install-jobs"
import { useInstalledEntries } from "@/hooks/use-installed-entries"
import {
  createInstallJob,
  getInstallJob,
  retryInstallJob,
  type InstallJob,
} from "@/lib/api"

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

  // BS.7.5 — live install state from SSE. The same ``useInstallJobs()``
  // hook that powers the bottom-right ``<InstallProgressDrawer />``
  // (mounted in ``components/providers.tsx``) feeds the catalog cards
  // here so a job's lifecycle ticks (queued → running → completed /
  // failed / cancelled) are reflected on the card visual without an
  // extra round-trip. ``pickInstallJobForEntry`` matches the freshest
  // job per ``entry_id`` (preferring in-flight over terminal so a
  // retry-while-running scenario shows the active install rather than
  // the old failure), and ``deriveCatalogStateFromInstallJob`` maps
  // backend lifecycle → catalog 5-state visual. Cancelled rows revert
  // to the entry's seeded ``installState`` so an ``update-available``
  // chip is preserved across an aborted install.
  //
  // The hook subscribes once per mount; calling it here in addition to
  // the drawer-side mount is intentional — ``api.subscribeEvents``
  // shares a single ``EventSource`` per tab and only registers an extra
  // listener callback, so the cost is one extra ``InstallJob[]`` array
  // and one extra listener (negligible). When BS.7.6/7.7 land we may
  // share state via context; for this row the duplicate listener is
  // the simplest scope-minimal wiring.
  const { jobs: installJobs } = useInstallJobs()
  const renderCardOverlay = useCallback(
    (entry: CatalogEntry): {
      installState: CatalogEntry["installState"]
      installProgressPercent: number | undefined
    } => {
      const fallback = entry.installState ?? "available"
      const job = pickInstallJobForEntry(installJobs, entry.id)
      const installState = deriveCatalogStateFromInstallJob(job, fallback)
      const installProgressPercent = deriveCatalogProgressPercent(job)
      return { installState, installProgressPercent }
    },
    [installJobs],
  )

  // BS.7.6 — failed-state retry + view-log handlers.
  //
  // Retry path mirrors handleInstall: pick the most-recent install job
  // for the entry (any terminal state — failed / cancelled / completed),
  // call ``POST /installer/jobs/{id}/retry`` to clone it into a fresh
  // queued row. The retry endpoint itself runs through the same R20-A
  // PEP gateway HOLD path, so the operator gets a fresh coaching card
  // before the install actually starts. Failures (404 source row gone /
  // 409 source still active / 403 PEP deny / 408 timeout) are surfaced
  // by the global ``<ApiErrorToastCenter />`` — we just log so dev
  // consoles see the precise rejection reason.
  //
  // View-log path opens the local modal with the freshest InstallJob
  // for the entry. When the SSE snapshot already has the row (typical:
  // operator just watched the install fail) we render immediately. When
  // the operator opens the page after the failure has already been
  // streamed off, we fetch a fresh row via ``getInstallJob`` so the
  // log_tail and error_reason are populated. The modal is closed by
  // setting ``logModalJob`` back to null.
  const [logModalJob, setLogModalJob] = useState<InstallJob | null>(null)
  const [logModalEntryName, setLogModalEntryName] = useState<string | undefined>(undefined)

  const handleRetry = useCallback(
    async (entry: CatalogEntry) => {
      const job = pickInstallJobForEntry(installJobs, entry.id)
      if (!job) {
        // No row in the SSE snapshot — fall back to a fresh create.
        // This covers the page-loaded-after-failure path where the
        // entry is still flagged as ``failed`` from a feed snapshot
        // but the install_jobs row is no longer in local state.
        try {
          await createInstallJob(entry.id)
        } catch (err) {
          console.error("[platforms] retry-as-create failed", err)
        }
        return
      }
      try {
        await retryInstallJob(job.id)
      } catch (err) {
        console.error("[platforms] install retry failed", err)
      }
    },
    [installJobs],
  )

  const handleViewLog = useCallback(
    async (entry: CatalogEntry) => {
      const job = pickInstallJobForEntry(installJobs, entry.id)
      if (job) {
        setLogModalJob(job)
        setLogModalEntryName(entry.displayName)
        return
      }
      // Page reloaded after the failure SSE has rolled off — try a
      // direct fetch by entry_id.metadata.lastInstallJobId if the
      // catalog feed exposed it; otherwise we cannot recover a tail.
      const lastJobId =
        typeof entry.metadata?.lastInstallJobId === "string"
          ? entry.metadata.lastInstallJobId
          : undefined
      if (!lastJobId) {
        // Surface a clear stub so the operator sees the modal opened
        // but knows there is no log to show. Future BS.8 history view
        // will provide a richer recall path.
        setLogModalJob({
          id: `${entry.id}-no-job`,
          tenant_id: "",
          entry_id: entry.id,
          state: "failed",
          idempotency_key: "",
          sidecar_id: null,
          protocol_version: 0,
          bytes_done: 0,
          bytes_total: null,
          eta_seconds: null,
          log_tail: "",
          result_json: null,
          error_reason: null,
          pep_decision_id: null,
          requested_by: "",
          queued_at: "",
          claimed_at: null,
          started_at: null,
          completed_at: null,
        })
        setLogModalEntryName(entry.displayName)
        return
      }
      try {
        const fetched = await getInstallJob(lastJobId)
        setLogModalJob(fetched)
        setLogModalEntryName(entry.displayName)
      } catch (err) {
        console.error("[platforms] install log fetch failed", err)
      }
    },
    [installJobs],
  )

  const handleCloseLogModal = useCallback(() => {
    setLogModalJob(null)
    setLogModalEntryName(undefined)
  }, [])

  // BS.8.2 — installed entries source. The hook fetches
  // `GET /installer/installed` on mount + on manual `refresh()`. The
  // refresh is fired after a successful bulk uninstall so the cleanup
  // modal's just-uninstalled rows fall out of the next render.
  const {
    entries: installedEntries,
    refresh: refreshInstalledEntries,
  } = useInstalledEntries()

  // BS.8.2 — cleanup-unused modal state. Toggled by the "Cleanup
  // unused" button in the InstalledTab toolbar; the modal owns its
  // own bulk-select state + uninstall round-trip. The badge count
  // reuses `pickCleanupCandidates` so the toolbar's "(N)" matches
  // exactly what the modal will render once opened.
  const [cleanupModalOpen, setCleanupModalOpen] = useState(false)
  const cleanupCandidateCount = useMemo(
    () => pickCleanupCandidates(installedEntries).length,
    [installedEntries],
  )
  const handleCleanupOpen = useCallback(() => {
    setCleanupModalOpen(true)
  }, [])
  const handleCleanupClose = useCallback(() => {
    setCleanupModalOpen(false)
  }, [])
  const handleCleanupCompleted = useCallback(() => {
    // After a successful bulk uninstall, refetch so the modal's
    // candidate list shrinks on the next open and the InstalledTab
    // drops the just-uninstalled rows.
    void refreshInstalledEntries()
  }, [refreshInstalledEntries])

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
          {tab === "installed" ? (
            // BS.8.1 / BS.8.2 — installed tab list view + cleanup-
            // unused entry point. ``installedEntries`` come from the
            // BS.8.2 ``useInstalledEntries()`` hook (`GET /installer/
            // installed`); the Cleanup-unused button mounts the
            // BS.8.2 modal which runs bulk uninstall through the
            // standard R20-A PEP HOLD path. ``onUpdate`` /
            // ``onReinstall`` reuse the BS.7.1 ``handleInstall`` path
            // so the same R20-A PEP gate covers both flows;
            // ``onViewLog`` opens the same modal the catalog card
            // uses; ``onUninstall`` (per-row) is still left unwired
            // until BS.8.4 lands the dependency-check gate.
            <div className="flex flex-col gap-3">
              <div
                className="flex items-center justify-end"
                data-testid="installed-tab-extras"
              >
                <button
                  type="button"
                  onClick={handleCleanupOpen}
                  disabled={cleanupCandidateCount === 0}
                  className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-2.5 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
                  data-testid="installed-tab-cleanup-button"
                  data-cleanup-candidate-count={cleanupCandidateCount}
                  aria-label={`Open cleanup-unused modal — ${cleanupCandidateCount} idle candidates`}
                >
                  <Trash2 size={12} aria-hidden />
                  Cleanup unused
                  {cleanupCandidateCount > 0 ? ` (${cleanupCandidateCount})` : ""}
                </button>
              </div>
              <InstalledTab
                entries={installedEntries}
                onViewLog={(entry) => {
                  handleViewLog({
                    id: entry.id,
                    displayName: entry.displayName,
                    vendor: entry.vendor,
                    family: entry.family,
                  })
                }}
                onUpdate={(entry) => {
                  void handleInstall({
                    id: entry.id,
                    displayName: entry.displayName,
                    vendor: entry.vendor,
                    family: entry.family,
                  })
                }}
                onReinstall={(entry) => {
                  void handleInstall({
                    id: entry.id,
                    displayName: entry.displayName,
                    vendor: entry.vendor,
                    family: entry.family,
                  })
                }}
              />
            </div>
          ) : tab === "catalog" ? (
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
              }) => {
                // BS.7.5 — splice the live SSE-derived install state
                // onto the entry. The catalog card already paints
                // ``entry.installState`` through its 5-state palette
                // and accepts ``installProgressPercent`` for the
                // installing-state conic-gradient ring; we just hand
                // it the freshest values the SSE feed has observed.
                // When no job is present, ``deriveCatalogStateFrom-
                // InstallJob`` returns the entry's static state
                // verbatim so the BS.6.2 visual is unchanged for
                // entries the operator hasn't touched.
                const { installState, installProgressPercent } =
                  renderCardOverlay(entry)
                const liveEntry =
                  installState !== entry.installState
                    ? { ...entry, installState }
                    : entry
                return (
                  <CatalogCard
                    entry={liveEntry}
                    density={density}
                    cardPaddingClass={cardPaddingClass}
                    // BS.6.6 — stable per-position float variant
                    // cycling (a/b/c/d) so adjacent cards land on
                    // different idle-drift keyframe phases without
                    // the catalog growing a shared counter.
                    floatVariantIndex={floatVariantIndex}
                    // BS.6.3 — propagate the tab's selection callback
                    // so a card click flips `<CatalogTab />`'s
                    // selection state and the detail panel slides in.
                    onSelect={onSelect ? () => onSelect() : undefined}
                    // BS.7.1 — wire the install button. The card's
                    // BS.6.7 PendingInstallTooltip flips to its
                    // passthrough branch (no wrapper span, no tab
                    // stop, no portal mount) once the handler is
                    // non-undefined.
                    onInstall={handleInstall}
                    // BS.7.6 — failed-state retry button calls the
                    // backend retry endpoint (clones the source row
                    // through the same PEP HOLD); view-log opens the
                    // local InstallLogModal showing the row's
                    // log_tail + error_reason.
                    onRetry={handleRetry}
                    onViewLog={handleViewLog}
                    // BS.7.5 — live SSE-derived progress percentage
                    // drives the installing-state conic-gradient
                    // border (state 3). Undefined when total bytes
                    // are unknown so the card falls back to its
                    // static "downloading…" hint.
                    installProgressPercent={installProgressPercent}
                  />
                )
              }}
              renderDetail={({ entry, onClose }) => {
                // BS.7.5 — same SSE-derived install state powers the
                // detail panel header chip / footer CTA so the panel
                // matches the card visual the operator clicked on.
                // The detail panel re-derives its own progress block
                // from ``entry.installState``; we splice the live
                // state onto the entry the same way as the card.
                const { installState } = renderCardOverlay(entry)
                const liveEntry =
                  installState !== entry.installState
                    ? { ...entry, installState }
                    : entry
                return (
                  <CatalogDetailPanel
                    entry={liveEntry}
                    onBack={onClose}
                    // BS.7.1 — same handler powers the detail panel's
                    // primary CTA (Install / Update).
                    onInstall={handleInstall}
                    // BS.7.6 — same retry / view-log handlers as the
                    // card so the operator can act from either
                    // surface.
                    onRetry={handleRetry}
                    onViewLog={handleViewLog}
                  />
                )
              }}
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

      {/* BS.7.6 — install log + retry modal. Opens when handleViewLog
          sets ``logModalJob``; closes via the modal's Close button or
          Esc / overlay click (handleCloseLogModal). The retry button
          inside the modal reuses ``handleRetry`` so the operator can
          read the tail and re-trigger the install in one place. */}
      <InstallLogModal
        job={logModalJob}
        entryDisplayName={logModalEntryName}
        onClose={handleCloseLogModal}
        onRetry={(job) => {
          // The retry handler keys off ``entry.id`` since the catalog
          // card / detail panel pass entries; rebuild a minimal entry
          // shape from the job's ``entry_id`` so we can reuse the
          // existing handleRetry path verbatim.
          handleRetry({
            id: job.entry_id || job.id,
            displayName: logModalEntryName ?? (job.entry_id || job.id),
            vendor: "",
            family: "custom",
          })
        }}
      />

      {/* BS.8.2 — Cleanup-unused modal. Opens when the operator clicks
          the InstalledTab "Cleanup unused" button. The modal owns its
          own bulk-select state and calls `bulkUninstallEntries` directly
          (BS.8.2 contract: still goes through PEP — `tool="uninstall_entry"`
          lands in `tier_unlisted` HOLD). On success, `onCompleted` fires
          `refreshInstalledEntries()` so the just-uninstalled rows fall
          out of the InstalledTab on the next render. */}
      <CleanupUnusedModal
        open={cleanupModalOpen}
        entries={installedEntries}
        onClose={handleCleanupClose}
        onCompleted={handleCleanupCompleted}
      />
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
