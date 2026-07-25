# Backup DLP — reviewing `claude_memory_versions.body`

**Ticket:** OP-2729 · **Scope label:** `scope:failed-units-2026-07-25`

## Why this gate needs a human, and why it cannot be a regex

`claude_memory_versions.body` holds operator engineering notes that by design *discuss* this
stack's own topology — `pg-primary`, `ai_cache`, throwaway loopback test DSNs. From the leg-3 prod
ingest (2026-07-23) onward, the backup DLP scanner correctly flagged them and, being fail-closed,
shredded the plaintext dump and produced **no encrypted backup at all** for three nights.

A content-pattern scanner cannot distinguish "discusses X" from "leaks X" — architecture
anti-pattern **#11** at the data layer. Three rounds of adversarial review rejected every attempt
to fix that with better patterns, and the error was the same shape each time: a gate that says
*"release unless my patterns detect a credential"* is only as sound as its detector is
**complete**, and no regex set is complete over free-form prose. Concretely, the classifier misses
`postgresql+asyncpg://user:pw@host` and `redis://:pw@host` (its DSN rule needs a bare scheme
**and** a non-empty user — and `+asyncpg` is this repo's own documented DSN form), it has **no
password-assignment rule at all**, and `.pgpass`, JSON, YAML folded scalars, `curl -u`, `-p<pw>`
and line-split URIs all escape it.

So release is keyed on **a human having reviewed the exact body**, identified by sha256. Any edit
is a new digest and therefore a new review. Completeness stops being a requirement.

## What the gate will and will not release

Reviewing a body releases **only** `pg_internal`, `ai_internal` and `database_url`. Strong-format
secret labels — `anthropic`, `github_pat`, `aws_secret`, `private_key_block`, `jwt`, … — block
**even on a reviewed body**, because a human can miss an `sk-ant-` in a 264 KB note and a regex
cannot. That bounds this design's residual risk, which is reviewer error.

## The recurring task

The nightly backup will block whenever a reviewed body changes or a new body starts firing one of
those labels. That is intended. `omnisight-prod-backup.service` has an `OnFailure=` alert (OP-2728),
so it arrives as a JIRA issue rather than as silence.

**1 — get the candidates, with review hints:**

```sh
cd /home/user/omnisight-prod
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  --entrypoint python3 backend-a \
  /app/scripts/backup_dlp_scan.py --postgres-tmp-db omnisight --emit-reviewed-digests
```

Each line is `<sha256>  # table.column rowid=… labels=…` plus `HINTS:` where the body contains a
shape worth reading twice — a **non-loopback** credential URI (any scheme, including `+driver` and
empty-user forms), a password-like assignment, or a private-key block. The hints never gate
anything; they only aim your attention.

**2 — actually read each body before approving.** Printing a digest is not approval. A hint means
read that part twice; *no* hint does not mean safe, only that those particular shapes are absent.
Pull a body read-only with:

```sh
docker exec omnisight-pg-primary psql -U omnisight -d omnisight -tA -c \
  "select body from claude_memory_versions where body_sha256 = '<digest>'"
```

**3 — paste approved digests** into `~/.config/omnisight/backup-dlp-reviewed-bodies.txt`, one per
line, with a `#` comment naming the slug and, for any hinted body, *why* the hint was benign.

⚠ **The file must be world-readable (`chmod 644`).** The scan runs inside the backend container as
uid **65532**; a `0600` file owned by uid 1000 is unreadable there, the digest set comes back
empty, and the gate blocks everything with no obvious cause. The file holds digests and slug names
— no secrets — so `0644` is appropriate. Keep the parent directory `0700`.

**4 — verify before trusting the next nightly run** (read-only, no writes to prod):

```sh
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  --volume ~/.config/omnisight/backup-dlp-reviewed-bodies.txt:/tmp/omnisight-dlp-reviewed-bodies.txt:ro \
  --env OMNISIGHT_DLP_REVIEWED_BODIES=/tmp/omnisight-dlp-reviewed-bodies.txt \
  --entrypoint python3 backend-a \
  /app/scripts/backup_dlp_scan.py --postgres-tmp-db omnisight --json
```

Expect `"passed": true, "total_findings": 0`. (`backup_prod_db.sh` performs the same mount and env
wiring itself; the explicit flags above are only needed for a manual rehearsal.)

## Where the allowlist lives, and why not in the repo

`~/.config/omnisight/backup-dlp-reviewed-bodies.txt`, **outside** the release artefact. Prod runs
from a release-pinned checkout, so keeping reviewed digests in a tracked file would dirty that
checkout on every review — and a dirty tree hard-fails `deploy-prod.sh` **and emergency digest
rollback**. It is also deliberately *not* placed in `/etc/omnisight` on the host side: that path is
already bound to the deploy overlay directory, and putting operator review state inside
deploy-managed state is exactly the invisible coupling this change exists to remove.

A missing or unreadable file yields an **empty** set: nothing is released and the gate keeps
blocking. Absent evidence of review is never treated as review.

## Review of record — 2026-07-25

Fifteen bodies (14 slugs; `project_leg2_worker_loop_audit` contributes r1 and r2) were reviewed and
approved. Method: scan every candidate for the shapes the classifier provably misses. Result: no
non-loopback credential URI, no private-key block, and no strong-format label in any of them. The
only DSNs present are four throwaway loopback test containers (`localhost:55440`, `localhost:5434`,
`127.0.0.1:5455`, `localhost:5455`). Three bodies carried a password-like-assignment hint; all
three were read in context and are false positives — a `cosign-password.txt` **file path**, a
Python `token=set(effective)` **ContextVar**, and a prose list of **rejected** principal prefixes
(`apikey:/-bot/ci-/ai-`).

⚠ Not approved here, and not reachable by this gate: four other rows carry
`oauth2:<token>@sora.services` GitLab push tokens (one over plain `http://`). They fire **no**
blocking label at all, so they pass the DLP gate today without review. Tracked on **OP-2730**.
