"use client"

import { I18nProvider } from "@/lib/i18n/context"
import { AuthProvider } from "@/lib/auth-context"
import { TenantProvider } from "@/lib/tenant-context"
import { StorageBridge } from "@/components/storage-bridge"
import { ApiErrorToastCenter } from "@/components/omnisight/api-error-toast-center"

interface ProvidersProps {
  children: React.ReactNode
}

export function Providers({ children }: ProvidersProps) {
  return (
    <I18nProvider>
      <AuthProvider>
        <TenantProvider>
          <StorageBridge />
          {children}
          <ApiErrorToastCenter />
        </TenantProvider>
      </AuthProvider>
    </I18nProvider>
  )
}
