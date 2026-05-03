import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// B13 Part A (#339): when the backend is in first-run state the bootstrap
// gate returns 503 `{ "error": "bootstrap_required" }` to every API path.
// The client-side `lib/api.ts` interceptor catches that on API calls, but
// a user who opens a protected page cold (no client state yet) would
// otherwise see the auth redirect to `/login` and get stuck trying to
// log in against an un-initialised system. This middleware probes the
// public `/bootstrap/status` endpoint server-side on page navigations and
// sends the user straight to the `/setup-required` landing page whenever
// bootstrap has not been finalised — even before any React code runs.
const BOOTSTRAP_STATUS_PATH = "/api/v1/bootstrap/status";
const SETUP_REQUIRED_PATH = "/setup-required";
const BOOTSTRAP_PROBE_TIMEOUT_MS = 1500;
// Small in-process cache so we don't fan out one backend probe per
// static asset / partial request. After bootstrap finalises it flips
// permanently, so a short TTL is plenty.
const BOOTSTRAP_CACHE_TTL_MS = 5_000;
const SECURITY_HEADER_DEFAULTS: Array<[string, string]> = [
  ["Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload"],
  ["X-Frame-Options", "DENY"],
  ["X-Content-Type-Options", "nosniff"],
  ["Referrer-Policy", "strict-origin"],
  ["Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"],
  ["Cross-Origin-Resource-Policy", "same-origin"],
  ["Cross-Origin-Embedder-Policy", "require-corp"],
  ["Cross-Origin-Opener-Policy", "same-origin"],
];
let _bootstrapCache: { finalized: boolean; expiresAt: number } | null = null;

function _resolveBackendUrl(): string {
  const raw = process.env.BACKEND_URL || "http://localhost:8000";
  return raw.replace(/\/+$/, "");
}

async function _probeBootstrapFinalized(): Promise<boolean | null> {
  const now = Date.now();
  if (_bootstrapCache && _bootstrapCache.expiresAt > now) {
    return _bootstrapCache.finalized;
  }
  try {
    const res = await fetch(`${_resolveBackendUrl()}${BOOTSTRAP_STATUS_PATH}`, {
      method: "GET",
      cache: "no-store",
      signal: AbortSignal.timeout(BOOTSTRAP_PROBE_TIMEOUT_MS),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { finalized?: boolean };
    const finalized = data?.finalized === true;
    _bootstrapCache = { finalized, expiresAt: now + BOOTSTRAP_CACHE_TTL_MS };
    return finalized;
  } catch {
    // Backend unreachable — fail open so the frontend still renders.
    return null;
  }
}

function _isBootstrapExemptPath(path: string): boolean {
  // Already on the setup landing page or the wizard itself — must not
  // redirect, otherwise we'd loop.
  if (path === SETUP_REQUIRED_PATH) return true;
  if (path === "/bootstrap" || path.startsWith("/bootstrap/")) return true;
  // Public error pages keep working even when bootstrap isn't done.
  return false;
}

export async function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}'`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self' https:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");

  const path = request.nextUrl.pathname;

  // B13 Part A: redirect to /setup-required when backend reports the
  // install wizard hasn't been finalised. This fires for page
  // navigations only (the matcher below already excludes /api, static
  // assets, and next-image), so the backend's own JSON-503 path stays
  // untouched and the wizard remains reachable.
  if (!_isBootstrapExemptPath(path)) {
    const finalized = await _probeBootstrapFinalized();
    if (finalized === false) {
      const url = request.nextUrl.clone();
      url.pathname = SETUP_REQUIRED_PATH;
      url.search = "";
      return NextResponse.redirect(url);
    }
  }

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  for (const [header, value] of SECURITY_HEADER_DEFAULTS) {
    response.headers.set(header, value);
  }
  return response;
}

export const config = {
  matcher: [
    { source: "/((?!api|_next/static|_next/image|favicon.ico|icon-.*\\.png|apple-icon\\.png|icon\\.svg).*)" },
  ],
};
