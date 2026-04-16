/**
 * N3 — openapi-msw contract-aware handlers.
 *
 * `createOpenApiHttp` is a tiny wrapper around MSW's `http` that
 * infers the params / body / response types of every handler from the
 * generated `paths` type. If FastAPI adds, removes, or reshapes an
 * endpoint — and `pnpm run openapi:sync` is re-run — the handlers
 * below stop compiling until the fixtures are updated. That's the
 * "mock stays honest" half of N3's contract test story.
 *
 * Scope: this file is intentionally tiny. It registers handlers for
 * the handful of endpoints the Home/Agents/Tasks pages read on mount,
 * so smoke tests can render without a live backend. Extend as more
 * pages get contract coverage — do NOT turn this into a full fake
 * backend (use real FastAPI in a fixture container for that).
 */
import { createOpenApiHttp } from "openapi-msw"
import { HttpResponse } from "msw"
import type { paths, Schemas } from "@/lib/generated/openapi"

// The generated `paths` keys already include the `/api/v1/...` prefix
// (FastAPI prefixes the v1 router at mount). Leave `baseUrl` empty so
// handler paths below match the `paths` keys verbatim.
export const http = createOpenApiHttp<paths>({ baseUrl: "" })

// ─── Minimal fixtures ────────────────────────────────────────────────
// Keep fixture shapes pinned to the generated schema. `satisfies` here
// turns a missing / renamed field into a compile error, which is the
// whole point of using openapi-msw.

const sampleAgent = {
  id: "agent-sample",
  name: "Sample Agent",
  type: "firmware",
  sub_type: "camera-isp",
  status: "idle",
  progress: { current: 0, total: 0 },
  thought_chain: "",
  ai_model: null,
  sub_tasks: [],
  file_scope: [],
} satisfies Schemas["Agent"]

const sampleTask = {
  id: "task-sample",
  title: "Sample Task",
  description: null,
  priority: "medium",
  status: "backlog",
  assigned_agent_id: null,
  created_at: new Date("2026-01-01T00:00:00Z").toISOString(),
  completed_at: null,
  ai_analysis: null,
  suggested_agent_type: null,
  suggested_sub_type: null,
  parent_task_id: null,
  child_task_ids: [],
  depends_on: [],
  external_issue_id: null,
  issue_url: null,
  external_issue_platform: null,
  last_external_sync_at: null,
  acceptance_criteria: null,
  labels: [],
  npi_phase_id: null,
} satisfies Schemas["Task"]

/** Default handlers for contract-level smoke tests. */
export const handlers = [
  http.get("/api/v1/agents", () => HttpResponse.json([sampleAgent])),
  http.get("/api/v1/tasks", () => HttpResponse.json([sampleTask])),
]

export const fixtures = { sampleAgent, sampleTask }
