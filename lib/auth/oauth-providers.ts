/**
 * AS.7.1 — Login page OAuth provider catalog.
 *
 * Pure data + URL builder for the "5+1 OAuth buttons" surface
 * (圓形 provider energy spheres + brand 色 halo). The login page
 * renders the 5 primary providers as round energy spheres, and an
 * extra "More" expand button reveals the 6+ remaining vendors that
 * the AS.6.1 backend supports (`backend/security/oauth_vendors.py`
 * defines 11 total: github / google / microsoft / apple / gitlab /
 * bitbucket / slack / notion / salesforce / hubspot / discord).
 *
 * The 5 primary picks (Google / GitHub / Microsoft / Apple /
 * Discord) match the consumer-facing IdPs most users actually have
 * an account on; everyone else lives behind the More dropdown.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 *   - All exports are `as const` frozen object literals + pure
 *     functions. No module-level mutable container.
 *   - `buildOAuthAuthorizeUrl()` derives the redirect URL from
 *     the function arguments only — no env reads, no DOM access,
 *     no React state. SSR / browser / vitest see identical output
 *     for identical input. Answer #1 of the SOP §1 audit
 *     (deterministic-by-construction across workers / tabs).
 *
 * Read-after-write timing audit: N/A — pure value module.
 */

/** Provider identifiers — byte-equal to the AS.1.3 vendor catalog
 *  slugs (`backend/security/oauth_vendors.py`). The drift guard test
 *  pins this set against a hard-coded list; if a vendor is added /
 *  removed the test fails and forces both surfaces to update in
 *  lockstep. */
export const OAUTH_PROVIDER_IDS = [
  "google",
  "github",
  "microsoft",
  "apple",
  "discord",
  "gitlab",
  "bitbucket",
  "slack",
  "notion",
  "salesforce",
  "hubspot",
] as const

export type OAuthProviderId = (typeof OAUTH_PROVIDER_IDS)[number]

/** Visual catalog row for one provider button. Frozen `as const`
 *  so mutation at runtime is a TS error.
 *
 *  Field invariants (pinned by `oauth-providers.test.ts`):
 *    - `id` matches one of `OAUTH_PROVIDER_IDS`
 *    - `displayName` is human-facing copy (used as `aria-label`)
 *    - `brandColor` is a hex string the energy-sphere CSS halo uses
 *    - `haloColor` may differ for contrast against the dark BG
 *    - `tier === "primary"` for the 5 main spheres,
 *      `tier === "secondary"` for the More dropdown
 */
export interface OAuthProviderInfo {
  readonly id: OAuthProviderId
  readonly displayName: string
  readonly brandColor: string
  readonly haloColor: string
  readonly tier: "primary" | "secondary"
}

const _CATALOG: readonly OAuthProviderInfo[] = Object.freeze([
  // ── Primary 5 ────────────────────────────────────────────────
  {
    id: "google",
    displayName: "Google",
    brandColor: "#4285F4",
    haloColor: "rgba(66, 133, 244, 0.55)",
    tier: "primary",
  },
  {
    id: "github",
    displayName: "GitHub",
    brandColor: "#E6EDF3",
    haloColor: "rgba(230, 237, 243, 0.55)",
    tier: "primary",
  },
  {
    id: "microsoft",
    displayName: "Microsoft",
    brandColor: "#00A4EF",
    haloColor: "rgba(0, 164, 239, 0.55)",
    tier: "primary",
  },
  {
    id: "apple",
    displayName: "Apple",
    brandColor: "#F5F5F7",
    haloColor: "rgba(245, 245, 247, 0.55)",
    tier: "primary",
  },
  {
    id: "discord",
    displayName: "Discord",
    brandColor: "#5865F2",
    haloColor: "rgba(88, 101, 242, 0.55)",
    tier: "primary",
  },
  // ── Secondary 6 (More dropdown) ──────────────────────────────
  {
    id: "gitlab",
    displayName: "GitLab",
    brandColor: "#FC6D26",
    haloColor: "rgba(252, 109, 38, 0.45)",
    tier: "secondary",
  },
  {
    id: "bitbucket",
    displayName: "Bitbucket",
    brandColor: "#2684FF",
    haloColor: "rgba(38, 132, 255, 0.45)",
    tier: "secondary",
  },
  {
    id: "slack",
    displayName: "Slack",
    brandColor: "#4A154B",
    haloColor: "rgba(74, 21, 75, 0.45)",
    tier: "secondary",
  },
  {
    id: "notion",
    displayName: "Notion",
    brandColor: "#E2E2E2",
    haloColor: "rgba(226, 226, 226, 0.45)",
    tier: "secondary",
  },
  {
    id: "salesforce",
    displayName: "Salesforce",
    brandColor: "#00A1E0",
    haloColor: "rgba(0, 161, 224, 0.45)",
    tier: "secondary",
  },
  {
    id: "hubspot",
    displayName: "HubSpot",
    brandColor: "#FF7A59",
    haloColor: "rgba(255, 122, 89, 0.45)",
    tier: "secondary",
  },
] as const)

export const OAUTH_PROVIDER_CATALOG: readonly OAuthProviderInfo[] = _CATALOG

/** Return the 5 primary providers in the exact visual order the
 *  login page renders them (left → right). */
export function getPrimaryProviders(): readonly OAuthProviderInfo[] {
  return _CATALOG.filter((p) => p.tier === "primary")
}

/** Return the secondary providers behind the More dropdown. */
export function getSecondaryProviders(): readonly OAuthProviderInfo[] {
  return _CATALOG.filter((p) => p.tier === "secondary")
}

/** Lookup a provider by id; throws on unknown so a typo at the
 *  callsite is a loud error rather than a silent missing button. */
export function getProvider(id: OAuthProviderId): OAuthProviderInfo {
  const found = _CATALOG.find((p) => p.id === id)
  if (!found) {
    throw new Error(`unknown oauth provider id: ${JSON.stringify(id)}`)
  }
  return found
}

/** AS.6.1 backend route shape — `GET /api/v1/auth/oauth/{provider}/authorize`.
 *  Returning a fully-qualified path lets the page use `<a href>` so
 *  the browser does the 302 redirect natively (the backend cookie
 *  set on the response needs the round-trip, fetch() with redirect:
 *  follow strips set-cookie). */
export const OAUTH_AUTHORIZE_PATH_PREFIX = "/api/v1/auth/oauth/"

/** Build the absolute URL the OAuth button hrefs to. The `next`
 *  parameter is forwarded to the backend so the post-callback
 *  redirect lands on the original intended destination instead
 *  of the dashboard root. */
export function buildOAuthAuthorizeUrl(
  providerId: OAuthProviderId,
  next?: string,
): string {
  const base = `${OAUTH_AUTHORIZE_PATH_PREFIX}${encodeURIComponent(providerId)}/authorize`
  if (!next || next === "/") return base
  return `${base}?next=${encodeURIComponent(next)}`
}
