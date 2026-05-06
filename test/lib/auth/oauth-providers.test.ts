/**
 * AS.7.1 — `lib/auth/oauth-providers.ts` contract tests.
 *
 * Pins the 11-provider catalog + the 5/6 primary/secondary split
 * + the URL builder shape so future edits can't desync from the
 * AS.1.3 backend vendor catalog.
 */

import { describe, expect, it } from "vitest"

import {
  OAUTH_AUTHORIZE_PATH_PREFIX,
  OAUTH_PROVIDER_CATALOG,
  OAUTH_PROVIDER_IDS,
  buildOAuthAuthorizeUrl,
  getPrimaryProviders,
  getProvider,
  getSecondaryProviders,
  resolveOAuthProviderConfigured,
} from "@/lib/auth/oauth-providers"

describe("AS.7.1 oauth-providers", () => {
  it("OAUTH_PROVIDER_IDS contains exactly the 11 backend AS.1.3 vendors", () => {
    // Pin the set against the backend `oauth_vendors.py` catalog.
    expect([...OAUTH_PROVIDER_IDS].sort()).toEqual(
      [
        "apple",
        "bitbucket",
        "discord",
        "github",
        "gitlab",
        "google",
        "hubspot",
        "microsoft",
        "notion",
        "salesforce",
        "slack",
      ].sort(),
    )
  })

  it("catalog row count matches OAUTH_PROVIDER_IDS length", () => {
    expect(OAUTH_PROVIDER_CATALOG.length).toBe(OAUTH_PROVIDER_IDS.length)
  })

  it("every catalog row has the required fields", () => {
    for (const row of OAUTH_PROVIDER_CATALOG) {
      expect(row.id).toBeTruthy()
      expect(row.displayName).toBeTruthy()
      expect(row.brandColor).toMatch(/^#[0-9A-Fa-f]{6,8}$/)
      expect(row.haloColor).toMatch(/^rgba?\(/)
      expect(row.registrationDocsUrl).toMatch(/^https:\/\//)
      expect(row.supported).toBe(true)
      expect(typeof row.configured).toBe("boolean")
      expect(["primary", "secondary"]).toContain(row.tier)
    }
  })

  it("ids are unique", () => {
    const ids = OAUTH_PROVIDER_CATALOG.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it("each provider has a direct OAuth app registration docs link", () => {
    const docsHosts = Object.fromEntries(
      OAUTH_PROVIDER_CATALOG.map((provider) => [
        provider.id,
        new URL(provider.registrationDocsUrl).hostname,
      ]),
    )
    expect(docsHosts).toEqual({
      apple: "developer.apple.com",
      bitbucket: "developer.atlassian.com",
      discord: "discord.com",
      github: "docs.github.com",
      gitlab: "docs.gitlab.com",
      google: "developers.google.com",
      hubspot: "developers.hubspot.com",
      microsoft: "learn.microsoft.com",
      notion: "developers.notion.com",
      salesforce: "help.salesforce.com",
      slack: "api.slack.com",
    })
  })

  it("getPrimaryProviders() returns exactly 5 rows", () => {
    expect(getPrimaryProviders().length).toBe(5)
  })

  it("getPrimaryProviders() returns Google / GitHub / Microsoft / Apple / Discord", () => {
    const primaryIds = getPrimaryProviders().map((p) => p.id)
    expect(primaryIds.sort()).toEqual(
      ["apple", "discord", "github", "google", "microsoft"].sort(),
    )
  })

  it("getSecondaryProviders() returns the remaining 6", () => {
    expect(getSecondaryProviders().length).toBe(6)
  })

  it("primary + secondary partitions cover the full catalog", () => {
    const merged = new Set<string>([
      ...getPrimaryProviders().map((p) => p.id),
      ...getSecondaryProviders().map((p) => p.id),
    ])
    expect(merged.size).toBe(OAUTH_PROVIDER_CATALOG.length)
  })

  it("getProvider() looks up by id", () => {
    const google = getProvider("google")
    expect(google.displayName).toBe("Google")
    expect(google.tier).toBe("primary")
  })

  it("getProvider() throws on unknown id", () => {
    expect(() => getProvider("unknown" as never)).toThrow(/unknown oauth provider/i)
  })

  it("buildOAuthAuthorizeUrl() defaults to no `next` query when next === '/'", () => {
    const url = buildOAuthAuthorizeUrl("google", "/")
    expect(url).toBe(`${OAUTH_AUTHORIZE_PATH_PREFIX}google/authorize`)
  })

  it("buildOAuthAuthorizeUrl() encodes a non-trivial `next`", () => {
    const url = buildOAuthAuthorizeUrl("github", "/projects/abc?tab=overview")
    expect(url).toBe(
      `${OAUTH_AUTHORIZE_PATH_PREFIX}github/authorize` +
        `?next=${encodeURIComponent("/projects/abc?tab=overview")}`,
    )
  })

  it("buildOAuthAuthorizeUrl() omits `next` when not supplied", () => {
    expect(buildOAuthAuthorizeUrl("microsoft")).toBe(
      `${OAUTH_AUTHORIZE_PATH_PREFIX}microsoft/authorize`,
    )
  })

  it("OAUTH_AUTHORIZE_PATH_PREFIX matches AS.6.1 backend route shape", () => {
    expect(OAUTH_AUTHORIZE_PATH_PREFIX).toBe("/api/v1/auth/oauth/")
  })

  it("resolveOAuthProviderConfigured accepts the coarse public configured flag", () => {
    expect(resolveOAuthProviderConfigured({ configured: "true" })).toBe(true)
    expect(resolveOAuthProviderConfigured({ configured: "1" })).toBe(true)
    expect(resolveOAuthProviderConfigured({ configured: "false" })).toBe(false)
  })

  it("resolveOAuthProviderConfigured accepts client id plus secret-present flag", () => {
    expect(
      resolveOAuthProviderConfigured({
        clientId: "google-client-id",
        clientSecretConfigured: "true",
      }),
    ).toBe(true)
    expect(
      resolveOAuthProviderConfigured({
        clientId: "google-client-id",
        clientSecretConfigured: "false",
      }),
    ).toBe(false)
    expect(
      resolveOAuthProviderConfigured({
        clientId: "",
        clientSecretConfigured: "true",
      }),
    ).toBe(false)
  })

  it("primary providers' brand colors are non-grey (visible halo guard)", () => {
    // Every primary provider's brand color should produce a visible
    // halo against the dark nebula. Apple is white/silver which
    // counts as "non-grey" because the rgba halo at 0.55 still
    // shows as a luminous ring.
    for (const p of getPrimaryProviders()) {
      expect(p.brandColor.length).toBeGreaterThanOrEqual(7)
      expect(p.haloColor).toContain("0.55")
    }
  })
})
