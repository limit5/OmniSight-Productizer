"use client"

import { I18nProvider } from "@/lib/i18n/context"

interface ProvidersProps {
  children: React.ReactNode
}

export function Providers({ children }: ProvidersProps) {
  return (
    <I18nProvider>
      {children}
    </I18nProvider>
  )
}
