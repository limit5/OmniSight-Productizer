# Ticket-filing draft — DSAR stories C … G

**Date:** 2026-07-26 · **Design:** `2026-07-26-dsar-subject-ownership-epic-design.md` (v6) ·
**META:** OP-2747 · **Scope label:** `scope:failed-units-2026-07-25` · **Draft revision:** v3

A, B, H are filed (OP-2745, OP-2746, OP-2749). This draft covers the rest.

**Draft v1 was NOT-READY** — 5 CRITICAL, 8 HIGH. Three of those were my own recurring failure modes,
recorded here because the fix is structural rather than a wording change:

1. **"0 skipped" is a human instruction, not a gate.** I was bitten this epic by verify commands that
   pass with every decisive test skipped, and my correction was to *write "0 skipped" in the AC* —
   the same class of error one level up. Fixed properly below with a fixture that **raises**.
2. **An AC that cannot execute.** D's decisive test said "one UPDATE performs the transition *and*
   sets `enabled = 1`" — but the design had just moved erasure state into a separate registry, so no
   single `users` UPDATE can do both. Second time this epic that an AC asserted something the schema
   cannot express.
3. **A corrected claim reintroduced downstream.** §1a had already been corrected to stop saying the
   `dek_ref` overwrite makes ciphertext unrecoverable (MVCC keeps the old tuple; WAL keeps it too).
   The ticket draft then restated the original wrong claim as E's rationale.

**Draft v2 was also NOT-READY** — and three of its four new CRITICALs were defects *the split
itself* introduced, which is the risk of splitting a ticket to make it reviewable:

4. **A seam that is not revertible.** D-a backfilled the non-cascading registry; if D-b never landed
   that is new orphaned personal data with no retention enforcement, and once D-b drops the FKs they
   cannot be restored over records whose parents are gone.
5. **An intermediate ticket that leaves the system worse than before it.** Erasure today rolls back
   atomically on failure (`privacy.py:411-428`). D-b commits `subject = erasing` before child DML —
   so a crash after the claim strands a subject in `erasing` **forever**, blocked by the barrier,
   with no worker to resume it. No ticket owned that worker.
6. **Two tickets requiring opposite orderings.** E deleted the local OAuth row atomically with the
   snapshot; G2 required contacting the IdP *before* that row was deleted.

**All tickets are `issuetype = Story`** (hard-coded, `file_jira_ticket.py:311`; runner JQL excludes
Task) and **`tier:X`** — they change the erasure contract, the schema, and a shared production
cluster that co-tenant projects use. Stage 5 (runner blind-test) is **N/A**: it exists to catch
goal-drift in an `agent:auto` runner picking a ticket up cold, and no runner is in the loop.
Areas are drawn from `VALID_AREAS` (`file_jira_ticket.py:62-74`) — note `db`, not `database`.
Titles follow the filed siblings: `[OP][<primary area>][privacy] …`. Labels mirror OP-2745/2746/2749
exactly: `agent:auto`, `area:*`, `capability:enable=gerrit_push`, `class:subscription-claude`,
`priority:meta`, `scope:failed-units-2026-07-25`, `tier:X`, `type:*` — `tier:X` is what makes them
un-pickable, so `agent:auto` is safe. File order matters and `--class` is required
(`file_jira_ticket.py:361`). The manifest mutex is expressed as **`mutex_with`** in the description
body, which the OP-1124 auto-injector also maintains (`file_jira_ticket.py:276-287`) — not as a
`mutex:` label.

**Everything in this chain ships default-OFF**, including E's worker and G2's processor. ACT is the
single enable step. A ticket that changes behaviour the moment it merges is a defect in this chain,
not a feature.

---

## The gate that every ticket below shares

**`dsar_required_pg`** — a new fixture. It **raises `pytest.UsageError`**, never `pytest.skip`, when
`OMNI_TEST_PG_URL` is absent, unreachable, points at production (`conftest.py` already refuses
production DSNs), or cannot reach Alembic head. Every decisive test in C…G uses it.

Verification names **exact test files, never a broad `-k`** — `-k` silently deselects, and a run of
unrelated green tests looks identical to a run that proved something. Any skipped or deselected
decisive test fails the gate.

**It must not be built on the existing skipping fixtures.** `pg_test_dsn` and
`pg_test_alembic_upgraded` skip (`conftest.py:715-722, 770-778`), and a fixture that depends on them
never runs its own raise — the skip happens first. So `dsar_required_pg` resolves the DSN and the
Alembic head itself, and a **negative test asserts that it raises `pytest.UsageError` rather than
skipping** when the DSN is absent. Without that test the gate is a comment.

This fixture is C's deliverable because C ships first; D…G consume it.

---

## Sequencing

A and B are **filed, not shipped** — `privacy.py` in this checkout still reads and deletes meetings
tenant-wide and still uses `current_user`. So C, which refactors exactly that code, must follow A;
and E, which widens erasure, must follow B's human gate.

```
OP-2745 (A) ──> C ──> D-a ──> D-b ──> D-c ──> G1 ──> E ──> G2 ──> ACT
OP-2746 (B) ──> E                              │
OP-2746 (B) ──┬─────────────────────────────> F
              C ──────────────────────────────┘
```

**G1 before E** is the ordering bug v1 missed: erasure already runs
`DELETE FROM oauth_tokens WHERE user_id = $1` (`privacy.py:297`), so if E widens erasure before the
outbox exists, the encrypted token material needed to revoke at the IdP is destroyed and **the retry
is impossible in principle**, not merely unimplemented.

**D and F both edit the manifest file.** They declare `mutex_with` each other in the description,
**not** a Blocks edge —
a false dependency would serialise unrelated work, and parallel same-file siblings produce a
retroactive conflict the merger bot cannot fix.

---

## Ticket C — `[OP][backend][privacy] Code-as-manifest: classify every physical table, with CI comparing it to the schema`

**Story** · `tier:X` · `type:feature` · areas **backend, tests, docs, security** · priority High
**blocked by** OP-2745 · **Spec-ref** design §4 story C, §4c, §7b decision ③

### Goal
DSAR behaviour lives in three hand-maintained tuples that nothing checks against the schema:
`_ERASURE_DELETE_STATEMENTS` (`privacy.py:275-298`), `_ERASURE_TENANT_DELETE_STATEMENTS` (`:316-321`)
and the fetch list in `_fetch_all_user_data` (`:102-223`). A table added anywhere else is silently
outside all three — which is how the memory tiers came to survive a `completed` erasure.

Replace them with one manifest carrying, per **reference** (not per table): subject selector, tenant
scope, access projection, portability projection, erasure action, legal basis, **expiry rule**,
verification query, barrier applicability, and external-processor action.

**Table-level classification is separate from reference-level rules.** `non_subject` is a table-level
verdict, mutually exclusive with having any subject rule, and requires a written rationale. A table
is not classified merely because one harmless column was given a rule.

### Files / Paths
- new `backend/privacy_manifest.py`
- new `backend/tests/conftest_dsar.py` (or a fixture in `backend/tests/conftest.py`) —
  `dsar_required_pg`
- `backend/routers/privacy.py` — the three tuples become manifest consumers
- new `backend/tests/test_privacy_manifest_schema_parity.py`
- new `scripts/gen_privacy_manifest_doc.py` + generated `docs/operations/privacy-data-manifest.md`

### Out of scope / MUST NOT
**MUST NOT** change what any endpoint returns or deletes — this is a refactor plus a classification;
behaviour belongs to D/E/F/G. **MUST NOT** edit migrations. **MUST NOT** add a wildcard or default
classification: an unclassified table must fail, not fall through. **MUST NOT** classify
`challenges` or `episodic_memory` as `non_subject` — they reference subjects by e-mail and inside
JSON (`action_challenges.py:156,200` write; PG storage `0264_challenges_action_grants.py:56`;
`intent_memory.py:108-113`)
and must be pinned as **noncanonical locators, outside the trigger's guarantee**, each with **one**
declared disposition (normalise / generated column / pseudonymise) — not a list of options.
**MUST NOT** hand-write the generated doc.

### Acceptance
1. **Code** — the manifest compares against application-owned `public` **base and partitioned**
   tables at Alembic head, with exclusions enumerated explicitly by name. Every schema-detectable
   subject locator, plus the two pinned noncanonical locators, appears **exactly once**. Every
   retained rule has an `expiry`.
2. **Deploy** — no migration, no runtime behaviour change; the manifest is import-time data.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_privacy_manifest_schema_parity.py
   backend/tests/test_dsar_access.py backend/tests/test_op2239_bi0b_retention_dsar_audit.py -q`
   passes with **zero deselected and zero skipped**, enforced by `dsar_required_pg` raising rather
   than skipping.
4. **Exercised** — two **negative controls**, each proving the gate fails: (a) create an
   unclassified table in `public` **inside a rolled-back transaction** and assert parity fails;
   (b) a retained rule with no `expiry` fails. `pg_temp` is not acceptable for (a) — it is outside
   the inspected schema and would prove nothing.
5. **Go-Live** — the generated doc is committed and a test asserts it matches a fresh generation, so
   the documentation cannot drift from behaviour.

---

## Ticket D-a — `[OP][db][privacy] Subject registry and tenant-lifecycle schema (additive, inert)`

**Story** · `tier:X` · `type:feature` · areas **backend, db, tests** · priority High
**blocked by** C · `mutex:privacy-manifest`
**Spec-ref** design §4c(i), §4b constraint 1

### Goal
Create the durable state that everything downstream consults — **additively**, changing no
behaviour. Erasure state and the pseudonymous tombstone live in a **non-cascading subject registry**
that outlives both `users` and `tenants`, because `users.erasure_state` would be deleted by tenant
deletion (`admin_tenants.py:1091-1097`), leaving nothing to consult and nothing proving the erasure
happened — the receipt's defect one level up.

Also: tenant lifecycle state and the indexes the later tickets need, including
**`episodic_memory (tenant_id, owner_user_id)`**, which F and E both require and which does not
exist today.

**Empty tables only — D-a writes no rows.** Backfilling here would create non-cascading registry rows
that survive tenant deletion *before* any retention enforcement exists, so if D-b never landed, D-a
alone would have manufactured orphaned personal data. Backfill belongs to D-b, where the cutover and
its rollback window are defined together.

### Files / Paths
- new alembic migration — subject registry, tenant lifecycle state, indexes
- `backend/privacy_manifest.py` — register the new tables
- new `backend/tests/test_subject_registry_schema.py`

### Shared-cluster safety (mandatory, `postgres-ha` is shared with co-tenant projects)
Additive DDL only: no table rewrite, no volatile default on an existing table, `CREATE INDEX
CONCURRENTLY`, a short `lock_timeout` on every DDL statement, and **batched** backfill. Any FK added
here is `NOT VALID`, validated separately.

### Out of scope / MUST NOT
**MUST NOT** drop the `dsar_requests` FKs (that is D-b). **MUST NOT** add triggers of any kind.
**MUST NOT** write, backfill or dual-write any registry or lifecycle row — the tables ship empty.
**MUST NOT** change any application behaviour. **MUST NOT** run an index build that holds a lock on
a shared table.

### Acceptance
1. **Code** — registry and lifecycle tables exist with no FK that cascades from `users`/`tenants`.
2. **Deploy** — migration is additive; `alembic upgrade head` then `downgrade -1` round-trips on a
   production-shape clone. **No test or benchmark runs against the production database.**
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_subject_registry_schema.py -q`,
   `dsar_required_pg`, zero skipped.
4. **Exercised** — a test asserts the new indexes exist and that the tables are **empty after
   migration**; the durability property itself is D-b's to prove, because only D-b puts rows there.
5. **Go-Live** — DDL statement timings recorded from the clone; no statement holds a lock on a
   shared table beyond the stated `lock_timeout`.

---

## Ticket D-b — `[OP][db][privacy] Make DSAR records durable: FK cutover and the tenant-delete interlock`

**Story** · `tier:X` · `type:bug` · areas **backend, db, tests, security** · priority High
**blocked by** D-a · **Spec-ref** design §4c(i), §9 finding 1

### Goal
`dsar_requests` cascades on **both** `tenants(id)` and `users(id)` (`0064_dsar_requests.py:95-106`),
so a completed erasure receipt is destroyed by a later tenant deletion — the record that proves the
erasure cannot survive the disappearance of the subject it is about.

**Cutover, not a flag-day.** D-b owns the backfill into D-a's empty tables, via **dual-write with
the old FK-backed representation retained through a stated rollback window**. Rollback disables the
new consumers and preserves the regulatory rows; it **never attempts to restore cascading FKs**,
which is impossible once a surviving receipt references a deleted parent. The window length is
stated in the ticket before merge.

Drop both cascading FKs; keep the ids as historical text; move creation-time integrity into a
`BEFORE INSERT` check that verifies the referenced rows exist taking `FOR KEY SHARE`; make the
historical ids and snapshot fields immutable; add `retention_until` to terminal receipts and frozen
scopes, with a keyed pseudonym wherever raw identity is not required (decision ③ — a retained
category with no expiry rule is a defect, and v1 of this design retained raw ids indefinitely).

**The tenant-delete interlock goes at initiation, not at the users phase.** The delete phases run
under `acquire()` with **no enclosing `conn.transaction()`** (`admin_tenants.py:1222-1229`), so each
phase autocommits and users are deleted near the end. A guard firing at `DELETE FROM users` would
abort *after* earlier phases had already committed — worse than either outcome. Tenant-delete
initiation and DSAR phase 1 take the **same transaction-scoped advisory lock in a fixed order
(tenant lifecycle → user)** and atomically claim either `tenant = deleting` or `subject = erasing`,
committing the claim **before** any child DML. The loser refuses or queues; it never waits holding
locks. The guard covers **every nonterminal DSAR state**, not only `erasing`. Every nonterminal
request is finished or cancelled with a recorded reason so no orphan poison job is left behind.

### Files / Paths
- new alembic migration — FK drop, insert-time check, immutability, `retention_until`
- `backend/routers/admin_tenants.py` — the initiation interlock
- `backend/routers/privacy.py` — phase 1 claim
- `backend/tests/test_alembic_0064_dsar_requests.py` — **reverse** the cascade/FK assertions at
  `:247-254, 310-318` and the migration-ordering assumption at `:370-374`
- `backend/tests/test_admin_tenants_delete.py` — the drift guard at `:298-323` needs a third
  classification, **retained regulatory record**, beside "explicitly deleted" and "cascading child"
- new `backend/tests/test_dsar_durability.py`

### Out of scope / MUST NOT
**MUST NOT** add erasure-barrier triggers — those are D-c's. D-b **MAY** add only the named
`dsar_requests` creation-integrity and immutability constraint triggers this ticket requires;
PostgreSQL has no other way to express them, and v2's blanket "MUST NOT add triggers" contradicted
D-b's own goal. **MUST NOT** rewrite tenant deletion into one giant transaction —
that changes the failure mode of an operation co-tenant projects depend on, and is a separate
decision. **MUST NOT** leave a partially deleted tenant behind on interlock loss. **MUST NOT** let
retention purges be unbounded deletes.

### Acceptance
1. **Code** — no FK from `dsar_requests` cascades; insert-time existence check present; historical
   ids immutable.
2. **Deploy** — FK drop takes a brief lock only; migration run and timed on a production-shape
   clone.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_dsar_durability.py
   backend/tests/test_alembic_0064_dsar_requests.py backend/tests/test_admin_tenants_delete.py -q`,
   `dsar_required_pg`, zero skipped.
4. **Exercised** — the decisive test: **concurrent** tenant-delete and DSAR phase 1 — exactly one
   wins, the loser refuses or queues, **no partially deleted tenant remains**, and the receipt
   survives tenant deletion. Also: the three reversed pre-existing assertions land in this same
   change, not left failing.
5. **Go-Live** — the application changes ship **default-OFF**; retention purge is batched with a
   bounded per-run ceiling, and the ceiling is stated. The rollback procedure is written down and
   exercised on the clone **before** merge, including the explicit statement that FK restoration is
   not part of it.

---

## Ticket D-c — `[OP][db][privacy] The write barrier: one conjunctive rule, installed disabled`

**Story** · `tier:X` · `type:feature` · areas **backend, db, tests, security** · priority High
**blocked by** D-b · `mutex:privacy-manifest` · **Spec-ref** design §4c(ii), §9 findings 2-3

### Goal
Stop concurrent writers re-creating what erasure just deleted.

**Semantic, not privileged.** The application role is verified `rolsuper = t, rolbypassrls = t`, so a
caller-settable GUC is forgeable and `SET ROLE` is freely available — neither a GUC nor a privileged
erasure role is worth anything. (§4b's earlier "narrowly privileged procedure or DB role" is
superseded by §4c.)

**One conjunctive predicate over `OLD` and `NEW`** — not three allowances OR-ed together, which
would let a single statement perform a permitted transition *while* re-enabling the account. Extract
every subject reference from `OLD` and `NEW`; require every non-`active` subject in `OLD` to be
absent from `NEW`; require every newly introduced subject to be `active`. The barrier consults the
**subject-registry PK** (state lives there since D-a, not on `users`).

The sole `erasing → erased` transition locks the registry row and the user row and succeeds **only**
when the user row has the complete canonical redacted shape (`privacy.py:345-349` — disabled,
credential-free) and every non-allowlisted column is unchanged.

### The guarantee, stated honestly
It covers **ordinary `INSERT`/`UPDATE` on canonical structured references while triggers are
enabled.** It does **not** resist deliberate superuser trigger suppression: `SET LOCAL
session_replication_role = 'replica'` is already used in this repo's own tests
(`test_execution_service.py:225`, `test_resume_worker.py:125`), and a superuser can `ALTER TABLE …
DISABLE TRIGGER`. Nor does it cover noncanonical locators — `challenges.confirmer_actor` stores an
e-mail and `episodic_memory.solution` embeds one in JSON, and once redaction rewrites the e-mail
nothing can join it back to a subject. Those are C's declared dispositions, owned here for any
generated/normalised column the barrier needs.

**Do not write "nothing to forge" or equivalent into the ticket, the code, or the docs.** De-
superusering the app role is a separate prerequisite for a stronger guarantee.

### Files / Paths
- new alembic migration — triggers, installed with **execution disabled**
- `backend/privacy_manifest.py` — barrier applicability per reference
- `backend/routers/privacy.py` — bounded retry with backoff for phase 1
- new `backend/tests/test_erasure_barrier.py`

### Out of scope / MUST NOT
**MUST NOT** use a GUC, `SET ROLE`, or a privileged DB role as the bypass. **MUST NOT** replace DB
enforcement with call-site checks in writers — the point is that it holds for writers nobody
remembered. **MUST NOT** add `DELETE` barriers; erasure is deletion. **MUST NOT** hold the user-row
lock across phase-2 child DML (child→user vs user→child inversion). **MUST NOT** enable enforcement
in the deploy that installs it. **MUST NOT** change application DB roles here.

### Acceptance
1. **Code** — triggers installed **disabled**; enabling is a separate operator action (ACT).
2. **Deploy** — trigger DDL uses a short `lock_timeout`; timings from a production-shape clone;
   coverage verified against the manifest before any enable.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_erasure_barrier.py -q`,
   `dsar_required_pg`, zero skipped, triggers force-enabled within the test transaction.
4. **Exercised** — the decisive tests: a transition attempt with a **noncanonical** user row
   (`enabled = 1`, or credentials still present) is **rejected and leaves state `erasing`**; the
   canonical shape succeeds; a later re-enable or re-credential attempt fails; `detach` passes;
   re-attaching a reference to a non-`active` subject fails; and `INSERT … ON CONFLICT DO UPDATE` is
   covered because **both** trigger paths apply — pinned as a regression test against the existing
   upsert at `db.py:5398-5405`.
5. **Go-Live** — phase 1 uses bounded retry with backoff rather than relying on the pool's 10s
   `lock_timeout` (`db_pool.py:33-39`); and the added registry-PK lookup is benchmarked on the chat
   and bulk-scheduler write paths **on an isolated production-shape clone**, against a
   threshold fixed in this ticket: **capture the pre-change baseline on the same clone, then require
   p95 write latency ≤ baseline + 10% and throughput ≥ baseline − 5%** on both paths. A relative
   bound is used deliberately — an absolute number invented before measuring is not a gate, and the
   baseline is not known until this ticket runs it. Failing the bound blocks ACT, not D-c.

---

## Ticket G1 — `[OP][db][privacy] Durable processor-purge outbox schema, shipped inert`

**Story** · `tier:X` · `type:feature` · areas **backend, db, tests, security** · priority High
**blocked by** D-c · **Spec-ref** design §4 story G, §9

### Goal
Ship the outbox **before** E widens erasure. Erasure already deletes `oauth_tokens`
(`privacy.py:297`), so once E runs without an outbox the encrypted material needed to revoke at the
IdP is gone and retry becomes impossible in principle. The schema must exist, inert, first.

`security/oauth_revoke.py` provides `revoke_record`, whose own docstring names its callers as the
OAuth router "**or the DSAR runbook script for the regulatory path**" — **no such caller exists.**

### Files / Paths
- new alembic migration — outbox table with dispositions
- `backend/privacy_manifest.py` — external-processor action per reference
- new `backend/tests/test_processor_outbox_schema.py`

### Out of scope / MUST NOT
**MUST NOT** process the outbox (G2). **MUST NOT** store plaintext tokens or key material in it —
the retry snapshot holds what `revoke_record` needs, via the existing token vault, and nothing more.
**MUST NOT** use an audit row as the retry queue: audit is a record of what happened, not a work
queue, and conflating them means a purge is retried by rewriting history.

### Acceptance
1. **Code** — outbox table with an explicit disposition set including `manual_purge_required`.
2. **Deploy** — additive migration, inert; no code path writes to it yet.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_processor_outbox_schema.py -q`,
   `dsar_required_pg`, zero skipped.
4. **Exercised** — a column-set assertion proves nothing, because any `TEXT`/`BYTEA`/JSON column
   mechanically accepts plaintext. Instead: the schema exposes **no plaintext-token column** and
   requires a **versioned encrypted-envelope shape**; a **negative** test submits known raw access
   and refresh tokens through the outbox repository API and asserts **rejection**; a **positive**
   test stores an encrypted snapshot and then **scans every persisted value** to prove neither raw
   fixture token appears anywhere in the row.
5. **Go-Live** — documented as inert, with G2 named as the consumer.

---

## Ticket E — `[OP][backend][privacy] Erasure over the manifest, including every memory tier`

**Story** · `tier:X` · `type:bug` · areas **backend, tests, security** · priority High
**blocked by** G1 **and** OP-2746 · areas add **db** (owns a constraint migration) ·
**Spec-ref** design §4 story E, §4b constraint 4

### Goal
Drive erasure from C's manifest under D-c's barrier, covering **five** tables that a `completed`
erasure survives today: `episodic_memory`, `chat_session_summaries` (which has **no delete path
anywhere in `backend/` outside tests**), `l3_facts`, `l3_eval_runs`, `l3_approvals`.

**Memory tiers are erased via `u6_l3_store.erase_user` / `erase_user_evals`, not a plain `DELETE`** —
for the **scoped-delete predicate** and the **pseudonymous tombstone** (`u6_l3_store.py:196-219`,
tombstone INSERT at `:216-219`).

On the `dek_ref` overwrite: keep it, and describe it accurately. The code already does
(`u6_l3_store.py:198-203`) — *"the shred COMPLETES only once the dek_ref is gone from every copy
(backups/WAL/PITR/replicas) within the retention RPO — a purge SLA the ops layer owns; txn commit
alone does not scrub a WAL/dead-tuple pre-image."* So it is the **first step of a shred, not a
completed one**: real, but not self-sufficient. **The receipt must not claim unrecoverability at
commit time** — which is precisely decision ②, and it makes the retention RPO a required field of
G2's backup disposition rather than an ops detail nobody wrote down.

`hard_erase_user` is **not** reused: it asserts `assert_human_owner(ctx, scope)` and the router has
no `ExecutionContext`; its human gate is OP-2746's.

**Completion semantics — decision ② (OP-2747): comply with regulation, do not overclaim.** E may
only reach the nonterminal state **`processor_purge_pending`** — exactly one new value, not a
choice of two. `status` is **`TEXT` guarded by a CHECK**, not a PostgreSQL enum
(`0064_dsar_requests.py:89-106`), so this is a **constraint migration on a shared table**, not an
"additive enum value": short `lock_timeout`, timed on a production-shape clone, with an explicit
validation and rollback plan. **E must not write `completed`.**

**E owns the durable DSAR worker.** This is the gap v2 left open. Erasure today rolls back
atomically on failure (`privacy.py:411-428`), so a crash is harmless; once D-b commits
`subject = erasing` before child DML, a crash after the claim would strand the subject in `erasing`
forever behind the barrier. The worker row-locks and checkpoints `dsar_requests`, **resumes every
nonterminal request after a restart**, and runs phase 1 plus manifest-driven phase 2.

**OAuth ordering — E deletes, G2 revokes.** E writes the encrypted retry snapshot to G1's outbox
**atomically with** deleting the local `oauth_tokens` row; G2's worker then reconstructs the
token-vault record from that snapshot and calls the IdP. v2 stated the opposite in G2 (revoke first,
then delete) and the two could not both hold. Deleting first is the correct half: the local row must
go for the erasure to be true, and the durable snapshot is what makes the revocation retryable.

### Files / Paths
- `backend/routers/privacy.py` — `_erase_user_data` becomes manifest-driven
- new durable DSAR worker module + its registration
- `backend/agents/u6_l3_store.py` — expose scoped erase for the router path
- new alembic migration — the `processor_purge_pending` CHECK expansion
- new `backend/tests/test_erasure_manifest_coverage.py`,
  `backend/tests/test_dsar_worker_resume.py`

### Out of scope / MUST NOT
**MUST NOT** write `completed`. **MUST NOT** replace the scoped erase + tombstone with a plain
DELETE for any envelope-encrypted table. **MUST NOT** widen erasure to `meetings` /
`transcript_segments` — OP-2745 removed them deliberately and OP-2749 is the way back. **MUST NOT**
rely on RLS for scoping: the app role is `rolbypassrls = t`, so an RLS-scoped delete is not scoped.
**MUST NOT** delete another subject's rows.

### Acceptance
1. **Code** — a manifest rule with an erasure action and no implementation fails the suite;
   no code path writes `completed` (asserted on **behaviour** — run the erasure and check the
   resulting status — **and** by a source check that strips comments and strings first, because a
   naive grep matches the comment explaining the rule).
2. **Deploy** — additive enum value; no destructive migration.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_erasure_manifest_coverage.py
   backend/tests/test_dsar_access.py -q`, `dsar_required_pg`, zero skipped.
4. **Exercised** — seed one target row in **each of the five tables**, plus a **same-tenant control**
   and a **cross-tenant control**, **and** a row for each of the two noncanonical locators
   (`challenges.confirmer_actor`, `episodic_memory.solution.operator`) asserting the **exact
   post-erasure shape C declared** for them. After erasure: target residue is zero by the manifest's
   own verification queries, **both controls survive untouched**, the L3 tombstone exists, and the
   OAuth retry snapshot is in the outbox. Concurrency: an erasure racing a concurrent writer leaves
   no re-created row **for canonical structured references, with triggers enabled** — the bound D-c
   states; this AC must not restate it unqualified. Plus a **process-death-after-phase-1 restart
   test**: kill the worker after the claim commits and assert the request resumes to
   `processor_purge_pending` rather than stranding.
5. **Go-Live** — the worker ships **default-OFF**; the receipt reads nonterminal, and the operator
   doc says what that means.

---

## Ticket F — `[OP][backend][privacy] Access and portability with enumerated coverage`

**Story** · `tier:X` · `type:bug` · areas **backend, tests, security** · priority Medium
**blocked by** OP-2746 **and** C — **not** E · `mutex:privacy-manifest`
**Spec-ref** design §4 story F

### Goal
`_fetch_all_user_data` (`privacy.py:102-223`) has the blind spot erasure had. Enumerate:
`episodic_memory WHERE owner_user_id = subject`; **every** `chat_session_summaries` scope; **all** L3
states — `list_live_memories` returns promoted only (`memories.py:159-167`, state argument at
`:166`); all L3 evals and approvals; the decrypted `value` and `source_span`; clear semantic
metadata; and retained-data notices.

**Never** return ciphertext, `dek_ref` or key material. Classify the deterministic L3 tombstone
explicitly as **pseudonymous, not anonymous** (`u6_l3_store.py:213`).

Returning fact type and predicate while withholding `Fact.value` — which lives in the sealed payload
(`u6_l3_store.py:69-101`) — would repeat the false-completeness defect this epic exists to fix. The
earlier objection to decrypting was raised against a route with **no CSRF that accepted api-key
principals**; OP-2746 answers it, and the owner-facing `/memories` route already decrypts
(`memories.py:141-167`).

### Files / Paths
- `backend/routers/privacy.py` — `_fetch_all_user_data` becomes manifest-driven
- `backend/privacy_manifest.py` — access/portability projections
- new `backend/tests/test_dsar_access_coverage.py`

### Out of scope / MUST NOT
**MUST NOT** land before OP-2746 — decrypting on a route that accepts a bearer-minted admin is the
exact hazard the earlier objection named. **MUST NOT** return `dek_ref`, ciphertext or key material
under any flag. **MUST NOT** re-add meeting content (OP-2745 / OP-2749). **MUST NOT** rely on RLS or
use a tenant-wide memory query for a per-subject response.

### Acceptance
1. **Code** — a manifest rule with an access projection and no implementation fails the suite.
2. **Deploy** — no migration.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_dsar_access_coverage.py
   backend/tests/test_dsar_access.py -q`, `dsar_required_pg`, zero skipped.
4. **Exercised** — a fact seeded in **each** L3 state appears (not just promoted); a **cross-tenant
   control** does not appear; and a test asserts the serialised response contains **no value equal
   to the fixture's stored `dek_ref` or ciphertext** — an equality check against the known fixture
   values, not a grep for the word "ciphertext", which proves nothing.
5. **Go-Live** — the response's own metadata discloses the tombstone as pseudonymous and lists
   retained categories with their expiry.

---

## Ticket G2 — `[OP][backend][privacy] Processor and backup boundary: the receipt stops overclaiming`

**Story** · `tier:X` · `type:bug` · areas **backend, tests, docs, security** · priority High
**blocked by** E · **Spec-ref** design §4 story G, §7b decisions ② and ③

### Goal
Close the gap between "erased from the active system" and "erased everywhere". Process G1's outbox
with durable retries, wire `revoke_record`, and gate the terminal transition.

**Ordering: E has already deleted the local row.** G2's worker reconstructs the token-vault record
from G1's durable encrypted snapshot and calls the IdP from there. v2 required the IdP to be
contacted *before* the local delete, which contradicted E — and would have been the weaker design
anyway, since it makes revocation a precondition of erasure rather than a retryable obligation
after it.

Today erasure drops `oauth_tokens` locally (`privacy.py:297`), **never tells the IdP**, and never
writes the audit row that exists to "preserve the failure for follow-up retries". The processor keeps
a live token while the receipt says the data is gone.

### Files / Paths
- `backend/routers/privacy.py` + a durable worker — outbox processing
- `backend/security/oauth_revoke.py` — wire the existing hook (no rewrite)
- new alembic migration — DB-level terminal-transition guard
- `docs/operations/` — the receipt contract
- new `backend/tests/test_processor_purge_and_receipt.py`

### Out of scope / MUST NOT
**MUST NOT** claim backups are erased — decision ② is to disclose, not overclaim. **MUST NOT**
implement the outbox worker as an in-process background task: the tenant-delete path already does
that (`admin_tenants.py:1451-1465`) and a restart loses the work, which is exactly what a regulatory
retry must not do. **MUST NOT** let a failed purge mark a request terminal. **MUST NOT** make live
IdP calls in tests. **MUST NOT** run unbounded retention deletes.

### Acceptance
1. **Code** — the IdP is actually contacted, from the snapshot, and a disposition succeeds **only**
   when `revocation_attempted = True` **and** `revocation_outcome = 'success'`. A provider with **no
   revocation endpoint** produces an outstanding `manual_purge_required` disposition, **never
   success**. Verified: `revoke_record` returns `OUTCOME_SUCCESS` with `revocation_attempted=False`
   when `revoke_fn is None or not revocation_endpoint` (`oauth_revoke.py:390-399`), so a check on
   `outcome` alone passes while nothing was revoked.
2. **Deploy** — the terminal guard is enforced **in the database**: direct SQL attempting a terminal
   transition while any disposition is outstanding is rejected. An application-only check is
   bypassable by exactly the paths this epic exists to close.
3. **Integration** — `backend/.venv/bin/pytest backend/tests/test_processor_purge_and_receipt.py -q`,
   `dsar_required_pg`, zero skipped.
4. **Exercised** — the outbox survives a **process restart** (test restarts the worker); a
   revocation failure produces the audit row **and** a retryable entry rather than silence.
5. **Go-Live** — the processor ships **default-OFF**. The receipt asserts an **exact structured
   schema** — active-system, processor, backup (including the **retention RPO** by which the L3
   `dek_ref` shred actually completes, per `u6_l3_store.py:198-203`), retained-category, legal-basis
   and expiry dispositions — rather than a string check for
   "erased everywhere", which any synonym defeats. Restore-time re-erasure: **name and exercise the
   real restore entrypoint** against a throwaway restored database; **if no such entrypoint exists,
   file that as a prerequisite rather than testing an unused quarantine marker**, which would prove
   no restore protection at all.

---

## Ticket ACT — `[OP][devops][privacy] Enable erasure-barrier enforcement (operator gate)`

**Story** · `tier:X` · `type:feature` · areas **backend, devops, security** · priority High
**blocked by** D-c, E, G2 · **Spec-ref** design §4b constraint 3

### Goal
Everything above ships **default-OFF**. This ticket is the single, separately reviewable moment when
enforcement is turned on, after the whole chain exists. It runs the real end-to-end erasure on a
production-shape clone, audits runtime trigger state, confirms no trigger-suppression is in use on
the write paths, and only then enables.

### Acceptance
1. **Code** — no code change beyond the flag/enable step.
2. **Deploy** — enablement is reversible and the rollback is written down and tested first.
3. **Integration** — full DSAR suite green on the clone with `dsar_required_pg`, zero skipped.
4. **Exercised** — one real end-to-end erasure on the clone, verified by the manifest's own
   verification queries, with both controls surviving.
5. **Go-Live** — runtime trigger state audited on the target database; benchmark limits from D-c
   met; co-tenant write paths sampled for latency regression before and after.

---

## What is deliberately NOT in this chain

- **Participant attribution** (OP-2749 / story H) — dormant by decision ①; it is what later lifts
  OP-2745's containment.
- **Support-acting-for-a-subject** — needs its own `actor_id` / `subject_user_id` / reason / audit
  design; OP-2746 excludes it explicitly.
- **De-superusering the application role.** The barrier design exists *because* the role is
  `rolsuper = t, rolbypassrls = t`, and the guarantee is bounded by it. Fixing that is a platform
  change with blast radius across co-tenant projects on the shared cluster; folding it in here would
  couple this epic to it. It deserves its own ticket — and when it lands, §1a's RLS argument becomes
  load-bearing and the barrier's guarantee strengthens.
