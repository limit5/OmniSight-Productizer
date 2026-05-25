"use client"

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react"

import {
  DEFAULT_EFFECTIVE_FEATURE_FLAGS,
  fetchEffectiveFeatureFlags,
  normalizeEffectiveFeatureFlags,
  type EffectiveFeatureFlags,
  type PublicEffectiveFeatureFlag,
} from "@/lib/api"

interface FeatureFlagsContextValue {
  flags: EffectiveFeatureFlags
  loading: boolean
  isEnabled: (name: PublicEffectiveFeatureFlag) => boolean
  refresh: () => Promise<void>
}

const FeatureFlagsContext = createContext<FeatureFlagsContextValue | null>(null)

export interface FeatureFlagsProviderProps {
  children: React.ReactNode
  initialFlags?: EffectiveFeatureFlags | null
}

export function FeatureFlagsProvider({
  children,
  initialFlags = DEFAULT_EFFECTIVE_FEATURE_FLAGS,
}: FeatureFlagsProviderProps) {
  const [flags, setFlags] = useState<EffectiveFeatureFlags>(() =>
    normalizeEffectiveFeatureFlags({ flags: initialFlags ?? undefined }),
  )
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setFlags(await fetchEffectiveFeatureFlags())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh() // eslint-disable-line react-hooks/set-state-in-effect -- fetch-on-mount refreshes SSR bootstrap flags
  }, [refresh])

  const isEnabled = useCallback(
    (name: PublicEffectiveFeatureFlag) => flags[name] === true,
    [flags],
  )

  const value = useMemo(
    () => ({ flags, loading, isEnabled, refresh }),
    [flags, isEnabled, loading, refresh],
  )

  return (
    <FeatureFlagsContext.Provider value={value}>
      {children}
    </FeatureFlagsContext.Provider>
  )
}

export function useFeatureFlags(): FeatureFlagsContextValue {
  const value = useContext(FeatureFlagsContext)
  if (!value) {
    throw new Error("useFeatureFlags must be used inside <FeatureFlagsProvider>")
  }
  return value
}

export function useFeatureFlag(name: PublicEffectiveFeatureFlag): boolean {
  return useFeatureFlags().isEnabled(name)
}

// OP-1724: a non-throwing variant for ubiquitous low-level primitives
// (e.g. <Block/>) that may be rendered in isolation outside the app's
// <FeatureFlagsProvider> (unit tests, isolated stories). When no provider
// is mounted it resolves to the all-dark DEFAULT_EFFECTIVE_FEATURE_FLAGS
// posture (fail-closed / default-OFF) instead of crashing the subtree.
// Top-level feature gates should keep using useFeatureFlag / FeatureGate,
// which throw to flag a missing provider.
export function useFeatureFlagOrDark(name: PublicEffectiveFeatureFlag): boolean {
  const value = useContext(FeatureFlagsContext)
  if (!value) return DEFAULT_EFFECTIVE_FEATURE_FLAGS[name] === true
  return value.isEnabled(name)
}

export interface FeatureGateProps {
  flag: PublicEffectiveFeatureFlag
  children: React.ReactNode
  fallback?: React.ReactNode
}

export function FeatureGate({
  flag,
  children,
  fallback = null,
}: FeatureGateProps) {
  return useFeatureFlag(flag) ? <>{children}</> : <>{fallback}</>
}
