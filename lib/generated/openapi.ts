/**
 * N3 — Typed helpers over the generated OpenAPI schema.
 *
 * `api-types.ts` is auto-generated and enormous; consumers should import
 * the narrow aliases from this file instead of reaching into the raw
 * `paths` / `components` shapes. Keeping the surface here thin means the
 * schema file can churn without forcing a ripple of imports across the
 * app.
 *
 * Contract: if a route or model name referenced below disappears, this
 * file stops compiling, which fails `tsc --noEmit` in CI — exactly the
 * "FastAPI changes schema, frontend blows up at compile time" guarantee
 * N3 is meant to give us.
 */
import type { components, paths } from "./api-types"

export type { components, paths } from "./api-types"

/** Convenience: the whole `components.schemas` bag. */
export type Schemas = components["schemas"]

/** Pick a schema by name — e.g. `Schema<"Agent">`. */
export type Schema<K extends keyof Schemas> = Schemas[K]

/**
 * Response body for GET <path> (200 application/json).
 * Usage: `type Agents = GetResponse<"/api/v1/agents">`
 */
export type GetResponse<P extends keyof paths> = paths[P] extends {
  get: {
    responses: {
      200: { content: { "application/json": infer T } }
    }
  }
}
  ? T
  : never

/** Request body for POST <path> (application/json). */
export type PostBody<P extends keyof paths> = paths[P] extends {
  post: {
    requestBody?: { content: { "application/json": infer T } }
  }
}
  ? T
  : never

// ─── Load-bearing type aliases used by lib/api.ts ───────────────────
//
// These aliases act as compile-time tripwires: when the FastAPI side
// renames or reshapes any of these models, this file stops compiling
// and `tsc --noEmit` in the `lint` CI job fails. That's the
// "FastAPI schema change → frontend editor squiggles" UX N3 promised.
//
// If you need to expose more of the schema to the app, add an alias
// here instead of importing the raw `components` type everywhere —
// keeps the blast radius of future schema churn contained.

export type AgentSchema = Schema<"Agent">
export type TaskSchema = Schema<"Task">
export type TaskStatusSchema = Schema<"TaskStatus">
export type AgentStatusSchema = Schema<"AgentStatus">
