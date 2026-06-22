// launch dispatcher for the web launcher (U2.4).
//
// Web analogue of launcher-qt/src/app_launcher.cpp::activate — but instead
// of QProcess / qmlViewRequested, we drive plain web navigation: a same-
// origin path goes through history.pushState (the static SPA stays a
// single page), an external http(s) URL goes through window.location.assign
// (a safe non-eval navigation primitive).
//
// AC-Code (OP-2294):
//   - activate(appId) resolves entry.web off the U2.2 model and dispatches
//   - web-only apps appear; qt-only apps (no entry.web) never reach here
//     because lib/manifest.ts::filterApps drops them from the AppList
//   - target validated: same-origin path matching the schema pattern OR an
//     absolute http(s) URL parseable by `new URL(...)`. Anything else
//     (`javascript:`, `data:`, `file:`, protocol-relative `//host`, raw
//     gibberish, empty string) is rejected with `unsafe-target` and the
//     navigation is suppressed — no eval, no inline-exec, no shell.
//
// MUST NOT: do NOT use eval / Function() / dangerouslySetInnerHTML to
// dispatch the navigation. Do NOT touch the productizer backend / auth
// surface here — this is purely a client-side navigation helper.
import type { FilteredApp } from './types';

/** Schema pattern for entry.web (apps.manifest.schema.json $defs.entry.web). */
const SAFE_PATH = /^\/[A-Za-z0-9/_-]*$/;

/**
 * Outcome of a single {@link activate} call. Emitted via `onEvent` and
 * returned to the caller so a UI layer can decide whether to show a toast
 * for `unsafe-target` / `unknown-app` without re-implementing the policy.
 */
export type LaunchEvent =
  | { code: 'launched'; appId: string; mode: 'internal'; target: string }
  | { code: 'launched'; appId: string; mode: 'external'; target: string }
  | { code: 'unknown-app'; appId: string }
  | { code: 'no-web-entry'; appId: string }
  | { code: 'unsafe-target'; appId: string; target: string; reason: string };

/**
 * Caller-supplied seams. All three are optional — production wiring uses
 * the defaults (history.pushState + window.location.assign). Tests pass
 * spies so navigation is observable without jsdom actually changing
 * window.location.
 */
export interface ActivateConfig {
  /** Same-origin path dispatcher; default = `window.history.pushState`. */
  navigateInternal?: (path: string) => void;
  /** External URL dispatcher; default = `window.location.assign`. */
  navigateExternal?: (url: string) => void;
  /** Event sink (always fires, even on rejection). */
  onEvent?: (event: LaunchEvent) => void;
}

type Classified =
  | { kind: 'internal'; path: string }
  | { kind: 'external'; url: string }
  | { kind: 'unsafe'; reason: string };

/**
 * Decide whether `target` is a same-origin path, an external http(s) URL,
 * or unsafe. Exported for tests and for callers that want to pre-flight
 * a target before activating.
 */
export function classifyTarget(target: string): Classified {
  if (typeof target !== 'string' || target.length === 0) {
    return { kind: 'unsafe', reason: 'empty target' };
  }
  // Protocol-relative URLs (`//host/path`) inherit the page's scheme and
  // jump to an arbitrary host. Reject before the path branch sees the
  // leading slash.
  if (target.startsWith('//')) {
    return { kind: 'unsafe', reason: 'protocol-relative URL not allowed' };
  }
  if (target.startsWith('/')) {
    // Same-origin path. Lock to the schema's entry.web pattern so the
    // launcher never navigates somewhere a valid manifest couldn't put it
    // (e.g. `/x?javascript:alert(1)` is filtered out here even though it
    // starts with `/`).
    if (!SAFE_PATH.test(target)) {
      return { kind: 'unsafe', reason: 'path contains disallowed characters' };
    }
    return { kind: 'internal', path: target };
  }
  // External: must parse as an absolute URL and use an http(s) scheme.
  // `new URL` rejects everything that isn't a valid absolute URL, so
  // bare strings ("foo"), `javascript:alert(1)`-without-the-base trick,
  // etc., land in the catch.
  let parsed: URL;
  try {
    parsed = new URL(target);
  } catch {
    return { kind: 'unsafe', reason: 'not an absolute URL' };
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { kind: 'unsafe', reason: `scheme ${parsed.protocol} not allowed` };
  }
  return { kind: 'external', url: parsed.toString() };
}

function findApp(
  apps: readonly FilteredApp[],
  appId: string,
): FilteredApp | undefined {
  for (const a of apps) {
    if (a.id === appId) return a;
  }
  return undefined;
}

function defaultNavigateInternal(path: string): void {
  // Static SPA — there is no per-route React tree yet (U2.5+ wires the
  // inner views). pushState updates the address bar without forcing a full
  // document reload, leaving the launcher mounted; downstream tickets can
  // hook popstate to swap the active view. Falls back to a no-op when
  // history is unavailable (server-render path, jsdom without history).
  if (typeof window !== 'undefined' && window.history?.pushState) {
    window.history.pushState({}, '', path);
  }
}

function defaultNavigateExternal(url: string): void {
  // window.location.assign is the safe non-eval navigation primitive — it
  // takes a URL string (not script) and lets the browser perform a normal
  // navigation, identical to a user clicking a link.
  if (typeof window !== 'undefined' && window.location?.assign) {
    window.location.assign(url);
  }
}

/**
 * Resolve `appId` in `apps`, validate its `entry.web` target, and dispatch
 * the appropriate navigation. Returns the {@link LaunchEvent} that fired
 * so the caller can render UI off it; the event also flows through
 * `config.onEvent` if supplied.
 *
 * The function is intentionally synchronous and side-effect-narrow: the
 * only mutable thing it touches is the browser navigation surface (or the
 * test-supplied seams).
 */
export function activate(
  appId: string,
  apps: readonly FilteredApp[],
  config: ActivateConfig = {},
): LaunchEvent {
  const navigateInternal = config.navigateInternal ?? defaultNavigateInternal;
  const navigateExternal = config.navigateExternal ?? defaultNavigateExternal;

  const app = findApp(apps, appId);
  if (!app) {
    const event: LaunchEvent = { code: 'unknown-app', appId };
    config.onEvent?.(event);
    return event;
  }

  // filterApps drops qt-only entries, so a FilteredApp always carries
  // entry.web — but a defensive check keeps the dispatcher honest if the
  // upstream model is ever bypassed.
  const target = app.entry.web;
  if (typeof target !== 'string' || target.length === 0) {
    const event: LaunchEvent = { code: 'no-web-entry', appId };
    config.onEvent?.(event);
    return event;
  }

  const classified = classifyTarget(target);
  if (classified.kind === 'unsafe') {
    const event: LaunchEvent = {
      code: 'unsafe-target',
      appId,
      target,
      reason: classified.reason,
    };
    config.onEvent?.(event);
    return event;
  }

  if (classified.kind === 'internal') {
    navigateInternal(classified.path);
    const event: LaunchEvent = {
      code: 'launched',
      appId,
      mode: 'internal',
      target: classified.path,
    };
    config.onEvent?.(event);
    return event;
  }

  navigateExternal(classified.url);
  const event: LaunchEvent = {
    code: 'launched',
    appId,
    mode: 'external',
    target: classified.url,
  };
  config.onEvent?.(event);
  return event;
}

export default activate;
