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

interface ProvidersProps {
  children: React.ReactNode
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
            {/* BS.7.3: bottom-right install-progress drawer.
             *  Mounted with the default empty `jobs` array so it stays
             *  invisible until BS.7.4's `use-install-jobs` hook lands;
             *  once that ships this <InstallProgressDrawer /> just
             *  receives `jobs={useInstallJobs().jobs}` here. */}
            <InstallProgressDrawer />
          </ProjectProvider>
        </TenantProvider>
      </AuthProvider>
    </I18nProvider>
  )
}
