import { fileURLToPath } from "node:url"
import { dirname } from "node:path"

const __dirname = dirname(fileURLToPath(import.meta.url))

/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_URL || "http://localhost:8000"

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

export default nextConfig
