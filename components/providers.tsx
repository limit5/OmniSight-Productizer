"use client"

import { I18nProvider } from "@/lib/i18n/context"
import { AuthProvider } from "@/lib/auth-context"

interface ProvidersProps {
  children: React.ReactNode
}

export function Providers({ children }: ProvidersProps) {
  // AuthProvider sits inside I18n so login forms can also render
  // localised copy. Both are top-level so any page in the app can
  // call useAuth() / useI18n() without re-mounting the context.
  return (
    <I18nProvider>
      <AuthProvider>
        {children}
      </AuthProvider>
    </I18nProvider>
  )
}
