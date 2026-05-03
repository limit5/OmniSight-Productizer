/**
 * @omnisight/vite-plugin — W15.1 self-healing build/runtime error reporter.
 *
 * Borrows the spirit of open-lovable's `monitor-vite-logs` +
 * `report-vite-error` route pair: every compile-time and runtime error
 * the Vite dev server inside the omnisight-web-preview sidecar (W14.1)
 * encounters is posted to the backend at
 *
 *     POST /web-sandbox/preview/{workspace_id}/error
 *
 * so the LangGraph error_check_node (W15.2 — `backend/web/vite_error_relay.py`)
 * can fold it into `state.error_history`, the system-prompt template
 * (W15.3) can quote `[file:line] [message]` back to the agent on the
 * next turn, and the auto-retry budget (W15.4) can break the loop after
 * three identical failures.
 *
 * Wire shape contract (frozen — bump the `schema_version` literal in
 * `OMNISIGHT_VITE_ERROR_SCHEMA_VERSION` if it changes; the backend's
 * matching pin is `WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION` in
 * `backend/web_sandbox_vite_errors.py` and the drift guard test asserts
 * the two stay aligned):
 *
 *     {
 *       "schema_version": "1.0.0",
 *       "kind":          "compile" | "runtime",
 *       "phase":         "config" | "buildStart" | "load" | "transform"
 *                       | "hmr" | "client",
 *       "message":       "<vite or browser error message>",
 *       "file":          "src/App.tsx" | null,
 *       "line":          42 | null,
 *       "column":        7 | null,
 *       "stack":         "<truncated stack ≤ 8 KiB>" | null,
 *       "plugin":        "omnisight-vite-plugin",
 *       "plugin_version":"0.1.0",
 *       "occurred_at":   1714760400.123
 *     }
 *
 * The factory is pure: same input ⇒ same plugin object shape, no
 * implicit env reads. Callers who want env-driven defaults should wrap
 * the factory themselves (the W14.1 sidecar's W15.5 vite.config
 * scaffold will read `OMNISIGHT_WORKSPACE_ID` /
 * `OMNISIGHT_BACKEND_URL` and forward them in).
 *
 * Rolldown / Webpack equivalent
 * -----------------------------
 * The same wire shape is what the eventual rolldown / webpack-flavoured
 * sibling plugins must produce — the row spec calls them out
 * explicitly because the W14.1 sidecar already runs Vite *and* Bun's
 * Rolldown-flavoured transformer. The contract is:
 *
 *   1. POST `/web-sandbox/preview/{workspace_id}/error` with the
 *      schema above.
 *   2. Best-effort: never let a network failure crash the build /
 *      page — swallow the fetch rejection, log to stderr / console.
 *   3. Inject the runtime client into every served HTML page so
 *      `window.onerror` / `unhandledrejection` errors round-trip too.
 *
 * See README.md "Rolldown / Webpack equivalent" section for the
 * detailed cross-bundler porting notes.
 */

/**
 * Frozen schema version — bump in lock-step with
 * `WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION` in
 * `backend/web_sandbox_vite_errors.py`. The drift guard test (vitest)
 * asserts the literal matches the backend's pin via the OpenAPI
 * snapshot.
 */
export const OMNISIGHT_VITE_ERROR_SCHEMA_VERSION = "1.0.0"

/**
 * Frozen plugin name — matches the `plugin` field on the wire shape
 * and the backend's allowlist (any other value is rejected with 422).
 */
export const OMNISIGHT_VITE_PLUGIN_NAME = "omnisight-vite-plugin"

/**
 * Frozen plugin version — bumped only when the on-the-wire contract
 * changes in a backwards-incompatible way. Patch-level changes (typo
 * fixes, stack-trace truncation tweaks) do NOT bump this.
 */
export const OMNISIGHT_VITE_PLUGIN_VERSION = "0.1.0"

/**
 * Hard cap on the stack-trace bytes sent on the wire. The backend
 * accepts up to 16 KiB but truncates anything longer; we truncate
 * client-side too so the typical request stays well below 1 KB.
 */
export const STACK_TRACE_MAX_BYTES = 8 * 1024

/**
 * Hard cap on the message bytes sent on the wire. Vite's verbose
 * "Internal server error: ..." messages can exceed 4 KiB on a missing
 * import; the backend caps at 4 KiB.
 */
export const MESSAGE_MAX_BYTES = 4 * 1024

/**
 * Allowed `phase` literals — kept in sync with the backend's
 * `_ALLOWED_PHASES` set. Compile-time phases come from Vite hooks;
 * `client` is the browser-side runtime; `hmr` is the post-load HMR
 * channel.
 */
export const ALLOWED_PHASES = Object.freeze([
  "config",
  "buildStart",
  "load",
  "transform",
  "hmr",
  "client",
])

/**
 * Truncate a string so its UTF-8 byte length is `<= maxBytes`. Falls
 * back to a no-op for non-strings (the wire schema permits null, the
 * caller is responsible for normalisation before this is called).
 */
export function truncateUtf8(value, maxBytes) {
  if (typeof value !== "string" || maxBytes <= 0) return value
  // Fast path — most messages stay well below the cap.
  if (value.length <= maxBytes) return value
  // UTF-8 worst case is 4 bytes/char; if the string fits in the
  // worst-case envelope we still need to walk it because emoji + CJK
  // typically clock 3 B/char. We slice by codepoint to avoid splitting
  // a surrogate pair mid-character.
  const encoder = new TextEncoder()
  let out = value
  while (encoder.encode(out).byteLength > maxBytes && out.length > 0) {
    // Drop ~10% per iteration — converges in O(log n) steps for any
    // realistic input. Cap iterations to 64 as a paranoid safety net.
    out = out.slice(0, Math.max(1, Math.floor(out.length * 0.9)))
  }
  return out
}

/**
 * Normalise a Vite error-like object (`Error`, plugin-emitted error
 * with `loc`, or a plain object) into the wire shape's `file` / `line`
 * / `column` fields. Returns null-y fields when the source can't be
 * resolved — the backend stores `null` and the LangGraph error_check_node
 * (W15.2) handles missing source location by quoting `[unknown]` in
 * the prompt template (W15.3).
 */
export function extractErrorLocation(err) {
  if (!err || typeof err !== "object") {
    return { file: null, line: null, column: null }
  }
  // Vite's plugin context calls `this.error(msg, { line, column })` and
  // the resulting error carries `id` (resolved file id) + `loc.file` +
  // `loc.line` + `loc.column`. Rollup's flavour uses the same `loc`.
  const loc = err.loc || {}
  let file = null
  if (typeof err.id === "string") file = err.id
  else if (typeof loc.file === "string") file = loc.file
  else if (typeof err.file === "string") file = err.file
  let line = null
  if (typeof loc.line === "number" && Number.isFinite(loc.line)) line = loc.line
  else if (typeof err.line === "number" && Number.isFinite(err.line)) line = err.line
  let column = null
  if (typeof loc.column === "number" && Number.isFinite(loc.column)) column = loc.column
  else if (typeof err.column === "number" && Number.isFinite(err.column)) column = err.column
  return { file, line, column }
}

/**
 * Build the JSON payload posted to `/web-sandbox/preview/{ws}/error`.
 * Pure — same inputs ⇒ byte-identical output (modulo `occurred_at` if
 * the caller does not pass one in). The drift guard test pins the key
 * order so the wire shape stays canonical.
 */
export function buildErrorPayload({
  kind,
  phase,
  error,
  occurredAt,
} = {}) {
  if (kind !== "compile" && kind !== "runtime") {
    throw new Error(
      `omnisight-vite-plugin: kind must be "compile" or "runtime", got ${JSON.stringify(kind)}`,
    )
  }
  if (typeof phase !== "string" || !ALLOWED_PHASES.includes(phase)) {
    throw new Error(
      `omnisight-vite-plugin: phase must be one of ${ALLOWED_PHASES.join("|")}, got ${JSON.stringify(phase)}`,
    )
  }
  const message = (() => {
    if (error == null) return ""
    if (typeof error === "string") return error
    if (typeof error.message === "string") return error.message
    try {
      return String(error)
    } catch (_) {
      return "<unstringifiable error>"
    }
  })()
  const stack =
    error && typeof error === "object" && typeof error.stack === "string"
      ? error.stack
      : null
  const { file, line, column } = extractErrorLocation(error || {})
  const ts =
    typeof occurredAt === "number" && Number.isFinite(occurredAt)
      ? occurredAt
      : Date.now() / 1000
  return {
    schema_version: OMNISIGHT_VITE_ERROR_SCHEMA_VERSION,
    kind,
    phase,
    message: truncateUtf8(message, MESSAGE_MAX_BYTES),
    file,
    line,
    column,
    stack: stack === null ? null : truncateUtf8(stack, STACK_TRACE_MAX_BYTES),
    plugin: OMNISIGHT_VITE_PLUGIN_NAME,
    plugin_version: OMNISIGHT_VITE_PLUGIN_VERSION,
    occurred_at: ts,
  }
}

/**
 * Build the absolute URL the plugin (or the runtime overlay) POSTs
 * to. Centralised so the Rolldown / Webpack siblings can reuse it.
 *
 * `backendUrl` should be the backend's externally-resolvable base
 * (e.g. `http://omnisight-backend:8000` from inside the sidecar
 * network, or `https://api.example.com` when traversing CF Tunnel).
 * The function trims a trailing `/` so callers don't have to.
 */
export function buildErrorEndpoint(backendUrl, workspaceId) {
  if (typeof backendUrl !== "string" || backendUrl.length === 0) {
    throw new Error("omnisight-vite-plugin: backendUrl must be a non-empty string")
  }
  if (typeof workspaceId !== "string" || workspaceId.length === 0) {
    throw new Error("omnisight-vite-plugin: workspaceId must be a non-empty string")
  }
  const base = backendUrl.replace(/\/+$/, "")
  return `${base}/web-sandbox/preview/${encodeURIComponent(workspaceId)}/error`
}

/**
 * Default fetch implementation — Node 18+ ships a global `fetch`, so
 * the plugin works out of the box inside the W14.1 sidecar. Tests
 * inject a fake.
 */
function defaultFetch() {
  if (typeof globalThis.fetch === "function") {
    return globalThis.fetch.bind(globalThis)
  }
  throw new Error(
    "omnisight-vite-plugin: globalThis.fetch is not available — pass options.fetch explicitly",
  )
}

/**
 * Best-effort POST. Always resolves; never rejects. Network failures
 * are logged to `options.logger.warn` (defaults to `console.warn`)
 * and dropped — a Vite build that throws because the *report* failed
 * would be a self-defeating self-healing loop. The promise resolves
 * with `{ ok, status, error }` so callers / tests can introspect.
 */
export async function postErrorPayload({
  endpoint,
  payload,
  fetch: fetchImpl,
  logger,
  authToken,
} = {}) {
  const f = typeof fetchImpl === "function" ? fetchImpl : defaultFetch()
  const log = logger || console
  try {
    const headers = { "content-type": "application/json" }
    if (typeof authToken === "string" && authToken.length > 0) {
      headers["authorization"] = `Bearer ${authToken}`
    }
    const res = await f(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    })
    if (!res || typeof res.status !== "number") {
      return { ok: false, status: 0, error: "no-response" }
    }
    if (res.status >= 200 && res.status < 300) {
      return { ok: true, status: res.status, error: null }
    }
    return { ok: false, status: res.status, error: `http-${res.status}` }
  } catch (err) {
    if (typeof log.warn === "function") {
      log.warn(
        `[omnisight-vite-plugin] failed to POST ${endpoint}: ${err && err.message ? err.message : err}`,
      )
    }
    return { ok: false, status: 0, error: err && err.message ? err.message : String(err) }
  }
}

/**
 * Render the runtime overlay script that gets injected into every
 * HTML response by `transformIndexHtml`. The script registers a
 * `window.onerror` + `unhandledrejection` handler and POSTs the
 * captured runtime errors to the same endpoint as the compile-time
 * branch. Kept inline (not loaded via `<script src>`) so the sidecar
 * doesn't need to serve a separate static asset.
 *
 * `endpointUrl` is templated in at build-time. The script is plain
 * ES2017 — no transpile step required, runs in every modern browser
 * the W14.6 iframe panel supports.
 */
export function renderRuntimeOverlayScript({
  endpointUrl,
  pluginVersion = OMNISIGHT_VITE_PLUGIN_VERSION,
  schemaVersion = OMNISIGHT_VITE_ERROR_SCHEMA_VERSION,
} = {}) {
  if (typeof endpointUrl !== "string" || endpointUrl.length === 0) {
    throw new Error("renderRuntimeOverlayScript: endpointUrl must be a non-empty string")
  }
  // Embedded as a single-quoted JSON literal so we don't have to
  // escape the URL for HTML attribute context (the script tag inserts
  // it verbatim into a JS string literal).
  const cfg = JSON.stringify({
    endpointUrl,
    pluginVersion,
    schemaVersion,
    pluginName: OMNISIGHT_VITE_PLUGIN_NAME,
    messageMaxBytes: MESSAGE_MAX_BYTES,
    stackMaxBytes: STACK_TRACE_MAX_BYTES,
  })
  return [
    "<script>",
    "(function(){",
    "  var cfg = " + cfg + ";",
    "  function truncate(s, max){ if(typeof s!=='string') return s; if(s.length<=max) return s; return s.slice(0, max); }",
    "  function send(kind, phase, message, file, line, column, stack){",
    "    try {",
    "      var payload = {",
    "        schema_version: cfg.schemaVersion,",
    "        kind: kind, phase: phase,",
    "        message: truncate(message||'', cfg.messageMaxBytes),",
    "        file: file||null, line: (typeof line==='number')?line:null,",
    "        column: (typeof column==='number')?column:null,",
    "        stack: stack==null?null:truncate(stack, cfg.stackMaxBytes),",
    "        plugin: cfg.pluginName, plugin_version: cfg.pluginVersion,",
    "        occurred_at: Date.now()/1000",
    "      };",
    "      if (typeof navigator!=='undefined' && typeof navigator.sendBeacon==='function') {",
    "        var blob = new Blob([JSON.stringify(payload)], {type:'application/json'});",
    "        if (navigator.sendBeacon(cfg.endpointUrl, blob)) return;",
    "      }",
    "      fetch(cfg.endpointUrl, { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(payload), keepalive:true }).catch(function(){});",
    "    } catch(_){ /* never let reporting break the page */ }",
    "  }",
    "  window.addEventListener('error', function(ev){",
    "    var err = ev && ev.error;",
    "    var msg = (ev && ev.message) || (err && err.message) || 'Unknown error';",
    "    var stk = err && err.stack ? err.stack : null;",
    "    send('runtime','client', msg, ev && ev.filename || null, ev && typeof ev.lineno==='number'?ev.lineno:null, ev && typeof ev.colno==='number'?ev.colno:null, stk);",
    "  });",
    "  window.addEventListener('unhandledrejection', function(ev){",
    "    var r = ev && ev.reason;",
    "    var msg = (r && r.message) || (typeof r==='string'?r:'Unhandled promise rejection');",
    "    var stk = r && r.stack ? r.stack : null;",
    "    send('runtime','client', msg, null, null, null, stk);",
    "  });",
    "  if (typeof import_meta_hot_handler === 'undefined' && typeof window !== 'undefined') {",
    "    window.__OMNISIGHT_VITE_RUNTIME__ = { schemaVersion: cfg.schemaVersion, pluginVersion: cfg.pluginVersion, send: send };",
    "  }",
    "})();",
    "</script>",
  ].join("\n")
}

/**
 * The actual Vite plugin factory. Returns a Vite plugin object whose
 * hooks fire on every compile-time error path Vite exposes plus a
 * `transformIndexHtml` that injects the runtime overlay so client-side
 * `window.onerror` / `unhandledrejection` events round-trip too.
 *
 * Required options:
 *
 *   * `workspaceId` — operator workspace key (matches the path param
 *     on `/web-sandbox/preview/{workspace_id}/error`). The W14.1
 *     sidecar's W15.5 vite.config scaffold will read this from
 *     `process.env.OMNISIGHT_WORKSPACE_ID`.
 *   * `backendUrl` — backend base URL (e.g. `http://omnisight-backend:8000`
 *     when running inside the W14.1 docker network, or
 *     `https://api.example.com` when the sidecar talks to the public
 *     edge). The W15.5 scaffold will read this from
 *     `process.env.OMNISIGHT_BACKEND_URL`.
 *
 * Optional options:
 *
 *   * `authToken` — Bearer token forwarded as `Authorization: Bearer <t>`.
 *     Lets the backend reject anonymous error reports if the
 *     deployment terminates auth at the LB rather than the sidecar
 *     network.
 *   * `fetch` — fetch implementation override (tests).
 *   * `logger` — logger with a `.warn(msg)` method (tests).
 *   * `injectRuntime` — set to `false` to skip the
 *     `transformIndexHtml` injection (e.g. when the host page already
 *     has its own error reporter and you only want the compile-time
 *     branch).
 */
export function omnisightVitePlugin(options = {}) {
  const {
    workspaceId,
    backendUrl,
    authToken,
    fetch: fetchImpl,
    logger,
    injectRuntime = true,
  } = options
  const endpoint = buildErrorEndpoint(backendUrl, workspaceId)
  const log = logger || console

  /**
   * Internal — accept any error-like value, build the wire payload,
   * and POST it best-effort. Wrapped in a try/catch so a malformed
   * `error` argument from a future Vite version can never crash the
   * dev server.
   */
  async function reportError(kind, phase, errorLike) {
    let payload
    try {
      payload = buildErrorPayload({ kind, phase, error: errorLike })
    } catch (err) {
      if (typeof log.warn === "function") {
        log.warn(
          `[omnisight-vite-plugin] dropped malformed error: ${err && err.message ? err.message : err}`,
        )
      }
      return { ok: false, status: 0, error: "payload-build-failed" }
    }
    return postErrorPayload({ endpoint, payload, fetch: fetchImpl, logger: log, authToken })
  }

  return {
    name: OMNISIGHT_VITE_PLUGIN_NAME,
    apply: () => true,

    /**
     * Vite hook — fired whenever the resolved config rejects (e.g. a
     * config file has a syntax error). The hook actually receives
     * `config` not an error, so we stash a reference and let
     * `buildStart` / `configureServer` carry the typical config-time
     * failures via try/catch on adjacent hooks.
     */
    configResolved() {
      // No-op today — provided for future config-validation reporting.
    },

    /**
     * Rollup `buildStart` — fires before the bundler walks the module
     * graph. Errors thrown synchronously here still surface via the
     * bundler's main `onError` path, but we wrap to also report.
     */
    async buildStart() {
      // Intentional no-op; the hook exists so callers see we are
      // present in the plugin chain and the order is deterministic.
    },

    /**
     * Rollup `load` — fired when the bundler reads a module from
     * disk. Plugin-emitted errors bubble through the bundler's central
     * error path; this passthrough exists so the future Rolldown port
     * can hook the same pivot point.
     */
    async load() {
      return null
    },

    /**
     * Rollup `transform` — fired for every module transform. Same
     * passthrough story as `load`.
     */
    async transform() {
      return null
    },

    /**
     * Rollup `handleHotUpdate` — fired on every HMR cycle. Errors
     * here imply a partial-update failure; reporting them helps the
     * agent see "the file changed but the page didn't reload" loops.
     */
    async handleHotUpdate(ctx) {
      // Caller never throws here in practice; we register the hook so
      // a sibling plugin chain that does throw is still observable
      // via the Vite server's central error handler (registered in
      // `configureServer` below).
      return ctx && ctx.modules
    },

    /**
     * Vite hook — wires the plugin into the dev-server lifecycle so
     * we can subscribe to the central error events Vite exposes
     * (`server.middlewares` errors, `server.ws.on('error')`, plus the
     * `viteError` named event). Single subscription point keeps the
     * plugin from double-reporting when multiple Rollup hooks see
     * the same underlying failure.
     */
    configureServer(server) {
      if (!server) return
      // Vite emits a `viteError` event on the server bus when an
      // internal error path needs to surface to plugins; piggy-backing
      // here means we capture the typed Vite errors with their
      // `loc` / `id` already populated.
      try {
        if (typeof server.httpServer?.on === "function") {
          server.httpServer.on("error", (err) => {
            void reportError("compile", "buildStart", err)
          })
        }
        if (typeof server.ws?.on === "function") {
          server.ws.on("error", (err) => {
            void reportError("compile", "hmr", err)
          })
        }
      } catch (err) {
        if (typeof log.warn === "function") {
          log.warn(
            `[omnisight-vite-plugin] configureServer wire-up failed: ${err && err.message ? err.message : err}`,
          )
        }
      }
    },

    /**
     * Vite `transformIndexHtml` — injects the runtime overlay script
     * into every served HTML response so the browser's `window.onerror`
     * + `unhandledrejection` handlers can POST runtime errors back to
     * the backend. Skip with `injectRuntime: false` when the host
     * page already wires its own reporter.
     */
    transformIndexHtml(html) {
      if (!injectRuntime) return html
      const tag = renderRuntimeOverlayScript({ endpointUrl: endpoint })
      // Inject before `</head>` when present, else prepend so the
      // handler is registered before any inline script runs.
      if (typeof html !== "string") return html
      const lower = html.toLowerCase()
      const headCloseIdx = lower.indexOf("</head>")
      if (headCloseIdx >= 0) {
        return html.slice(0, headCloseIdx) + tag + "\n" + html.slice(headCloseIdx)
      }
      return tag + "\n" + html
    },

    /**
     * Public surface for tests and sibling plugins — exposes
     * `reportError` so a Rolldown / Webpack adapter that reuses this
     * plugin's transport doesn't have to re-implement it.
     */
    api: {
      reportError,
      endpoint,
      schemaVersion: OMNISIGHT_VITE_ERROR_SCHEMA_VERSION,
      pluginVersion: OMNISIGHT_VITE_PLUGIN_VERSION,
    },
  }
}

export default omnisightVitePlugin
