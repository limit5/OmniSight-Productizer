import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"
import { readFileSync } from "node:fs"
import createNextIntlPlugin from "next-intl/plugin"

const __dirname = dirname(fileURLToPath(import.meta.url))

// FX.7.11 — next-intl scaffolding. Points at `i18n/request.ts` which
// resolves the request-scoped locale (cookie-driven) and loads the
// matching `messages/<locale>.json` bundle. The plugin is a no-op for
// pages that don't call `useTranslations()` / `getTranslations()`, so
// adding it here is safe even before any component is migrated off the
// legacy `lib/i18n/context.tsx::useI18n()` API.
const withNextIntl = createNextIntlPlugin("./i18n/request.ts")

/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_URL || "http://localhost:8000"

// OP-1483 — bake the bundle id + FE-built-against-api version into
// `process.env.NEXT_PUBLIC_*` at build time so `lib/api.ts` can emit
// them as bookkeeping headers on every fetch (the runtime FE/BE compat
// detector). The source of truth is the checked-in `bundle.json` that
// `scripts/build_image_bundle.py` overwrites in CI; the placeholder
// values in the repo-root copy keep local builds working.
//
// Env precedence: explicit NEXT_PUBLIC_* from the CI shell wins (lets
// the deploy SOP override without rewriting bundle.json), then
// bundle.json if present, then "unknown"/"v1" as the final fallback —
// same shape Dockerfile.frontend's `ARG BUNDLE_ID=unknown` uses.
function _resolveBuildManifest() {
  const fromEnv = (process.env.NEXT_PUBLIC_BUNDLE_ID || "").trim()
  const contractFromEnv = (process.env.NEXT_PUBLIC_API_CONTRACT || "").trim()
  let bundleId = fromEnv
  let apiContract = contractFromEnv
  if (!bundleId || !apiContract) {
    try {
      const raw = readFileSync(join(__dirname, "bundle.json"), "utf-8")
      const parsed = JSON.parse(raw)
      if (!bundleId && typeof parsed.bundle_id === "string") {
        bundleId = parsed.bundle_id
      }
      if (
        !apiContract
        && parsed.contracts
        && typeof parsed.contracts.frontend_built_against_api === "string"
      ) {
        apiContract = parsed.contracts.frontend_built_against_api
      }
    } catch {
      // bundle.json may be absent in `next dev` standalone usage — fall
      // through to the defaults below.
    }
  }
  return {
    bundleId: bundleId || "unknown",
    apiContract: apiContract || "v1",
  }
}

const _buildManifest = _resolveBuildManifest()

const nextConfig = {
  output: "standalone",
  typescript: {
    // P0.1 (audit 2026-04-27): flipped from `true` to `false`. Earlier
    // setting allowed TS errors to ship to production — confirmed by
    // commit c881bedf (PromptVersionDrawer broken-bundle ship saga: a
    // TS2304 "Cannot find name 'drawer'" was raised by tsc but Next.js
    // ignored it and shipped the bundle anyway; operator only saw the
    // damage when clicking the launcher did nothing).
    //
    // Hard-fail on TS errors at build time. The cost: any pre-existing
    // type drift now blocks deploys until fixed. The benefit: silent
    // shipping of broken bundles becomes impossible. The deploy SOP
    // (docs/operations/deployment.md) also adds an explicit
    // `npx tsc --noEmit` gate so the error surfaces *before* `docker
    // compose build frontend`, not after.
    ignoreBuildErrors: false,
  },
  images: {
    unoptimized: true,
  },
  turbopack: {
    root: __dirname,
  },
  // OP-1483 — re-export the resolved manifest as NEXT_PUBLIC_* so the
  // client bundle can read the values via `process.env.*` at runtime.
  // Next.js inlines `NEXT_PUBLIC_*` env vars into the client bundle at
  // build time; this `env` block is the supported way to seed those
  // values from build-side computation.
  env: {
    NEXT_PUBLIC_BUNDLE_ID: _buildManifest.bundleId,
    NEXT_PUBLIC_API_CONTRACT: _buildManifest.apiContract,
  },
  async rewrites() {
    return [
      {
        // Proxy all /api/v1/* requests to the Python backend
        source: "/api/v1/:path*",
        destination: `${backendUrl}/api/v1/:path*`,
      },
    ]
  },
}

export default withNextIntl(nextConfig)
