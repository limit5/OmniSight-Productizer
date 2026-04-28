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

// AS.7.1 — Login page primitives
export { AuthFieldElectric } from "./auth-field-electric"
export { OAuthEnergySphere } from "./oauth-energy-sphere"
export { OAuthProviderIcon } from "./oauth-provider-icons"
export { AuthHoneypotField } from "./auth-honeypot-field"
export { AuthTurnstileWidget } from "./auth-turnstile-widget"
export { AccountLockedOverlay } from "./account-locked-overlay"
export {
  WARP_DURATION_BY_LEVEL,
  WarpDriveTransition,
} from "./warp-drive-transition"

// AS.7.2 — Signup page primitives
export { PasswordSlotMachine } from "./password-slot-machine"
export {
  PASSWORD_STYLE_OPTIONS,
  PasswordStyleToggle,
} from "./password-style-toggle"
export { PasswordStrengthMeter } from "./password-strength-meter"
export { SaveAcknowledgementCheckbox } from "./save-acknowledgement-checkbox"
