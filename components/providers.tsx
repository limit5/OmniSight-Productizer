"use client"

import { useCallback } from "react"

import { I18nProvider } from "@/lib/i18n/context"
import { AuthProvider } from "@/lib/auth-context"
import { TenantProvider } from "@/lib/tenant-context"
import { ProjectProvider } from "@/lib/project-context"
import { StorageBridge } from "@/components/storage-bridge"
import { ApiErrorToastCenter } from "@/components/omnisight/api-error-toast-center"
import { Conflict409ToastCenter } from "@/components/omnisight/conflict-409-toast-center"
import { DraftSyncToastCenter } from "@/components/omnisight/draft-sync-toast-center"
import { InstallProgressDrawer } from "@/components/omnisight/install-progress-drawer"
import { useInstallJobs } from "@/hooks/use-install-jobs"
import { cancelInstallJob } from "@/lib/api"

interface ProvidersProps {
  children: React.ReactNode
}

// BS.7.4: thin wrapper that calls ``useInstallJobs()`` — keeps the
// SSE subscription scoped to a single mount inside <Providers/> so the
// drawer's ``jobs`` array tracks live install activity. Split out as
// its own component so the rest of <Providers/> stays a static tree
// (no extra hook call inside the top-level provider).
//
// BS.7.7: wires the drawer's per-row cancel button. The handler runs
// the OPTIMISTIC + CONFIRM pattern:
//
//   1. Optimistic — call ``removeJob(jobId)`` so the row drops from
//      this hook instance's local state immediately (drawer chip /
//      panel re-render hides the row before the network call returns).
//   2. POST — fire ``cancelInstallJob(jobId)`` against the backend.
//      Failures (404 / 409 / 403) are surfaced through the global
//      ``<ApiErrorToastCenter />``; we only log so dev consoles see
//      the precise rejection reason. No rollback because the SSE
//      stream is the source of truth — if the backend says the row
//      is already terminal (409), the next ``installer_progress`` tick
//      either re-adds it as ``running`` (cancel raced and lost) or
//      stays absent (already cancelled / completed).
//   3. Confirm — backend ``cancel_job`` emits ``installer_progress``
//      with ``state="cancelled" stage="cancel"`` immediately after the
//      DB UPDATE; that broadcast lands on every connected hook in the
//      tenant (including the catalog page's separate ``useInstallJobs``
//      mount, which doesn't see the optimistic ``removeJob``). The
//      catalog card's ``deriveCatalogStateFromInstallJob`` (BS.7.5)
//      maps the ``cancelled`` row back to the entry's static
//      ``installState`` fallback so an aborted install reverts to
//      ``available`` / ``update-available`` cleanly.
function InstallProgressDrawerLive() {
  const { jobs, removeJob } = useInstallJobs()
  const handleCancel = useCallback(
    (jobId: string) => {
      removeJob(jobId)
      void cancelInstallJob(jobId).catch((err) => {
        console.error("[providers] install cancel failed", err)
      })
    },
    [removeJob],
  )
  return <InstallProgressDrawer jobs={jobs} onCancel={handleCancel} />
}

export function Providers({ children }: ProvidersProps) {
  return (
    <I18nProvider>
      <AuthProvider>
        <TenantProvider>
          <ProjectProvider>
            <StorageBridge />
            {children}
            <ApiErrorToastCenter />
            <Conflict409ToastCenter />
            <DraftSyncToastCenter />
            {/* BS.7.4: bottom-right install-progress drawer wired to
             *  the live ``installer_progress`` SSE channel via
             *  ``useInstallJobs()``. The drawer filters in-flight
             *  states internally so terminal jobs drop off without
             *  this provider having to GC them. */}
            <InstallProgressDrawerLive />
          </ProjectProvider>
        </TenantProvider>
      </AuthProvider>
    </I18nProvider>
  )
}
