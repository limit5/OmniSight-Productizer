# OP-789 Generated Docs Audit

Status: completed 2026-05-08.

Scope: generated documentation files and generated documentation-adjacent
files found by grepping for `AUTO-GENERATED`, `auto-generated`,
`DO NOT EDIT`, known builder scripts, OpenAPI builders, and docs-site
migration markers. Frontend route-map migration is out of scope for OP-789
because this ticket explicitly forbids frontend changes.

| Candidate | Source / builder | Disposition | Reason |
|---|---|---|---|
| `docs/adr/README.md` | `backend.docs_site_adr.build_adr_index()` | already-migrated | OP-788 moved the ADR index to docs-site build time; OP-790 ignores the generated output. |
| `docs/sop/lessons-learned.md` | `scripts/build_lessons_index.py` | already-migrated | Per-lesson files under `docs/sop/lessons/` are the source of truth; OP-790 keeps the aggregate ignored and untracked. |
| `docs/status/handoff_status.yaml` | `scripts/extract_handoff_status.py` | already-migrated | The manifest is generated from frozen legacy handoff data and is ignored/untracked after OP-790. |
| `docs/agents/tool-reference.md` | `backend.agents.tool_schemas.generate_markdown_reference()` | migrate-to-docs-site | Deterministic registry-derived reference; OP-789 removes the committed output, adds `.gitignore`, and exposes `backend.docs_site_tool_reference.build_tool_reference()` for build-time rendering. |
| `docs/architecture.md` | `backend/self_healing_docs.py` rewrites only the sentinel block | keep-in-git | Mixed manual architecture notes plus generated API block; keeping the file preserves the hand-written wrapper and existing self-healing docs contract. |
| `docs/architecture/agents/*.md` | `scripts/batch_arch_notes.py` one-off Anthropic batch collect | keep-in-git | These are AI-authored architecture notes, not a deterministic local generated output; no safe docs-site builder exists to recreate them offline. |
| `openapi.json` | `scripts/dump_openapi.py` / `backend.self_healing_docs` | keep-in-git | This is the API contract snapshot used by CI and downstream type generation, not only a docs-site page. |
| `lib/generated/api-types.ts` | `pnpm exec openapi-typescript openapi.json` | keep-in-git | Generated client type surface consumed by application code; migration would touch frontend/generated-client workflow outside OP-789 scope. |
| `lib/generated/openapi.ts` | Hand-written narrow aliases over generated OpenAPI types | keep-in-git | Documentation-adjacent but not generated; it is the stable import facade for application code. |
| `lib/generated/README.md` | Static README for generated client directory | keep-in-git | Small static explanatory file; not generated and no migration needed. |

Selected migration: `docs/agents/tool-reference.md`.

Migration evidence:

- Dynamic build entry point: `backend.docs_site_tool_reference.build_tool_reference()`.
- Git hygiene: `docs/agents/tool-reference.md` is ignored and untracked.
- Drift contract: `backend/tests/test_tool_schemas.py` verifies the dynamic
  docs-site output comes directly from the tool schema registry.
