"use client"

/**
 * WP.6 (OP-1500) — Frontend settings sync-scope registry.
 *
 * Mirrors ``backend/settings_registry.py``. Exposes:
 *
 *   - ``SettingMetadata`` / ``SyncMode`` / ``Scope`` / ``Platform`` types
 *     (canonical strings — drift between frontend and backend triggers
 *     CI failures via the matching test in
 *     ``test/lib/settings-registry.test.ts``).
 *   - ``fetchSettingsRegistry()`` — single round-trip against
 *     ``GET /settings/registry`` which returns the registry + the
 *     server-derived ``platform`` (UA-sniffed).
 *   - ``getDeviceId()`` — stable per-browser-install device id used as
 *     the partition for ``sync='never'`` settings. Survives reloads via
 *     ``localStorage`` and falls back to a freshly minted id on a
 *     hard-clear / private-browsing context.
 *   - ``getUserPreferenceScoped()`` / ``setUserPreferenceScoped()`` —
 *     thin wrappers around the J4 ``user_preferences`` API that fold
 *     in the WP.6 ``?platform=`` / ``?device_id=`` query params based
 *     on a setting's registry sync mode. Use these from SoT modules
 *     (e.g. future ``notification-sound-preferences.ts``) instead of
 *     calling ``getUserPreference`` / ``setUserPreference`` directly,
 *     so per-platform / device-only sync is handled at one boundary.
 *
 * Module-global state audit (per ``implement_phase_step.md`` Step 1):
 *
 *   - ``_registryPromise`` is a per-tab in-flight memo cache for the
 *     registry fetch. It is intentionally module-scoped — every
 *     consumer in the tab benefits from a single round-trip and the
 *     registry is conceptually immutable for the tab's lifetime. The
 *     cache resets on a 4xx / 5xx so a transient failure doesn't
 *     permanently freeze the registry empty.
 *   - ``getDeviceId()`` reads / lazily writes ``localStorage`` keyed
 *     by ``OMNISIGHT_DEVICE_ID_KEY``. This is browser-local state by
 *     construction (the whole point of ``sync='never'``).
 *
 * Read-after-write timing audit: ``setUserPreferenceScoped`` awaits
 * the J4 PUT before resolving, identical to the legacy
 * ``setUserPreference`` shape; no additional ordering hazard.
 */

import {
  getSettingsRegistry,
  getUserPreferenceWithScope,
  setUserPreferenceWithScope,
} from "@/lib/api"

// ─── Type vocabulary ───────────────────────────────────────────────

export const SCOPE_VALUES = ["tenant", "user", "device"] as const
export type Scope = (typeof SCOPE_VALUES)[number]

export const SYNC_MODE_VALUES = ["globally", "per_platform", "never"] as const
export type SyncMode = (typeof SYNC_MODE_VALUES)[number]

export const PLATFORM_VALUES = [
  "macos",
  "windows",
  "linux",
  "ios",
  "android",
  "web",
] as const
export type Platform = (typeof PLATFORM_VALUES)[number]

export interface SettingMetadata {
  pref_key: string
  scope: Scope
  sync: SyncMode
  supported_platforms: Platform[]
  description: string
  default_value: string
}

export interface SettingsRegistry {
  settings: SettingMetadata[]
  derived_platform: Platform
}

// ─── Registry fetch (per-tab memo) ─────────────────────────────────

let _registryPromise: Promise<SettingsRegistry> | null = null

/**
 * Fetch the WP.6 settings sync-scope registry once per tab.
 *
 * The returned promise is shared by every caller in the tab — the
 * registry is conceptually immutable for the tab's lifetime, and a
 * single round-trip lets the settings UI render its sync-scope
 * badges + the SoT modules figure out partition keys in lock-step.
 *
 * On error we *do not* poison the cache: the next caller gets a
 * fresh attempt. This matches the rest of the J4 fetch surface
 * (transient SSE / pool outages should not freeze the UI's metadata
 * empty for the rest of the session).
 */
export async function fetchSettingsRegistry(): Promise<SettingsRegistry> {
  if (_registryPromise) return _registryPromise
  const p = (async () => {
    try {
      const payload = await getSettingsRegistry()
      const settings = payload.settings.map((s) => ({
        pref_key: s.pref_key,
        scope: s.scope as Scope,
        sync: s.sync as SyncMode,
        supported_platforms: (s.supported_platforms as Platform[]) ?? [],
        description: s.description,
        default_value: s.default_value,
      }))
      const derived = (PLATFORM_VALUES as readonly string[]).includes(
        payload.derived_platform,
      )
        ? (payload.derived_platform as Platform)
        : "web"
      return { settings, derived_platform: derived }
    } catch (err) {
      _registryPromise = null
      throw err
    }
  })()
  _registryPromise = p
  return p
}

/** Test helper — drop the per-tab memo cache. Exported for tests
 *  only; production callers should never need to clear the cache. */
export function _resetSettingsRegistryCacheForTests(): void {
  _registryPromise = null
}

// ─── Device id (browser-local, stable across reloads) ──────────────

export const OMNISIGHT_DEVICE_ID_KEY = "omnisight:device-id"

/**
 * Stable per-browser-install device id used as the partition for
 * ``sync='never'`` settings. Persisted in ``localStorage`` so the
 * same device picks up its own machine-local preferences after a
 * reload; falls back to an in-memory id (regenerated on every
 * import) in a hard-clear / private-browsing context where
 * ``localStorage`` is unavailable.
 *
 * SSR-safe: returns a placeholder ``"ssr"`` token if ``window`` is
 * not available. Callers should re-evaluate after mount.
 */
let _inMemoryDeviceId: string | null = null

export function getDeviceId(): string {
  if (typeof window === "undefined") return "ssr"
  try {
    const existing = window.localStorage.getItem(OMNISIGHT_DEVICE_ID_KEY)
    if (existing) return existing
    const minted = _mintDeviceId()
    window.localStorage.setItem(OMNISIGHT_DEVICE_ID_KEY, minted)
    return minted
  } catch {
    if (!_inMemoryDeviceId) _inMemoryDeviceId = _mintDeviceId()
    return _inMemoryDeviceId
  }
}

function _mintDeviceId(): string {
  try {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return `dev-${crypto.randomUUID()}`
    }
  } catch {
    // ignore — fall through to Math.random fallback below.
  }
  return `dev-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`
}

// ─── J4 helpers that respect WP.6 sync-scope ───────────────────────

/**
 * Read a user preference, transparently routing per-platform / device
 * sync modes through the WP.6 partitioning query parameters.
 *
 * ``meta`` is the registry entry for ``pref_key``; if ``undefined``,
 * the helper falls back to the legacy bare-key fetch (so an
 * unregistered key behaves exactly like ``getUserPreference``).
 *
 * Returns the same ``{ key, value }`` shape as
 * ``lib/api.ts::getUserPreference``; ``null`` on 404 / network error
 * so call sites can fall back to a default cleanly.
 */
export async function getUserPreferenceScoped(
  pref_key: string,
  meta: SettingMetadata | undefined,
  derivedPlatform: Platform,
): Promise<{ key: string; value: string } | null> {
  const scope = _scopeFromMeta(meta, derivedPlatform)
  return getUserPreferenceWithScope(pref_key, scope)
}

export async function setUserPreferenceScoped(
  pref_key: string,
  value: string,
  meta: SettingMetadata | undefined,
  derivedPlatform: Platform,
): Promise<void> {
  const scope = _scopeFromMeta(meta, derivedPlatform)
  await setUserPreferenceWithScope(pref_key, value, scope)
}

function _scopeFromMeta(
  meta: SettingMetadata | undefined,
  derivedPlatform: Platform,
): { platform?: string; device_id?: string } | undefined {
  if (!meta) return undefined
  if (meta.sync === "globally") return undefined
  if (meta.sync === "per_platform") return { platform: derivedPlatform }
  if (meta.sync === "never") return { device_id: getDeviceId() }
  return undefined
}

// ─── Human-readable copy for the sync-scope badge ──────────────────

export const SYNC_MODE_LABEL: Record<SyncMode, string> = {
  globally: "Synced everywhere",
  per_platform: "This platform only",
  never: "This device only",
}

export const SYNC_MODE_HINT: Record<SyncMode, string> = {
  globally:
    "Changes apply to all your devices automatically.",
  per_platform:
    "Each platform (macOS / Windows / Linux / mobile / web) keeps its own value.",
  never:
    "Stays on this machine — never synced to another device.",
}

/** Look up a setting in a registry list. ``undefined`` for unknown
 *  keys — call sites should treat that as legacy ``globally`` to
 *  preserve today's behaviour. */
export function findSetting(
  settings: SettingMetadata[],
  pref_key: string,
): SettingMetadata | undefined {
  return settings.find((s) => s.pref_key === pref_key)
}
