"use client"

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

interface ProvidersProps {
  children: React.ReactNode
}

// BS.7.4: thin wrapper that calls ``useInstallJobs()`` — keeps the
// SSE subscription scoped to a single mount inside <Providers/> so the
// drawer's ``jobs`` array tracks live install activity. Split out as
// its own component so the rest of <Providers/> stays a static tree
// (no extra hook call inside the top-level provider).
function InstallProgressDrawerLive() {
  const { jobs } = useInstallJobs()
  return <InstallProgressDrawer jobs={jobs} />
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
