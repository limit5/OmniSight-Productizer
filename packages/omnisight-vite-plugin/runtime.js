/**
 * @omnisight/vite-plugin/runtime — re-export of the runtime overlay
 * script renderer so consumers that already build their own HTML can
 * drop the script tag in by hand without pulling the full plugin
 * factory.
 *
 * Usage (Rolldown / Webpack siblings):
 *
 *     import { renderRuntimeOverlayScript } from "@omnisight/vite-plugin/runtime"
 *
 *     // Inside the bundler's `transformIndexHtml` / template hook:
 *     html = html.replace("</head>", renderRuntimeOverlayScript({
 *       endpointUrl: process.env.OMNISIGHT_BACKEND_URL +
 *                    "/web-sandbox/preview/" + process.env.OMNISIGHT_WORKSPACE_ID +
 *                    "/error",
 *     }) + "</head>")
 *
 * Wire shape lives in ../index.js — bump
 * `OMNISIGHT_VITE_ERROR_SCHEMA_VERSION` there in lock-step with the
 * backend's `WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION` literal.
 */

export {
  renderRuntimeOverlayScript,
  buildErrorPayload,
  buildErrorEndpoint,
  postErrorPayload,
  truncateUtf8,
  extractErrorLocation,
  OMNISIGHT_VITE_ERROR_SCHEMA_VERSION,
  OMNISIGHT_VITE_PLUGIN_NAME,
  OMNISIGHT_VITE_PLUGIN_VERSION,
  ALLOWED_PHASES,
  STACK_TRACE_MAX_BYTES,
  MESSAGE_MAX_BYTES,
} from "./index.js"
