// apps.manifest model for the web launcher — load + ajv-validate + filter.
//
// Web analogue of launcher-qt/src/manifest_model.cpp. The filtering and
// title-resolution semantics MUST stay byte-identical to that file (U1.2):
//   - caps_required: every entry must be present in the device caps
//   - roles: empty = visible to all; non-empty = user must hold any one
//   - title: titles[locale] -> titles.en fallback (schema-required key)
//   - sort: (categoryRank, order, id) ascending
//
// On top of those shared semantics the web filter adds ONE thing: apps
// whose `entry` block lacks a `web` route are dropped (qt-only). The
// schema permits qml/web/process and a qt-only app simply has no web
// renderer to land on — hiding it keeps the AppList free of unreachable
// tiles.
//
// Validation is ajv against design-system/apps.manifest.schema.json — the
// SAME file launcher-qt/scripts/manifest_load.py and scripts/validate_manifest.py
// consume. No second copy of the rules lives here. The schema is bundled
// into the static SPA at build time (no runtime fetch, no runtime Python).
import Ajv2020, { type ErrorObject, type ValidateFunction } from 'ajv/dist/2020.js';

// eslint-disable-next-line @typescript-eslint/consistent-type-imports
import schemaJson from '../../design-system/apps.manifest.schema.json';
import type {
  AppCategory,
  AppList,
  AppManifestEntry,
  FilterContext,
  FilteredApp,
  ManifestDoc,
} from './types';

// Mirrors apps.manifest.schema.json $defs.app.category enum ordering. Apps
// with an unknown category sort to the end (shouldn't happen post-validate).
const CATEGORY_ORDER: readonly AppCategory[] = [
  'core',
  'media',
  'communication',
  'tools',
  'diagnostics',
  'settings',
];

function categoryRank(cat: string): number {
  const i = CATEGORY_ORDER.indexOf(cat as AppCategory);
  return i >= 0 ? i : CATEGORY_ORDER.length;
}

/**
 * Thrown by {@link validateManifest} / {@link loadManifest} when the input
 * document fails schema validation. `errors` carries one string per
 * violation in the same shape manifest_load.py prints to stderr:
 *   `doc[i]: <message> (at <jsonPath>)`
 * so a SPA can surface the same diagnostic the CI cross-check would.
 */
export class ManifestError extends Error {
  public readonly errors: readonly string[];

  constructor(message: string, errors: readonly string[]) {
    super(message);
    this.name = 'ManifestError';
    this.errors = errors;
  }
}

let cachedValidator: ValidateFunction | null = null;

function getValidator(): ValidateFunction {
  if (cachedValidator) return cachedValidator;
  // Draft 2020-12 — the schema's declared $schema. ajv 2020 has the
  // meta-schema pre-loaded so no network fetch happens at validate time.
  // strict:false suppresses warnings about $comment / default keywords
  // (purely advisory in the schema; not part of the validation contract).
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  cachedValidator = ajv.compile(schemaJson as object);
  return cachedValidator;
}

function formatErrors(errors: readonly ErrorObject[], docIndex: number): string[] {
  return errors.map((e) => {
    const loc = e.instancePath ? e.instancePath.replace(/^\//, '').replace(/\//g, '/') : '<root>';
    return `doc[${docIndex}]: ${e.message ?? 'schema violation'} (at ${loc || '<root>'})`;
  });
}

/**
 * Validate `doc` against the shared apps.manifest.schema.json. On success
 * narrows the type to {@link ManifestDoc}; on failure throws a
 * {@link ManifestError} carrying every violation (ajv allErrors).
 *
 * @param docIndex Index used in the error prefix (`doc[i]: ...`); pass the
 *  doc's position in the multi-doc YAML so CI / SPA error lines match
 *  manifest_load.py's output.
 */
export function validateManifest(doc: unknown, docIndex: number = 0): asserts doc is ManifestDoc {
  const validate = getValidator();
  if (!validate(doc)) {
    const errors = formatErrors(validate.errors ?? [], docIndex);
    throw new ManifestError(
      errors[0] ?? `doc[${docIndex}]: invalid manifest (no error detail)`,
      errors,
    );
  }
}

function capsSatisfied(required: readonly string[], deviceCaps: readonly string[]): boolean {
  for (const c of required) {
    if (!deviceCaps.includes(c)) return false;
  }
  return true;
}

function rolesSatisfied(required: readonly string[], userRoles: readonly string[]): boolean {
  // Empty roles[] means visible to all (RBAC "any-of" gate, mirrors
  // ManifestModel::rolesSatisfied).
  if (required.length === 0) return true;
  for (const r of required) {
    if (userRoles.includes(r)) return true;
  }
  return false;
}

function resolveTitle(titles: Record<string, string>, locale: string): string {
  const active = titles[locale];
  if (typeof active === 'string' && active.length > 0) return active;
  // Schema requires every localizedString to carry `en`, so this is the
  // universal fallback (matches manifest_model.cpp::resolveTitle).
  return titles.en;
}

/**
 * Filter + project a validated {@link ManifestDoc} into a sorted
 * AppList for the web renderer.
 *
 * Semantics (locked to launcher-qt/src/manifest_model.cpp):
 *  1. Drop apps whose `entry` has no `web` route (qt-only; web-only filter).
 *  2. Drop apps whose `caps_required` is not a subset of `ctx.caps`.
 *  3. Drop apps whose `roles` is non-empty and disjoint from `ctx.userRoles`.
 *  4. Sort by (categoryRank, order, id) ascending.
 *  5. Resolve `title` using `ctx.locale` with `en` fallback.
 */
export function filterApps(doc: ManifestDoc, ctx: FilterContext = {}): FilteredApp[] {
  const caps = ctx.caps ?? [];
  const userRoles = ctx.userRoles ?? [];
  const locale = ctx.locale ?? 'en';

  const rows: FilteredApp[] = [];
  for (const a of doc.apps) {
    const web = a.entry.web;
    if (typeof web !== 'string' || web.length === 0) continue;

    const capsRequired = a.caps_required ?? [];
    if (!capsSatisfied(capsRequired, caps)) continue;

    const roles = a.roles ?? [];
    if (!rolesSatisfied(roles, userRoles)) continue;

    rows.push({
      id: a.id,
      title: resolveTitle(a.title, locale),
      icon: a.icon,
      category: a.category,
      // Schema default: 100 (kept in sync with apps.manifest.schema.json).
      order: typeof a.order === 'number' ? a.order : 100,
      entry: { web },
      capsRequired: [...capsRequired],
      roles: [...roles],
      // Schema default: true.
      singleInstance: typeof a.single_instance === 'boolean' ? a.single_instance : true,
    });
  }

  rows.sort((x, y) => {
    const cx = categoryRank(x.category);
    const cy = categoryRank(y.category);
    if (cx !== cy) return cx - cy;
    if (x.order !== y.order) return x.order - y.order;
    if (x.id < y.id) return -1;
    if (x.id > y.id) return 1;
    return 0;
  });

  return rows;
}

/**
 * Parse a JSON manifest, ajv-validate it, and return the filtered AppList.
 * Throws {@link ManifestError} if validation fails.
 *
 * This is the synchronous core used by {@link loadManifest} and by unit
 * tests (which feed YAML docs through their own parser before calling
 * here — js-yaml is dev-only).
 */
export function parseAndFilter(
  doc: unknown,
  ctx: FilterContext = {},
  docIndex: number = 0,
): AppList {
  validateManifest(doc, docIndex);
  return {
    deviceId: doc.device.id,
    apps: filterApps(doc, ctx),
  };
}

/**
 * Fetch a JSON apps.manifest from `url`, validate it, and return the
 * filtered AppList for the active device + user.
 *
 * The deployed shape is JSON (no YAML parser ships in the SPA — that path
 * stays on the build side via design-system/apps.manifest.example.yaml).
 *
 * @param url Absolute or app-relative URL of the JSON manifest.
 * @param ctx Device capabilities, user roles, and active locale.
 * @param fetchImpl Test seam — defaults to global `fetch`.
 */
export async function loadManifest(
  url: string,
  ctx: FilterContext = {},
  fetchImpl: typeof fetch = fetch,
): Promise<AppList> {
  const res = await fetchImpl(url);
  if (!res.ok) {
    throw new ManifestError(`fetch ${url} failed: HTTP ${res.status}`, [
      `doc[0]: fetch failed (HTTP ${res.status})`,
    ]);
  }
  const doc = (await res.json()) as unknown;
  return parseAndFilter(doc, ctx, 0);
}
