# Generated Docs Inventory — OP-871

**Date**: 2026-05-11
**Scope**: tracked files returned by the required generated-marker grep.
**Command**:

```bash
git ls-files | xargs grep -l 'AUTO-GENERATED\|DO NOT EDIT\|This file is generated'
```

## Summary

- **Files returned by required command**: 13
- **Class (a), needs migration to E3/E4 pattern**: 0
- **Class (b), build artifact / ignore candidate**: 1
- **Class (c), intentionally tracked / not a generated output**: 12

No class (a) file beyond the already-handled E3 lessons index and E4 ADR README
was found, so OP-871 does not need to file additional migration tickets.

## Classification Key

- **(a) migrate to E3/E4 pattern**: deterministic generated documentation that
  should move to source-per-file or docs-site build-time rendering.
- **(b) build artifact**: generated output that should not be tracked; should be
  ignored and removed from git in a cleanup ticket.
- **(c) intentionally tracked**: source, tests, UI copy, vendored/generated
  contract snapshots, or documentation whose marker text is explanatory rather
  than evidence that the file itself is disposable generated output.

## Inventory

| File | Marker evidence | Source / owner | Class | Decision |
|---|---|---|---|---|
| `HANDOFF.md` | Lines containing historic `AUTO-GENERATED` / `DO NOT EDIT` references inside frozen handoff entries. | Legacy archive, frozen since 2026-05-06. | (c) intentionally tracked | Keep. The file is an archive and the command hit is embedded historical prose, not a generated-file header. |
| `TODO.md` | Historic row text references generated architecture and Vite bootstrap markers. | Legacy task archive / runner marker source. | (c) intentionally tracked | Keep. The matches are task descriptions and evidence logs, not generated output. |
| `backend/security_hardening.py` | `render_acl_file()` emits `# DO NOT EDIT BY HAND` inside generated Redis ACL text. | Backend source renderer. | (c) intentionally tracked | Keep. The Python module is source code; only its rendered ACL output is generated. No docs migration candidate. |
| `backend/self_healing_docs.py` | Defines architecture sentinels and scaffold text for generated API block. | Backend source renderer for OpenAPI / architecture docs. | (c) intentionally tracked | Keep. Source code owns the generator; not itself generated. |
| `backend/tests/test_docs_site_lessons.py` | Test literals for the old lessons index `AUTO-GENERATED` banner. | Docs-site regression tests. | (c) intentionally tracked | Keep. Test fixtures intentionally contain the old banner to prove the dynamic index removed it. |
| `backend/tests/test_w15_5_vite_config_injection.py` | Assertion that rendered bootstrap contains `DO NOT EDIT BY HAND`. | Backend test coverage for scaffold output. | (c) intentionally tracked | Keep. Test source is not generated. |
| `backend/web/vite_config_injection.py` | Template literal for generated scaffold file includes `DO NOT EDIT BY HAND`. | Backend scaffold renderer. | (c) intentionally tracked | Keep. Source template is intentionally tracked; rendered downstream project files are generated outside this repo. |
| `components/omnisight/source-control-matrix.tsx` | UI label `AUTO-GENERATED STRUCTURE:`. | Frontend component. | (c) intentionally tracked | Keep. This is user-visible copy for a preview pane, not a generated-file marker. |
| `docs/architecture.md` | Lines 3-8 describe mixed manual/generated content; lines 14-742 are sentinel-delimited generated API surface. | `backend/self_healing_docs.py`. | (c) intentionally tracked | Keep for now. This is `GeneratedFileMixedContent`, but OP-789 already decided to keep the mixed manual wrapper plus generated API block in git to preserve the self-healing docs contract. If that contract changes later, split the generated API block into docs-site build output under a separate ticket. |
| `docs/design/fx-9-10-large-file-module-split.md` | Design text mentions generated `lib/generated/api-types.ts` and its header. | Design document. | (c) intentionally tracked | Keep. Marker appears as explanatory design prose, not because this file is generated. |
| `docs/operations/generated-docs-audit-op789.md` | Prior audit lists generated-doc search markers and dispositions. | Audit document. | (c) intentionally tracked | Keep. Marker appears in audit methodology; this file is hand-authored audit evidence. |
| `messages/en.json` | UI string `AUTO-GENERATED PASSWORD`. | Localisation copy. | (c) intentionally tracked | Keep. User-facing copy, not a generated-file header. |
| `repomix-output.xml` | Header says `This file is a merged representation... by Repomix`; embedded source includes `AUTO-GENERATED STRUCTURE:`. | Repomix packed-code output. | (b) build artifact | Cleanup candidate. `.gitignore` already contains `repomix-output.xml`; the file remains tracked and should be removed from git in a non-docs cleanup ticket if E6 covers generated artifact pruning. |
| `scripts/extract_handoff_status.py` | `_HEADER_COMMENT` renders `# AUTO-GENERATED FROM HANDOFF.md — DO NOT EDIT BY HAND.` | Script source for generated status manifest. | (c) intentionally tracked | Keep. The script is source; its output `docs/status/handoff_status.yaml` was already migrated/ignored per OP-789. |

## Migration Candidates

No class (a) migration candidates were found beyond the already-known E3/E4
pattern work:

- E3 lessons learned aggregate: already represented by per-lesson files and a
  build-time/dynamic index.
- E4 ADR README: already represented by docs-site build-time rendering.

`docs/architecture.md` is the only mixed-content documentation file found by
the required command. It is flagged as `GeneratedFileMixedContent`, but the
current disposition remains intentionally tracked because the generated block is
part of the existing self-healing docs contract documented in OP-789.

## Follow-Up Tickets

None required for class (a): no additional docs migration candidate was found.

Recommended non-class-(a) cleanup for E6 or a tooling/devex ticket:

- Remove tracked `repomix-output.xml` from git while keeping the existing
  `.gitignore` entry. This is a build artifact cleanup, not an E3/E4 docs
  migration.

## Evidence

- Required inventory command output: 13 tracked files returned.
- Prior generated-docs decision source: `docs/operations/generated-docs-audit-op789.md`
  classifies `docs/architecture.md` as keep-in-git and records the E3/E4
  migrations already handled.
- Ignore evidence: `.gitignore` already lists `repomix-output.xml` under the
  repomix section.
