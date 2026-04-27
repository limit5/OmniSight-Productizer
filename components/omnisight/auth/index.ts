/**
 * AS.7.0 — Auth Visual Foundation component barrel.
 *
 * Pages import the four primitives (`AuthVisualFoundation`,
 * `AuthGlassCard`, `AuthBrandWordmark`, `AuthNebulaBackground`)
 * via this barrel. Pure helpers are re-exported from
 * `@/lib/auth-visual` instead — keep this file React-only so SSR
 * imports of motion-policy don't pull in the WebGL canvas.
 */

export { AuthVisualFoundation } from "./auth-visual-foundation"
export {
  AuthGlassCard,
  type AuthGlassCardHandle,
} from "./auth-glass-card"
export {
  AUTH_BRAND_BLOOM_DURATION_MS,
  AuthBrandWordmark,
} from "./auth-brand-wordmark"
export { AuthNebulaBackground } from "./auth-nebula-background"
