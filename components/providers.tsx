"use client"

import { I18nProvider } from "@/lib/i18n/context"
import { AuthProvider } from "@/lib/auth-context"
import { TenantProvider } from "@/lib/tenant-context"
import { StorageBridge } from "@/components/storage-bridge"

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
        </TenantProvider>
      </AuthProvider>
    </I18nProvider>
  )
}
