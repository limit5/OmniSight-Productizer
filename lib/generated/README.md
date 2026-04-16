# `lib/generated/`

Auto-generated from `openapi.json` at the repo root. **Do not hand-edit.**

- `api-types.ts` — produced by `openapi-typescript` from the snapshot.
- `openapi.ts` — small hand-written re-export helpers used by `lib/api.ts`
  and contract tests. Keep this file focused on type aliases (no runtime
  logic) so it stays cheap to touch when the schema changes.

## Regenerate

```bash
# refresh both the snapshot and the TS types
pnpm run openapi:sync
```

Under the hood that runs:

```
python scripts/dump_openapi.py          # writes openapi.json
pnpm exec openapi-typescript openapi.json -o lib/generated/api-types.ts
```

CI's `openapi-contract` job enforces that both files stay in sync with
the FastAPI app — any drift fails the build. See
[`docs/ops/openapi_contract.md`](../../docs/ops/openapi_contract.md).
