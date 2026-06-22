// TS types mirroring design-system/apps.manifest.schema.json (U0 contract).
//
// The schema at design-system/apps.manifest.schema.json is the SOLE source
// of truth for "is this a valid manifest" (ajv validates against it at
// runtime). These TS shapes are a one-way mirror so callers can use the
// validated `unknown` payload with type safety after `validateManifest()`
// has narrowed it.
//
// Keep this file in lock-step with the JSON Schema — any field added there
// MUST be added here (or the launcher-web build will silently lose access
// to it).

/** Schema enum: device.display. */
export type DeviceDisplay = 'local' | 'headless' | 'both';

/** Schema enum: device.default_renderer. */
export type DeviceRenderer = 'qt' | 'web';

/** Schema enum: app.category (drives sort order + section accent). */
export type AppCategory =
  | 'core'
  | 'media'
  | 'communication'
  | 'tools'
  | 'diagnostics'
  | 'settings';

/** Schema enum: theme.density. */
export type ThemeDensity = 'comfortable' | 'compact';

/**
 * Locale-keyed strings. Schema requires `en` (universal fallback) and
 * locale keys match `^[a-z]{2}(-[A-Za-z]{2,4})?$`.
 */
export interface LocalizedString {
  en: string;
  [locale: string]: string;
}

/** Renderer-specific launch targets (`entry` block). At least one MUST be present. */
export interface AppEntry {
  /** QML component URL for launcher-qt (e.g. `qrc:/apps/CameraView.qml`). */
  qml?: string;
  /** Route in launcher-web (e.g. `/camera`). */
  web?: string;
  /** External process argv launched by launcher-qt. */
  process?: string[];
}

/** One app tile entry under `apps[]`. */
export interface AppManifestEntry {
  id: string;
  title: LocalizedString;
  icon: string;
  category: AppCategory;
  /** Sort within category (ascending). Schema default = 100. */
  order?: number;
  entry: AppEntry;
  /** Capabilities the device must have for the tile to show. Schema default = []. */
  caps_required?: string[];
  /** RBAC gate; empty = visible to all. Schema default = []. */
  roles?: string[];
  /** If true, re-launch focuses the running instance. Schema default = true. */
  single_instance?: boolean;
}

/** Top-level `device` block. */
export interface DeviceBlock {
  id: string;
  display: DeviceDisplay;
  default_renderer: DeviceRenderer;
  locales?: string[];
}

/** Optional `theme` block. */
export interface ThemeBlock {
  brand?: string;
  accent?: string;
  density?: ThemeDensity;
}

/** A single manifest document (one of potentially many in a multi-doc YAML). */
export interface ManifestDoc {
  schema_version: 1;
  device: DeviceBlock;
  theme?: ThemeBlock;
  apps: AppManifestEntry[];
}

/**
 * Capability + role + locale gate input passed to {@link filterApps}.
 * Web analogue of ManifestModel::setCapabilities / setUserRoles / setLocale.
 */
export interface FilterContext {
  /** Capabilities the running device exposes (HAL profile). */
  caps?: string[];
  /** RBAC roles held by the active user. */
  userRoles?: string[];
  /** Active locale tag (e.g. `en`, `zh-Hant`). Falls back to `en`. */
  locale?: string;
}

/**
 * One entry in the AppList produced by {@link filterApps} / {@link loadManifest}.
 * Note the entry collapses to a single `web` route — qt-only apps are
 * dropped by the web filter so this is always defined.
 */
export interface FilteredApp {
  id: string;
  /** Resolved title for the active locale (en fallback). */
  title: string;
  icon: string;
  category: AppCategory;
  order: number;
  entry: { web: string };
  capsRequired: string[];
  roles: string[];
  singleInstance: boolean;
}

/** Result of {@link loadManifest} — device id + the filtered AppList. */
export interface AppList {
  deviceId: string;
  apps: FilteredApp[];
}
