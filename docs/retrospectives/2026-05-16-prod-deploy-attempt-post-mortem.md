# Retrospective: 2026-05-16 Prod Deploy Attempt — Two Independent Failures + Future-Proofing

**Date**: 2026-05-16 → 2026-05-17
**Type**: Incident retrospective (cross-ticket synthesis for the deploy-recovery scope)
**META ticket**: OP-1186 (this retro)
**Sibling META tickets**:
- OP-1176 — Prod alembic drift recovery (0202 → develop heads)
- OP-1180 — main fast-forward to develop tip
- OP-1183 — Backend redeploy from main + merger-bot activation verification

**Child tickets referenced**: OP-1177, OP-1178, OP-1179, OP-1181, OP-1182, OP-1184,
OP-1185, OP-1187, OP-1188, OP-1189, OP-1190, OP-1191, OP-1192, OP-1193, OP-1194,
OP-1195, OP-1196, OP-1197

**Per-file lessons distilled from this incident**:
- `docs/sop/lessons/L-OP-1176-prod-alembic-drift-hidden-by-livez-routing.md`
- `docs/sop/lessons/L-OP-1186-api-keys-duplicate-key-lookup-index-blocks-backfill-migration.md`

**Author**: Claude (interactive runner pickup on OP-1186) — synthesis only;
underlying recovery work was done by Claude (interactive session) + sora
(operator) during the 2026-05-16 deploy window.

**Status**: Recovery shipped 2026-05-16 23:57 (prod backend on
`main-3967b980`, both replicas green on `/livez` AND `/readyz`); META
tickets OP-1180/1183 close-out tracked separately; this retro
captures the lessons + hardening roadmap.

---

## Situation

The 2026-05-16 deploy window was an attempt to land OP-718 (merger-bot
HTTP delegation, already merged into develop) on prod, plus
incidentally fast-forward main to develop tip after a 399-commit
develop→main gap. The chosen path was a develop-direct deploy via
`scripts/deploy-prod.sh`. The deploy hit **two independent failure
modes in sequence**, neither of which was caught by any pre-flight:

1. **Failure A — Alembic graph KeyError**: `alembic upgrade 0202 →
   heads` traversed ~33 linear migrations successfully, then crashed
   at the `m_2026_05_16_3head` merge node with `KeyError: '0203'` from
   `head_maintainer.remove`. The merge node's `down_revision` listed
   `("0203", "0237", "m_audit_29_final")`, but by the time it was
   applied, `0203` had been transitively absorbed via `0237`'s chain
   and was no longer in the in-memory head-set. Full mechanism in
   `L-OP-1176`. Fix shipped as OP-1189 (Gerrit #710) + cherry-pick
   to main as OP-1182 (Gerrit #711).

2. **Failure B — api_keys.key_lookup_index UniqueViolation**: after
   the graph hot-patch, the same deploy hit `0203a_kse_envelope_
   credential_backfill._backfill_api_keys()` setting
   `key_lookup_index = sha256(legacy_secret)[:12]` on an old
   UUID-derived legacy-stub row whose hash collided with an already
   canonicalised `ak-legacy-31be6f6c8763` row. PG raised
   `UniqueViolation` on `idx_api_keys_lookup`; alembic aborted. Full
   mechanism in `L-OP-1186`. Fix tracked across OP-1177 / OP-1178 /
   OP-1179.

The two failures share no mechanism. Failure A was a graph-hygiene
bug introduced by an earlier merge-resolution commit (OP-1164);
Failure B was a stale data condition predating the migration by
~9 months. They were chained only because they happened in the same
develop-direct deploy attempt.

Compounding context: the prod backend image had been silently
returning `/readyz=503` for ~48 hours before this deploy began,
because Caddy's upstream health-check was wired to `/livez` (cheap
pulse) instead of `/readyz` (DB + migrations probe). The drift
window was therefore *invisible* to every load-balancer signal and
to every Discord alert that derives from upstream health. This is
the same Caddy/`/livez` design flaw that the 2026-05-14 host-reboot
retro (`docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`)
flagged as ⑥ "Image-vs-DB alembic head drift — design blind spot"
and recommended fixing — the recommendation was not yet acted on
when the 2026-05-16 deploy attempt began.

## Timeline (relative)

```
t=−48h  prod DB had been advanced through some path other than the
        running image's `alembic upgrade head` (likely cross-branch
        merge work on develop tip); DB at 0202, image bundled
        migrations top out at 0200 → /readyz=503 thereafter
t=−48h  Caddy /livez=200 keeps both replicas "healthy" to the load
        balancer; no alert fires; no Discord page; operator
        unaware
t=−3h   OP-718 deploy attempt: `deploy-prod.sh` fetches develop tip
        e5549759, builds backend image, expects alembic head
        `m_2026_05_16_3head`
t=−2h   `alembic upgrade 0202 → heads` runs ~33 linear migrations OK,
        then hits Failure A (KeyError: '0203') at the merge node →
        emergency hot-patch OP-1189; rebuild image; retry
t=−1h   Migration restarts from where it left off, hits Failure B
        (UniqueViolation on idx_api_keys_lookup) at 0203a_kse →
        manual data dedupe; retry
t=0     Both backends rolling-restarted onto image
        `ghcr.io/your-org/omnisight-backend:main-3967b980`; both
        replicas report /livez=200 AND /readyz=200; prod
        alembic_version=m_2026_05_16_3head; deploy window closes at
        2026-05-16 23:57 CST
```

## Verification (snapshot at recovery close)

| Signal | Value | Source |
|---|---|---|
| prod backend image | `ghcr.io/your-org/omnisight-backend:main-3967b980` | OP-1184 deploy comment |
| build provenance | `git_ref=main`, `image_sha=3967b9809f…`, `alembic_head_in_image=m_2026_05_16_3head` | container `MANIFEST.json` |
| `/livez` both replicas | 200 | curl from operator + Caddy upstream check |
| `/readyz` both replicas | 200 | curl from operator (manual; Caddy still on `/livez`) |
| prod `alembic_version` | `m_2026_05_16_3head` | psql against omnisight-pg-primary |
| Gerrit Change #710 (OP-1189, develop) | Merged with human +2 from `sora` | Gerrit log |
| Gerrit Change #711 (OP-1182, main release-cut) | Merged with human +2 from `sora` | Gerrit log |
| develop→main delta | 0 commits | `git rev-list --count refs/remotes/gerrit/main..refs/remotes/gerrit/develop` |
| `prod-omnisight-2026-05-16-pre-develop-deploy.sql` backup | ~2 MB, retained on operator host | `/home/user/work/sora/backups/` |

## Hypothesis evolution during diagnosis

| Operator hypothesis (in order encountered) | Verdict | Notes |
|---|---|---|
| "alembic merge node syntax bug — re-author the merge to omit 0203" | ✅ Correct for Failure A | OP-1189 hot-patch landed; root cause is that `down_revision` must list current-and-mutually-unreachable heads at *apply* time, not at *commit-authoring* time. Pre-flight: `alembic heads` against the parent commit |
| "the api_keys collision is a fresh dupe from concurrent writes" | ❌ Wrong | The duplicate predates the migration; one row is a Task-#106-era UUID-derived legacy stub, the other is the canonical `ak-legacy-<sha256>` form. Both share the same `legacy_secret` plaintext |
| "the backfill migration is wrong; promote the index later" | 🟡 Partial | Promoting the unique index later avoids the immediate crash but only defers the symptom. Real fix is to make the backfill dedupe defensively (OP-1178) |
| "deploy-prod.sh `--sql` dry-run should have caught the collision" | ❌ Wrong | `--sql` emits the DDL/DML alembic would run; it does not execute against real data, so duplicate-row collisions never surface. Real pre-flight is "restore the prod backup into a throwaway PG, run `alembic upgrade heads` against it." OP-1188 tracks the runbook + script ergonomics |
| "OP-1035 per-develop-commit image pipeline should have caught the drift" | ❌ Wrong | OP-1035 builds the image; it does not deploy it. The consumer side (auto-pull / auto-redeploy / drift detector) is still unbuilt — same gap the 2026-05-14 retro flagged as ⑤ + ⑥ |

## What worked

- **PG backup before any DDL**. The `pg_dump
  prod-omnisight-2026-05-16-pre-develop-deploy.sql` taken *before*
  the first alembic attempt was the safety net that let the operator
  retry the migration after each failure without fear of
  unrecoverable loss. Without it, neither hot-patch nor manual
  data dedupe would have been attempted under deploy-clock pressure.
- **Filing during the incident**. The deploy-recovery scope label
  (`scope:deploy-recovery-2026-05-16`) and the META + 4-AC
  filing discipline meant by 2026-05-17 morning the retrospective
  surface area was already enumerated as 22 tickets, not "a vague
  blob of stuff that happened yesterday." This made post-mortem
  authoring (this doc + the two lessons) tractable.
- **Per-file lesson discipline**. Writing L-OP-1176 + L-OP-1186 as
  two separate lessons (one per root cause) preserves the
  mitigations as distinct guidance rather than collapsing them
  into "the 2026-05-16 deploy was bad." Future readers searching
  for "alembic merge node KeyError" or "api_keys backfill collision"
  will find the right lesson.
- **OP-718 merger-bot landed safely despite the chaos**. The
  develop-direct deploy did ultimately ship OP-718; the merger-bot
  end-to-end validation (OP-1185) is still pending but the code is
  live in prod and instrumented for the upcoming synthetic-conflict
  test.

## What did not work

- **`/livez`-only Caddy routing masked the 48-hour drift window**.
  The 2026-05-14 retro already identified this as gap ⑥ "Image-vs-DB
  alembic head drift — design blind spot." The recommendation was
  to either (a) flip Caddy upstream `health_uri` from `/livez` to
  `/readyz`, or (b) ship a separate drift monitor with its own
  alert. Neither was done by 2026-05-16. The 2026-05-16 deploy
  was effectively a forced confrontation with a problem that had
  been silently present for 2 days.
- **`alembic upgrade --sql` dry-run as a pre-flight**. The operator
  ran the dry-run before the deploy and got clean output; the
  collision was invisible because `--sql` does not execute against
  real data. This is a dry-run-vs-real-run asymmetry the runbook
  did not warn about.
- **`-e OMNISIGHT_IMAGE_TAG=<tag>` on `docker run`**. The session
  burned ~20 minutes when a throw-away migration container ran the
  wrong image because `docker compose up -d` reads only the `.env`
  file and ignores container-side `-e` flags for the image-name
  resolution step. Fix is `sed -i s|OMNISIGHT_IMAGE_TAG=.*|...|`
  the `.env` first, then `docker compose up -d`. Captured in
  L-OP-1176 generalisation.
- **OP-1188's runbook ergonomics work was not yet shipped before
  the deploy began**. `deploy-prod.sh` hardcodes `origin` for fetch
  but `origin` is the GitHub mirror (severely stale); the operator
  worked around this with `--gerrit-source` / explicit fetch by
  hand. Acceptable in an experienced operator's hands; intolerable
  if a future on-call who has never run a manual deploy attempts
  the same.

## Future-proofing punch list

Ordered by the marginal alert / pre-flight signal they would have
added BEFORE the 2026-05-16 deploy began. Each item names a JIRA
ticket where the work belongs; this retro does NOT file new tickets,
it only catalogues what should be picked up (per §11 / §14 discipline).

| # | Hardening item | Belongs to | Status today | Marginal value if it had existed on 2026-05-14 |
|---|---|---|---|---|
| 1 | Flip Caddy upstream `health_uri` from `/livez` to `/readyz` for prod backend pool | New ticket (area:devops, NOT this META — surrender per §11 if attempted from this scope) | Not filed | Would have surfaced the 48h drift window within seconds of it opening; Caddy would have stopped routing traffic to the failing replica and the LB-state change would have been visible to whatever ops dashboard watches Caddy upstreams |
| 2 | Routine-scrape `readyz_migrations_pending` Prometheus gauge + Alertmanager rule on `alembic_drift > 0 for >5m` | 31.G Wave (Sprint S12 — already scoped) | Scoped, not picked up | Would have paged on-call ~5 min into the drift window via Discord webhook bridge |
| 3 | `alembic upgrade heads` against a `pg_dump`-restored throwaway PG as part of `deploy-prod.sh` pre-flight (true data-side dry-run) | OP-1188 | Filed (公開済み) | Would have surfaced BOTH Failure A (graph KeyError) AND Failure B (UniqueViolation) before any prod DDL ran |
| 4 | Patch `0203a_kse` (and pattern-match other backfills) to dedupe defensively before unique-index promotion | OP-1178 | Filed (To Do) | Would have made Failure B raise a structured operator message instead of a raw `UniqueViolation`, and would have dedupe-or-bail in the same transaction |
| 5 | Auto-pull + auto-redeploy on new `:latest` digest (the consumer side of OP-1035) | New ticket (area:devops) | Not filed; same gap the 2026-05-14 retro flagged as ⑤ | Would have closed the "image stale for 7 days, then DB advances under it" loop entirely by making prod and develop tip not co-exist for more than minutes |
| 6 | Pre-flight `alembic heads` against parent commit at merge-node authoring time (lint / CI hook) | New ticket (area:tooling) | Not filed | Would have caught the L-OP-1176 merge-node bug at author time, not deploy time |
| 7 | `deploy-prod.sh` `--gerrit-source` flag + auto-detect that `origin` is the stale GitHub mirror | OP-1188 | Filed (公開済み) | Removes a foot-gun that costs ~10 min per fresh-operator deploy |
| 8 | Document "manual deploy protocol" (pg_dump + .env-not-`-e` + one-replica-at-a-time + curl /readyz manually) as a checklist in `docs/sop/deploy-prod-runbook.md` | OP-1188 (scope: runbook) | Filed (公開済み) | Codifies what L-OP-1176 and this retro both reference; removes "operator has to remember 8 things" failure mode |

Items 1, 5, and 6 are **discovered dependencies** from the perspective
of this retro — they belong to area:devops / area:tooling and cannot be
filed from inside the OP-1186 META scope (capability matrix permits
area:docs only). They are catalogued here so the operator can decide
where to file them; per §11 the runner does not self-add them.

## Cross-reference: how this incident relates to the 2026-05-14 retro

The 2026-05-14 host-reboot retro identified 9 gaps; the 2026-05-16
deploy incident exercised exactly the gaps the earlier retro flagged
as `🔴 None` / `🟡 Weak`:

| 2026-05-14 retro gap | 2026-05-16 incident manifestation |
|---|---|
| ⑤ Shipped-but-not-deployed runtime detector (weak) | Image 7 days behind DB; no monitor flagged it before 2026-05-16 |
| ⑥ Image-vs-DB alembic head drift (design blind spot) | 48 h of `/readyz=503` invisible because Caddy on `/livez`; root mechanism unchanged |
| ⑦ Auth middleware half-done / `/health` 401 | Unchanged; no consumer relied on it during 2026-05-16 |
| ⑧ WSL2 graceful shutdown / docker 30s timeout | Not triggered (deploy was operator-initiated, not host-event-driven) |
| ⑨ Sister-project `ai_gateway` orphan | Unchanged; out of Productizer scope |

The two incidents together (2026-05-14 + 2026-05-16) are the
strongest joint signal that the **Caddy `/readyz` flip + drift
monitor + auto-pull/redeploy** trio is the highest-leverage
hardening on the table. Each one has been independently flagged
twice now. The next deploy that catches the same drift in flight
without these is a self-inflicted incident.

## Filed-as

- `docs/retrospectives/2026-05-16-prod-deploy-attempt-post-mortem.md` (this file)
- `docs/sop/lessons/L-OP-1176-prod-alembic-drift-hidden-by-livez-routing.md` (already shipped via OP-1187)
- `docs/sop/lessons/L-OP-1186-api-keys-duplicate-key-lookup-index-blocks-backfill-migration.md` (new this ticket)
- META ticket: OP-1186 (this retro). Label `meta:retrospective` per §14;
  the META `scope:deploy-recovery-2026-05-16` ties the 22 deploy-recovery
  tickets together.

## What this retrospective is NOT

- Not a tier-drift retro per §14 — OP-1186 is `tier:M` and the work was
  within the META envelope; no estimate-to-actual ratio applies.
- Not a re-write of L-OP-1176 — that lesson stands as the canonical
  reference for the alembic-graph + `/livez`-routing root causes. This
  retro synthesises across both root causes and adds the
  future-proofing punch list.
- Not a filing of follow-up tickets — punch-list items 1, 5, 6 are
  out of OP-1186's area (`docs` only). They are catalogued for operator
  decision, not auto-filed.
