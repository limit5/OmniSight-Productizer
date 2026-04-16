# OpenAPI Contract (N3)

> Keeps the FastAPI backend and the Next.js frontend honest about their
> wire format. Single source of truth: `app.openapi()` inside
> `backend/main.py`. Everything else — the on-disk snapshot, generated
> TypeScript types, MSW fixtures — is a derived artifact that CI
> refuses to accept when it drifts.

## Why

Before N3, the frontend carried its own hand-rolled interfaces in
`lib/api.ts` (≈2 000 lines). If the backend renamed a field or dropped
a response key, the frontend compiled fine and failures surfaced at
runtime on real user traffic.

After N3:

- `openapi.json` at the repo root is a committed snapshot of the
  FastAPI schema.
- `lib/generated/api-types.ts` is generated from that snapshot by
  [`openapi-typescript`](https://github.com/openapi-ts/openapi-typescript).
- `lib/api.ts` imports a narrow set of probe types from the generated
  file (see the bottom of the file, `_N3_Contract*`). If FastAPI
  renames a route or model that the frontend depends on, `tsc --noEmit`
  in the `lint` CI job fails immediately.
- `test/msw/handlers.ts` wraps MSW's `http` with
  [`openapi-msw`](https://github.com/christoph-fricke/openapi-msw) so
  mocked request/response shapes also live under the schema — no more
  "the mock says X, the backend says Y".

## Files

| Path | What |
|---|---|
| `backend/main.py` | Source of truth — `app = FastAPI(...)`. |
| `scripts/dump_openapi.py` | Writes `openapi.json`. Also has `--check` for CI drift detection. |
| `openapi.json` | Committed snapshot. Sorted + indented for readable `git diff`. |
| `lib/generated/api-types.ts` | Auto-generated; do not hand-edit. |
| `lib/generated/openapi.ts` | Thin re-export (`GetResponse`, `PostBody`, schema aliases) — the stable seam the app imports against. |
| `test/msw/handlers.ts` | Contract-aware MSW handlers. Fixture shapes use `satisfies Schemas["X"]`. |
| `test/msw/server.ts` | Node-side MSW server for vitest. |
| `test/msw/openapi-contract.test.ts` | Smoke test that exercises the wire format. |
| `backend/tests/test_openapi_contract.py` | Python-side dump + determinism + snapshot-diff gate. |
| `.github/workflows/ci.yml` → `openapi-contract` job | CI gate: regenerates both artifacts and fails on `git diff`. |

## Developer workflow

**Day-to-day** — if you change a Pydantic model, a route signature, or
add/remove an endpoint:

```bash
pnpm run openapi:sync   # regenerates openapi.json + lib/generated/api-types.ts
git add openapi.json lib/generated/api-types.ts
```

That's it. If you forget, the `openapi-contract` CI job catches you.

**Targeted operations**:

| Command | Effect |
|---|---|
| `pnpm run openapi:dump` | Regenerate `openapi.json` only. |
| `pnpm run openapi:types` | Regenerate `lib/generated/api-types.ts` only (assumes the snapshot is current). |
| `pnpm run openapi:check` | Non-destructive drift check — exits non-zero if the snapshot would change. |

The Python script also works standalone:

```bash
python scripts/dump_openapi.py --check   # exit 1 on drift, prints first 4 kB of diff
```

## How the CI gate works

`.github/workflows/ci.yml → openapi-contract` runs on every push/PR:

1. Install Python + Node deps (uses existing cached lockfiles).
2. Run `python scripts/dump_openapi.py` — overwrites `openapi.json`.
3. Run `pnpm exec openapi-typescript openapi.json -o lib/generated/api-types.ts`.
4. `git status --porcelain -- openapi.json lib/generated/api-types.ts` —
   any output fails the build with a hint to run `pnpm run openapi:sync`.

Breaking-change visibility in PRs is a natural side-effect: because the
snapshot is committed, GitHub's file viewer shows the diff inline.
Reviewers can tell at a glance whether a PR is adding, removing, or
reshaping an endpoint.

## Adding a contract probe for a new load-bearing route

If your feature adds a new endpoint the frontend reads/writes on mount,
extend the tripwire in `lib/api.ts`:

```ts
type _N3_NewRouteResp = _N3_GetResponse<"/api/v1/your/new/route">

export type _N3_ContractProbes = [
  // ...existing probes...,
  _N3_NewRouteResp,
]
```

Leaving the probe in the tuple keeps it load-bearing without exporting
it publicly.

## Writing a new contract test with MSW

```ts
import { describe, it, expect, beforeAll, afterAll, afterEach } from "vitest"
import { server } from "@/test/msw/server"
import { http } from "@/test/msw/handlers"
import { HttpResponse } from "msw"

describe("my new endpoint", () => {
  beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
  afterEach(() => server.resetHandlers())
  afterAll(() => server.close())

  it("renders the list", async () => {
    server.use(
      http.get("/api/v1/your/new/route", () =>
        HttpResponse.json([/* fixture satisfies Schemas["YourModel"] */]),
      ),
    )
    // ...render component, assert, etc.
  })
})
```

`onUnhandledRequest: "error"` is deliberate: any fetch the test fires
that you forgot to mock becomes a loud failure, not silent pass.

## Non-goals

- We intentionally do **not** auto-replace the hand-rolled interfaces
  in `lib/api.ts` (`ApiAgent`, `ApiTask`, etc.) with generated types.
  Full migration is a multi-day refactor; the probe block at the
  bottom of the file is the incremental, load-bearing equivalent.
- MSW is **not** wired into `test/setup.ts` globally — the legacy
  suite uses direct `vi.stubGlobal('fetch', …)` mocks, and switching
  every test over would be orthogonal churn. Contract tests opt in.

## Related

- N1 — Dependency Governance (locks `openapi-typescript`, `msw`,
  `openapi-msw` into `pnpm-lock.yaml`).
- N2 — Renovate auto-PR policy (bumps these deps under the tier
  rules; `openapi-typescript` minor is an auto-merge candidate when CI
  green).
