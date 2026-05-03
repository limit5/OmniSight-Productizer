# `@omnisight/vite-plugin` — W15.1 self-healing build/runtime error reporter

> Borrows the spirit of open-lovable's `monitor-vite-logs` +
> `report-vite-error` route pair, restitched to fit OmniSight's
> backend. Every compile-time and runtime error the Vite dev server
> inside the [W14.1 `omnisight-web-preview` sidecar](../../web-preview/manifest.json)
> encounters is POSTed to the backend at
>
> ```
> POST /web-sandbox/preview/{workspace_id}/error
> ```
>
> so the LangGraph error_check_node ([W15.2](../../TODO.md) —
> `backend/web/vite_error_relay.py`) can fold it into
> `state.error_history`, the system-prompt template (W15.3) can quote
> `[file:line] [message]` back to the agent on the next turn, and the
> auto-retry budget (W15.4) can break the loop after three identical
> failures.

## Install

This is a private workspace package. The W14.1 sidecar's W15.5 vite
config scaffold imports it via a relative path mount:

```js
// vite.config.js (rendered by W15.5 scaffold)
import { defineConfig } from "vite"
import { omnisightVitePlugin } from "@omnisight/vite-plugin"

export default defineConfig({
  plugins: [
    omnisightVitePlugin({
      workspaceId: process.env.OMNISIGHT_WORKSPACE_ID,
      backendUrl: process.env.OMNISIGHT_BACKEND_URL,
      authToken: process.env.OMNISIGHT_BACKEND_TOKEN, // optional
    }),
  ],
})
```

`workspaceId` and `backendUrl` are required; everything else is
optional. The sidecar entry point sets both env vars at launch time
(W14.2 wiring).

## Wire shape

The plugin posts the following JSON shape (frozen — bump
`OMNISIGHT_VITE_ERROR_SCHEMA_VERSION` in lock-step with the backend's
`WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION` literal in
[`backend/web_sandbox_vite_errors.py`](../../backend/web_sandbox_vite_errors.py)
when changed):

```jsonc
{
  "schema_version": "1.0.0",
  "kind":          "compile",          // | "runtime"
  "phase":         "transform",        // | "config" | "buildStart"
                                        //   | "load" | "hmr" | "client"
  "message":       "Failed to parse module",
  "file":          "src/App.tsx",      // | null
  "line":          42,                  // | null
  "column":        7,                   // | null
  "stack":         "...truncated 8 KiB...",  // | null
  "plugin":        "omnisight-vite-plugin",
  "plugin_version": "0.1.0",
  "occurred_at":   1714760400.123
}
```

The endpoint accepts up to **16 KiB** of payload; this plugin
truncates `message` to 4 KiB and `stack` to 8 KiB client-side so the
typical request stays well below 1 KB.

## Best-effort transport

Network failures are **never** surfaced as plugin errors — a Vite
build that throws because the *report* failed would be a self-defeating
self-healing loop. Failed POSTs:

* log to `console.warn` (or the injected `options.logger.warn`),
* resolve as `{ ok: false, status: 0, error: "<reason>" }`,
* never reject the promise, never crash the dev server.

The browser-side runtime overlay uses `navigator.sendBeacon` when
available so the report flushes even mid-page-unload; falls back to
`fetch(..., { keepalive: true })`.

## Rolldown / Webpack equivalent

The row spec calls these out explicitly because the W14.1 sidecar
runs Vite *and* Bun's Rolldown-flavoured transformer, and operator
projects ship Webpack-based stacks too. The contract for those
sibling plugins is:

1. **Same wire shape.** Re-export
   [`buildErrorPayload`](./index.js) from
   `@omnisight/vite-plugin/runtime` and feed your bundler's error
   objects into it. `kind`, `phase`, and the location triple
   (`file` / `line` / `column`) are the only schema-defined
   compile-time inputs; everything else (`schema_version`, `plugin`,
   `plugin_version`, `occurred_at`) is filled in for you.

2. **Same endpoint.** Use
   [`buildErrorEndpoint(backendUrl, workspaceId)`](./index.js) so the
   path stays canonical (`/web-sandbox/preview/{ws}/error`). The
   backend rejects any other path.

3. **Same best-effort transport.** Reuse
   [`postErrorPayload`](./index.js) (Node 18+ `fetch`) so the
   network failure semantics line up — every adapter swallows fetch
   rejections, returns `{ ok, status, error }`, never throws.

4. **Same runtime overlay.** Use
   [`renderRuntimeOverlayScript({ endpointUrl })`](./runtime.js) and
   inject the returned `<script>` block into every served HTML
   response. The script registers `window.onerror` +
   `unhandledrejection` handlers and POSTs `kind: "runtime"` events
   with `phase: "client"`. Sibling adapters do not need to
   reimplement this — only invoke the renderer from the bundler's
   HTML-template hook (Rolldown's `transformIndexHtml`, Webpack's
   `HtmlWebpackPlugin` `beforeEmit` event, esbuild's
   `onLoad({ filter: /\\.html$/ })`).

5. **Same schema version pin.** Read
   `OMNISIGHT_VITE_ERROR_SCHEMA_VERSION` instead of hard-coding
   `"1.0.0"`. The drift guard test will fail red the moment the
   backend bumps the version and a sibling plugin lags behind.

### Webpack adapter sketch

```js
const {
  buildErrorPayload,
  buildErrorEndpoint,
  postErrorPayload,
  renderRuntimeOverlayScript,
} = require("@omnisight/vite-plugin/runtime")

class OmnisightWebpackPlugin {
  constructor({ workspaceId, backendUrl }) {
    this.endpoint = buildErrorEndpoint(backendUrl, workspaceId)
  }
  apply(compiler) {
    compiler.hooks.failed.tap("OmnisightWebpackPlugin", (err) => {
      const payload = buildErrorPayload({
        kind: "compile", phase: "buildStart", error: err,
      })
      void postErrorPayload({ endpoint: this.endpoint, payload })
    })
    compiler.hooks.compilation.tap("OmnisightWebpackPlugin", (c) => {
      c.errors && c.hooks.afterSeal.tap("OmnisightWebpackPlugin", () => {
        for (const e of c.errors) {
          const payload = buildErrorPayload({
            kind: "compile", phase: "transform", error: e,
          })
          void postErrorPayload({ endpoint: this.endpoint, payload })
        }
      })
    })
    // HtmlWebpackPlugin integration left to the sibling row.
  }
}
```

### Rolldown adapter sketch

Rolldown's plugin contract is the Rollup contract — drop-in with the
same hook names (`buildStart`, `load`, `transform`, `handleHotUpdate`,
`transformIndexHtml`). The current plugin is therefore the Rolldown
plugin too once Rolldown ships its Vite-compatibility shim; the
sibling row is reserved only for the case where Rolldown diverges on
the error-object shape (e.g. `loc` vs `pos`).

## Testing

The plugin is unit-tested in
[`test/web-preview/omnisight-vite-plugin.test.ts`](../../test/web-preview/omnisight-vite-plugin.test.ts).
Tests cover:

* schema version pin (drift guard against the backend literal),
* allowed phase set (drift guard against the backend `_ALLOWED_PHASES`),
* payload-shape validation (kind/phase/file/line/column normalisation),
* UTF-8 truncation respects the 4 KiB / 8 KiB caps,
* endpoint URL construction (`/` trim, `encodeURIComponent`),
* POST best-effort semantics (network failure ⇒ `ok=false`, no throw),
* runtime overlay render (`<script>` injected before `</head>`).

Run with:

```sh
pnpm test test/web-preview/omnisight-vite-plugin.test.ts
```

The backend half is tested in
[`backend/tests/test_w15_1_vite_error_endpoint.py`](../../backend/tests/test_w15_1_vite_error_endpoint.py).
