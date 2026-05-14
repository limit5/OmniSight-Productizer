# docs/audit/codex-reviews/

Persistent storage for codex independent review outputs. **Source-of-truth — `/tmp/` is volatile on WSL** (gets wiped on host reboot; that's what swallowed the 2026-05-13 reviews referenced by S12 phase specs).

## Convention

When codex finishes a review and writes to `/tmp/<name>.txt`, immediately run:

```bash
scripts/save-codex-review.sh --git-add /tmp/<name>.txt
git commit -m "[codex-review] save <name>"
```

Or batch-save everything:

```bash
scripts/save-codex-review.sh --git-add /tmp/*codex*review*.txt /tmp/*audit*.txt
```

Naming: keep the original `/tmp/` basename. The same name in `/tmp/` and here means "this WAS the live review output before the reboot risk".

## What lives here

- **G.A-v* spec reviews** — independent codex review of `docs/sprint-s12/sprint-s12g-*-spec.md` versions (one cycle per v1→v2 amendment).
- **Phase 31.\* spec reviews** — independent codex review of `docs/sprint-s12/phase-31*-ticket-spec.md`.
- **Runner self-audits** — codex critiquing its own runtime (started 2026-05-14; see `docs/codex-review-prompts/2026-05-14-runner-self-audit-and-redesign-prompt.md`).
- **Ad-hoc reviews** — anything else codex produced that informed a spec amendment.

## What does NOT live here

- Codex's per-ticket execution output (those go to the ticket's Gerrit change / commit message, not here)
- Claude's own audits (those live in `docs/audit/` top-level, e.g. `2026-04-27-deep-audit.md`)
- Live runner logs (those live in `docs/audit/runner-incidents/` if archived)

## Provenance trail

Each saved review file is the verbatim output codex wrote to `/tmp/`. The corresponding prompt that codex consumed lives in `docs/codex-review-prompts/`. Together they form a reproducible record: prompt + output. If a future operator wants to re-run a review with a tweaked prompt, the prompt is right there.
