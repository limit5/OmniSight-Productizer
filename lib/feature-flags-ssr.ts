import {
  DEFAULT_EFFECTIVE_FEATURE_FLAGS,
  normalizeEffectiveFeatureFlags,
  type EffectiveFeatureFlags,
  type EffectiveFeatureFlagsResponse,
} from "@/lib/api"

interface HeaderReader {
  get(name: string): string | null
}

export function resolveServerApiBase(headersList: HeaderReader): string | null {
  // Server-only internal base first: inside the frontend container the public
  // host:port (from the host header) has no listener, so deriving the API base
  // from it fails ECONNREFUSED. BACKEND_URL (e.g. http://caddy, injected at
  // runtime by deploy compose per OP-1725) is reachable from this container.
  // BACKEND_URL is server-only and must never be exposed to the browser.
  const internal = process.env.BACKEND_URL
  if (internal) return `${internal.replace(/\/+$/, "")}/api/v1`
  const configured = process.env.NEXT_PUBLIC_API_URL
  if (configured) return `${configured.replace(/\/+$/, "")}/api/v1`
  const host = headersList.get("host")
  if (!host) return null
  const proto = headersList.get("x-forwarded-proto") ?? "http"
  return `${proto}://${host}/api/v1`
}

export async function loadInitialEffectiveFeatureFlags(
  headersList: HeaderReader,
): Promise<EffectiveFeatureFlags> {
  const base = resolveServerApiBase(headersList)
  if (!base) return { ...DEFAULT_EFFECTIVE_FEATURE_FLAGS }

  try {
    const res = await fetch(`${base}/feature-flags/effective`, {
      cache: "no-store",
      headers: {
        cookie: headersList.get("cookie") ?? "",
        "x-tenant-id": headersList.get("x-tenant-id") ?? "",
      },
    })
    if (!res.ok) return { ...DEFAULT_EFFECTIVE_FEATURE_FLAGS }
    const payload = (await res.json()) as EffectiveFeatureFlagsResponse
    return normalizeEffectiveFeatureFlags(payload)
  } catch (exc) {
    console.warn("[feature-flags] SSR bootstrap failed closed", exc)
    return { ...DEFAULT_EFFECTIVE_FEATURE_FLAGS }
  }
}
