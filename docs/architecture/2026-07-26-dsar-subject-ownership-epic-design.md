# EPIC design — DSAR subject-ownership, human gating, and the memory tiers

**Date:** 2026-07-26 · **Origin:** OP-2729 SP-1, re-scoped after a 3-way audit ·
**Scope label:** `scope:failed-units-2026-07-25` · **Revision:** v6 (rounds 1-3 folded; operator decisions recorded; the two remaining engineering defects designed, then reviewed → 4 HIGH + 2 MEDIUM folded) · **Status: A+B+H1+UI filed (OP-2745/2746/2748/2749); C→G designed below, unreviewed**

## 1. Problem

The finding that started this was narrow: **account erasure reports `completed` while every memory
tier survives.** `_ERASURE_DELETE_STATEMENTS` (`backend/routers/privacy.py:275-298`) lists 11
tables, none of them memory; `_erase_user_data` (`:324`) then writes
`dsar_requests status='completed'` (`:391`, `:434`). Surviving a "completed" erasure: `l3_facts`,
`l3_eval_runs`, `l3_approvals`, `chat_session_summaries` (no delete path anywhere in `backend/`
outside two test files) and — found in codex round 1 — `episodic_memory` rows keyed by
`owner_user_id`. `_fetch_all_user_data` (`:102-223`) has the identical blind spot, so the
access/portability receipt never discloses that any of it was held.

Two adversarial review rounds turned a one-file fix into an epic. What they established:

**1a. The original rationale was wrong; the decision survives.** Reusing `u6_l3_store.erase_user`
was justified on crypto-shred grounds. That does not hold — MVCC does not overwrite in place, so
`UPDATE dek_ref='{}'` before `DELETE` *adds* a dead tuple holding the real key and removes nothing
from WAL, PITR, dead tuples or the streaming standby; the source says so
(`u6_l3_store.py:199-203`, `u6_memory_crypto.py:25-29`).

The reasons that survive: the `(tenant_id, user_id)` predicate and the tombstone — **today**; and
**RLS tomorrow**. That distinction was gotten wrong twice and is now pinned: the RLS argument says a
raw `DELETE FROM l3_facts` in the router never sets `app.tenant_id`/`app.user_id` and so matches
zero rows while still succeeding. **That is not true of the current deployment.** Verified live: the
application role is `rolsuper = t, rolbypassrls = t`, so it bypasses RLS entirely and a raw DELETE
*would* match. The RLS argument is therefore **protective-in-future** — it becomes the load-bearing
reason the moment the app role is de-superusered, which is a stated direction — and must not be
cited as a present-day fact. **v2: the pre-delete overwrite is
dropped from the DSAR path entirely** — on the shared cluster it buys WAL volume and lock time and
no recoverability (codex #9).

**1b. These are SELF-SERVICE endpoints, and the first proposed gate would have broken them.** The
docstrings say so — "Return/Erase **the current user's** account-owned data" (`privacy.py:3-11`) —
and the integration fixture creates the subject as a **`viewer`** (`test_dsar_access.py:21,265`).
v1 proposed `require_operator` + `assert_human_principal`; `require_operator` imposes a role floor
that would deny a viewer their statutory self-service right, and `assert_human_principal` infers
humanity from names and emails, which can deny a real person. **Both rejected.**

What is real is the gap they were aimed at: all three routes are `Depends(auth.current_user)` and
nothing else (`:358, 402, 444`); `current_user` mints a synthetic **admin** for any API-key bearer
(`auth.py:1846-1849`) with `csrf_token=""`; and `csrf_check` is reachable only through
`require_role` (`auth.py:1940-1946`), which `privacy.py` does not use.

**1c. Tenant ownership is being treated as user ownership.** `_ERASURE_TENANT_DELETE_STATEMENTS`
(`:301-321`) deletes `transcript_segments` and `meetings` **tenant-wide** by `user.tenant_id`, and
access reads them the same way (`:185-203`). For an API-key principal `tenant_id` defaults to
`"t-default"` (`auth.py:178`). This is documented and deliberate — those tables have no `user_id`
column, so tenant scope is "the only correct scope on read", and the export marks them
tenant-scoped. The consequence stands regardless: **a per-user DSAR discloses and deletes another
user's content.** The existing regression test protects a user in a *different* tenant
(`test_op2239_bi0b_retention_dsar_audit.py:303-367`) and so cannot catch it.

*Verified read-only on prod: `t-default` holds 0 meetings and 0 transcript_segments, and
`episodic_memory` holds 0 rows with a non-null `owner_user_id`. Both mines are armed and both blast
areas are currently empty — which bounds urgency, not correctness.*

**1d. Erasure destroys the information needed to erase.** Memory scopes are `(tenant_id, user_id)`
pairs and a user may hold several memberships (`:113-118`), but
`DELETE FROM user_tenant_memberships` is the **first** erasure statement (`:277`).

**1e. Ordering alone cannot make erasure complete.** Codex round 1's decisive finding. L3 insertion
takes no subject lock and checks no erasure state (`u6_l3_store.py:104-121`); the L2 scheduler
selects chat rows and writes summaries without joining `users.enabled` or locking the subject
(`u6_memory_scheduler.py:161-179, 220-257`); invite acceptance can insert a new membership
concurrently (`tenant_invites.py:1236-1242`). An in-flight request, a scheduler tick or an invite
acceptance can commit **after** the DSAR's deletes, recreating memory or introducing a tenant that
was absent from the frozen scope list. **No statement order closes this.**

**1f. The inventory is incomplete beyond memory.** Also user-bearing and absent from both paths:
`session_revocations`, `session_fingerprints` (IP subnet data), the KS decryption audit,
`orchestrator_tasks`, `proposed_actions`, shareable objects keyed by `owner_user_id`
(`0197_shareable_objects.py:71-112`), and **`episodic_memory` rows keyed by `owner_user_id`**
(`0260_episodic_memory_source_aware.py:62-92`) — the private store whose intent path writes the
subject id and the operator identity into the payload (`intent_memory.py:105-139`). v2's fold table
claimed this was listed here and it was not; corrected in v3.

## 2. Reusable vs missing

**Reusable:** `erase_user` / `erase_user_evals` (RLS-correct, `(tenant_id, user_id)`-scoped,
tombstone, nests as a savepoint) — **minus** the cosmetic pre-delete overwrite; the `(category, sql)`
statement-tuple shape for plain per-user tables; `auth.csrf_check` as a primitive.

**Deliberately NOT reused:** `hard_erase_user` (`u6_l3_confirm.py:126`) — it asserts
`assert_human_owner(ctx, scope)`, which needs an `ExecutionContext` the router lacks, and building
one would be theatre because `for_human()` hardcodes `principal_type="human"`
(`execution_context.py:58-59`) with no validation. `require_operator` and `assert_human_principal`
— see §1b.

**Missing and to be built:** a **human-session gate with no role floor**; a durable **subject
erasure state enforced by database triggers** (the write barrier) plus a retryable worker; a
**frozen scope snapshot** (`dsar_request_scopes`) — *not* a separate registry, since membership rows
are already authoritative (`db.py:1485`); a **code-as-manifest** of per-table dispositions rich
enough to express what the code already does; memory coverage in erasure *and* access; and a
same-tenant test.

## 3. Target architecture

```
POST /privacy/{access,portability,erasure}
  └─ require_human_session      cookie session; NO Authorization header;
                                session.user_id == user.id; CSRF; NO role floor
                                (erasure additionally: recent-auth / MFA)
  └─ two-phase erasure, not one transaction:
       phase 1  mark subject `erasing`, revoke sessions, drain writers
       phase 2  under the subject lock:
                  FREEZE   scopes from the authoritative registry
                  ERASE    per-table, by manifest disposition
                            (delete | redact | detach | retain | external-purge)
                  VERIFY   re-scan for residue
                  RECEIPT  dsar_requests -> completed, with retained-data notices
```

**The barrier is enforced by PostgreSQL, not by convention.** v2 proposed a subject-wide advisory
lock that every writer opts into. Round 2 showed why that fails: the writer set is far larger than
v2 named — ordinary chat (`chat.py:243`, `db.py:5285`), sessions and fingerprints (`auth.py:900`),
preferences (`preferences.py:201`), drafts (`db.py:5890`), shares (`shareable_objects.py:54`),
orchestrator tasks (`db.py:5391`), proposed actions (`db.py:5482`) — and **one missed call site
reopens the race**. A busy writer would also turn erasure into a 10-second failure
(`db_pool.py:33`).

Instead: `erasure_state` lives on the retained `users` row, and each subject-bearing table gets a
shared `BEFORE INSERT OR UPDATE` trigger that takes `SELECT … FOR SHARE` on that row and rejects a
non-`active` subject. Phase 1's `UPDATE users SET erasure_state='erasing'` then *waits* for
pre-existing writers; later writers wait, observe `erasing`, and fail. **Application call sites need
no edits at all** — the invariant cannot be forgotten because no one has to remember it.

Phase 2 runs in a **retryable durable worker** owning its job through `dsar_requests` row locking —
that table already carries `pending`/`processing` states (`0064_dsar_requests.py:89`). This closes
the crash-after-phase-1 hole, which would otherwise strand a subject in `erasing` indefinitely with
no way back in.

The separate "authoritative scope registry" is dropped (v3; any remaining mention elsewhere in this
document is stale): membership rows are already declared
authoritative (`db.py:1485`) and suspension preserves them (`tenant_members.py:681`). Freeze
`users.tenant_id ∪ all membership tenant_ids` into `dsar_request_scopes` before deleting
memberships. Add a historical ledger only if evidence shows subject rows can outlive their tenant.

Lock ordering still needs one order across writers and erasers: `confirm` locks approval/eval→fact
(`u6_l3_confirm.py:77-82`) while `hard_erase` locks fact→approval/eval (`:126-136`) — opposite
orders under a 10-second timeout.

`_enter_scope`'s transaction-local GUCs get a **scope context manager** that snapshots and restores
them, replacing v1's "just order memory last" workaround.

## 4. Child stories (revised decomposition)

v1 claimed D1–D4 were independently shippable. They are not: the gate denies the existing viewer
contract, erasure without a write barrier expands destruction, access needs a settled policy, and
the same-tenant test *necessarily exposes* the tenant-wide question. Revised:

| # | story | risk | gate |
|---|---|---|---|
| **A** | **Containment**: exclude `meetings`/`transcript_segments` from the self-service per-user endpoints, **and land the same-tenant test in the same change** | med | none — ships first |
| **B** | `require_human_session` for all three routes, all roles; separate support-acting-for-subject workflow deferred | med | after A |
| **C** | **Code-as-manifest**: multiple rules per table/reference, each carrying subject selector, tenant scope, access projection, portability projection, erasure action, legal basis + expiry, verification query, barrier applicability and external-processor action. Every physical table must be classified, including an explicit `non_subject`. CI compares schema shape and **generates** the human-readable doc | med | policy input |
| **D** | Subject erasure state (`active→erasing→erased`), subject-wide lock, authoritative scope registry, single lock order | **high** | after C |
| **E** | Erasure implementation over the manifest incl. every memory tier + concurrency tests | high | after C, D |
| **G** | **Processor / backup boundary**: distinguish `active_system_erased` from final disposition; durable purge outbox; wire the existing OAuth external-revocation hook (`security/oauth_revoke.py:40`), which today's erasure ignores; record backup expiry, restore-time re-erasure quarantine, retained categories and basis in the receipt | high | after E |
| **H** | **Participant attribution** for meetings/transcripts, so a subject can obtain their own content without disclosing a co-tenant's — neither table carries a participant identifier today (`0249_bi0_transcripts.py:58`) | high | unblocks A's limitation |
| **F** | Access/portability, with **enumerated** coverage: `episodic_memory WHERE owner_user_id=subject`; every `chat_session_summaries` scope; **all** L3 states (not only promoted — `memories.py:159` lists one state at a time); all L3 evals and approvals; decrypted `value` and `source_span`; clear semantic metadata; retained-data notices; **never** ciphertext, `dek_ref` or key material; and an explicit classification of the deterministic L3 tombstone, which is pseudonymous rather than anonymous (`u6_l3_store.py:213`) | med | after B ∥ C — **not** after E |

**A is the change that must not wait.** Two existing tests currently *affirm* the behaviour A
reverses — tenant-wide erasure (`test_op2239_bi0b_retention_dsar_audit.py:293`) and portability
including every tenant meeting and transcript (`:374`) — so they must be reversed in the same
review. v2 said "one commit or neither"; that was wrong and its second half was dangerous. There is
no runtime atomicity between code and test commits, and *"or neither"* would prefer continued
destructive behaviour over untested containment. **One mergeable change, and containment still ships
if test mechanics lag.**

**F reverses v1's "clear columns only".** Returning fact type and predicate while withholding
`Fact.value` — which lives in the sealed payload (`u6_l3_store.py:69-101`) — repeats the very
false-completeness defect this epic exists to fix. The earlier objection to decrypting was made
against a route that had **no CSRF and accepted api-key principals**; once B lands, that objection
is answered, and the owner-facing `/memories` route already decrypts (`memories.py:141-167`).

## 4b. Round 3 — three constraints that decide the implementation

**The subject state is not durable against the tenant cascade (CRITICAL).** Tenant deletion hard-
deletes `users` then `tenants` in separately committed phases (`admin_tenants.py:1091-1097,
1222-1229`), and `dsar_requests` CASCADEs on either (`0064_dsar_requests.py:99-102`). So between
phase 1 and phase 2 a tenant deletion can remove `erasure_state`, the memberships, the frozen scopes
**and the queued job**, leaving no retry. ⟹ freeze scopes **atomically in phase 1**; protect an
`erasing` subject from tenant/user deletion; make DSAR jobs and scopes **non-cascading regulatory
records**; coordinate tenant deletion through the same state machine.

**The trigger barrier can block the erasure it exists to protect (HIGH).** Phase 2 itself performs
`redact` and `detach` (`privacy.py:340-349`) — writes against a subject that is by then non-`active`.
Worse, a row-level `BEFORE` trigger sees the *pre-update* row, so the `erasing→erased` transition on
`users` reads `erasing` and rejects itself. Checking only `NEW` lets an ordinary writer escape by
detaching or rebinding an erased subject; checking `OLD`+`NEW` blocks legitimate phase-2 work. ⟹
per-reference `OLD`+`NEW` rules, an explicit allowed-transition set, and a **narrowly privileged
erasure procedure or DB role**. Explicitly **not** a caller-settable GUC as the bypass: the
application role is superuser, so a GUC bypass is forgeable by anything already inside the process.

**Phase 1 will time out, not drain (MEDIUM).** `FOR SHARE` blocks the users-row `UPDATE`, so a long
writer makes phase 1 hit the pool's 10-second `lock_timeout` (`db_pool.py:33-39`) rather than
waiting it out. ⟹ bounded retry with backoff from the durable worker; install triggers DB-first with
execution still disabled and verify coverage before enabling; do not hold the user-row lock across
phase-2 child DML (child→user vs user→child inversion). Benchmark the added users-PK lookup on the
chat and bulk-scheduler paths.

**Completion semantics (HIGH).** E marks `completed` while processor/backup disposition is deferred
to G — which recreates the original false-completion defect for OAuth processors, purge retries and
backups. ⟹ **E may only reach a nonterminal `active_system_erased` / `processor_purge_pending`**;
G co-ships or gates the terminal transition. The current enum has no such state
(`0064_dsar_requests.py:103-106`).

## 4c. The two remaining engineering defects — designed

### (i) DSAR records are not durable at all (was: "between phases")

Verified: `dsar_requests.tenant_id REFERENCES tenants(id) ON DELETE CASCADE` **and**
`user_id REFERENCES users(id) ON DELETE CASCADE` (`0064_dsar_requests.py:95-106`), while tenant
deletion hard-deletes `users WHERE tenant_id = $1` then `tenants WHERE id = $1`
(`admin_tenants.py:1088-1097`). So this is worse than a phase-1/phase-2 window: **a completed
erasure receipt is destroyed by a later tenant deletion.** The record that proves the erasure
happened cannot survive the disappearance of the subject it is about — which is precisely the case
it exists for.

**Design (v6, after targeted review).** A DSAR receipt is a *regulatory record about a subject who
may no longer exist*, so it must not be a child of that subject — and neither may the **barrier
state**, which has the same defect one level up: `users.erasure_state` is deleted by tenant deletion,
so after a tenant is removed nothing can be consulted and nothing proves the erasure happened.

- **A non-cascading subject registry** holds the erasure state and the pseudonymous tombstone, and
  outlives both parents. The receipt and the frozen scopes live with it. Dropping the two
  `ON DELETE CASCADE` FKs is part of this, but is not sufficient on its own.
  (`SET NULL` is unavailable — both columns are `NOT NULL` — and `RESTRICT` would let an open DSAR
  block tenant deletion forever.)
- **Creation-time integrity moves into the code path**: a `BEFORE INSERT` check that the referenced
  tenant/user exists, taking `FOR KEY SHARE`, replacing what the FK used to guarantee; the historical
  ids and the snapshot fields are then immutable.
- **Tenant deletion must interlock at initiation, not at the users phase.** Verified: the phase loop
  runs `acquire()` then `conn.execute()` per phase with **no enclosing `conn.transaction()`**
  (`admin_tenants.py:1222-1229`), so every phase autocommits and users are deleted near the end
  (`:1091-1097`). A guard that fires at `DELETE FROM users` would therefore abort *after* the earlier
  phases had already committed — a worse state than either outcome. Instead: durable tenant lifecycle
  state, and both tenant-delete initiation and DSAR phase 1 take the **same transaction-scoped
  advisory lock in a fixed order (tenant lifecycle → user)** and atomically claim either
  `tenant = deleting` or `subject = erasing`; the claim commits *before* any child DML. A loser
  refuses or queues — it never waits while holding locks. The guard covers **every nonterminal DSAR
  state**, not just `erasing`.
- **Every nonterminal request is finished or cancelled with a recorded reason** when a tenant is
  deleted, so no orphan is left as a poison job for a worker whose state is gone.
- **Retention applies to this record too.** Decision ③ says a retained category with no expiry rule
  is a defect — and the first draft of this very section retained raw tenant/user ids indefinitely.
  So: terminal receipts and frozen scopes carry an explicit `retention_until` and are purged at it,
  and identity is stored as a keyed pseudonym wherever the raw value is not required.
- **Not currently reachable, recorded anyway:** the registry also rejects reuse of an erased id.
  Both insert paths mint `u-{uuid4().hex[:10]}` (`auth.py:495`, `tenant_invites.py:1583`), so an id
  cannot be chosen by a caller today; this is a cheap invariant to hold, not a live hole.

### (ii) The barrier must not be a privileged role or a GUC

The trigger paradox: phase 2 itself performs `redact` and `detach` against a subject that is by then
non-`active`, and a row-level `BEFORE` trigger sees the pre-update row, so `erasing→erased` on
`users` reads `erasing` and rejects itself.

The obvious escapes both fail here. A **caller-settable GUC** is forgeable by anything already
inside the process. A **privileged erasure role** is no better today: verified live, the application
role is `rolsuper = t, rolbypassrls = t`, so `SET ROLE` is available to any code path that wants it.

**Design — make the rule semantic rather than privileged.** The barrier is on `INSERT`/`UPDATE`
only, and erasure is overwhelmingly `DELETE`, which is unbarriered by construction. What remains is
a small exception surface with a property worth exploiting: every legitimate phase-2 write *reduces*
the subject's footprint.

**One conjunctive predicate over the whole row — not three rules OR-ed together.** This was the
review's sharpest correction and it is right: a natural `detach_ok OR redaction_ok OR transition_ok`
admits a single statement that performs the allowed `erasing→erased` transition *while also* setting
`enabled = 1`, keeping credentials, or adding a fresh reference. The rule is therefore stated over
`OLD` and `NEW` together:

- extract every subject reference from `OLD` and from `NEW`;
- **require** that any non-`active` subject present in `OLD` is absent from `NEW` everywhere, and
  that any newly introduced subject is `active`;
- permit `erasing→erased` **only** when the complete canonical redacted user shape holds
  (`privacy.py:345-349` — disabled, credential-free) **and** every non-allowlisted column is
  unchanged.

So `detach` passes because it removes the reference; a rebind fails because it introduces a
reference to a non-`active` subject; and a combined statement fails because the predicate is
conjunctive over the whole row rather than satisfied by its best-looking clause.

**The guarantee is narrower than "no reference survives", and saying so is part of the design.**
Verified: `challenges.confirmer_actor` stores `user.email`, not an id
(`action_challenges.py:156,200`), and `episodic_memory.solution` embeds `"operator": operator_email`
inside a JSON blob (`intent_memory.py:108-113`). A trigger reading canonical scalar columns cannot
see either — and once redaction rewrites the email, neither can anything else: the reference can no
longer be joined back to a subject at all. **The trigger guarantees canonical structured references
and nothing more.** Everything else must be handled by normalising the reference into a real column
or association row, by an explicit generated reference column derived from the JSON, or by
pseudonymising the retained value. Free prose cannot be guaranteed by any trigger and the design
will not pretend otherwise. Story C's per-table reference manifest is where this contract is
written down, per reference, including which ones are *out* of the guarantee.

**Still open, and honestly so:** phase 1 hits the pool's 10-second `lock_timeout` rather than
draining a long writer, so it needs bounded retry from the durable worker; the added `users`-PK
lookup on every barriered write needs benchmarking on the chat and bulk-scheduler paths before
rollout; and `INSERT … ON CONFLICT DO UPDATE` is not an intrinsic bypass **provided both the INSERT
and the UPDATE trigger paths apply** — the existing upsert at `db.py:5398-5405` becomes a
regression test rather than an assumption.

### Test and drift-guard fallout of dropping the FKs

Reads are unaffected — DSAR queries are direct `WHERE user_id` with no parent join
(`privacy.py:179-183`) — but three things assert the current shape and must change *with* the
migration, not after it: the cascade/FK assertions in
`test_alembic_0064_dsar_requests.py:247-254,310-318`, the migration-ordering assumption at `:370-374`,
and the tenant-delete drift guard at `test_admin_tenants_delete.py:298-323`, which today recognises
only "explicitly deleted" or "cascading child" and needs a third classification: **retained
regulatory record**.

## 5. Codex review round 1 — folded in

| finding | disposition |
|---|---|
| #1 gate rejects legitimate subjects | **accepted** — `require_operator`/`assert_human_principal` dropped for `require_human_session`, no role floor (§3, story B) |
| #2 no write barrier; ordering cannot close the race | **accepted** — new story D: erasure state machine + subject lock + two-phase flow (§3) |
| #3 `episodic_memory` missed | **accepted and verified** — added to §1, §1f; prod currently 0 owned rows |
| #4 metadata-only access repeats the defect | **accepted** — story F now exports decrypted values (§4) |
| #5 ordering coexists but is insufficient; opposite lock orders | **accepted** — GUC context manager + one lock order (§3) |
| #6 taxonomy cannot express `detach`/`redact` | **accepted** — manifest with five dispositions + CI drift check (story C) |
| #7 D5 cannot be deferred; D6 depends on it | **accepted** — became story A, first and unconditional |
| #8 dependency graph wrong | **accepted** — decomposition replaced wholesale (§4) |
| #9 pre-delete overwrite not worth preserving | **accepted** — dropped from the DSAR path (§1a) |

## 6. Open questions for codex round 2

1. Story A excludes tenant-scoped artefacts from self-service. Does that create a *new* compliance
   gap — the subject can no longer obtain meeting content in which they participated — and if so, is
   the answer participant attribution, a tenant-admin export, or an explicit documented limitation?
2. Is a PG advisory lock keyed on the subject the right barrier, given writers span request handlers
   **and** a background scheduler in a separate process, with a 10-second pool lock timeout?
3. Where should the erasure state live — a column on `users`, or its own table — such that every
   writer's re-check is cheap and cannot be forgotten?
4. `require_human_session` forbids the `Authorization` header. Does any legitimate first-party
   caller (the frontend, an installer, a health probe) reach these routes with a bearer today?
5. Does story C's manifest belong in code (consumed at runtime) or in docs (asserted by CI)? v2 says
   both; is that duplication a maintenance trap?
6. Is two-phase erasure with a visible `erasing` state acceptable to a regulator, or does it need to
   look atomic from the subject's side?

## 7. Effort

A small and urgent. B small. C is reading-heavy (every migration) and needs policy input. **D is the
real cost — a concurrency contract touching every subject-data writer.** E follows D. F small once B
and E land.

## 7b. Operator decisions — recorded 2026-07-26 (OP-2747)

| # | decision | consequence |
|---|---|---|
| ① meeting content | **build participant attribution (story H)** | H moves from documented limitation to in-scope; as a data-model change it takes the safe-floor position and is filed as OP-2749, dormant. It is what later lifts OP-2745's containment. |
| ② receipt vs backups | **comply with regulation** — the receipt may not overclaim | erasure ends nonterminal; G gates the terminal transition; backup expiry is disclosed. Policy and the round-3 engineering finding now agree. |
| ③ retention | **compliance-driven but MUST END, bounded by project lifecycle and maintenance cycle** | the manifest's expiry is a **rule** relative to lifecycle events, not a date; and it implies enforcement — something must delete at that boundary. **A retained category with no expiry rule is now a defect, not a default.** |
| ④ account UI | **repoint the UI at `/privacy/*`** (my recommendation, accepted) | filed as OP-2748, blocked by OP-2746. No backend aliases. |

## 8. Verdict after three rounds, and what that means

Three adversarial rounds; the design converged in shape but is **still NOT-SOUND as a whole**. The
reviewer's own go/no-go, which I adopt:

> ship **A and B now**; make erasure **enqueue `pending` rather than claim completion**; finish
> **C→D→E+G before any terminal receipt**; document **H** as a known limitation and require
> attribution before participant exports or meaningful transcript ingestion.

On whether the empty blast areas justify building less: they justify **deferring H and the dormant
memory product work — they do NOT justify weakening completion semantics.** An erasure receipt that
says `completed` when it is not is wrong whether or not any rows exist today. Equally, the explicit
`non_subject` classification and the schema-shape CI in story C are justified by the **present**
inventory drift, and are not over-engineering.

**So the epic splits.** A and B are ready for ticket-splitting: three rounds agree they are safe,
independently shippable and urgent. C→G are not ready — they carry a CRITICAL durability finding, an
unresolved trigger-privilege design, and two product/legal decisions (the backup boundary, and who
may obtain meeting content). They stay in design.

## 9. Round 4 — targeted review of §4c only (2026-07-26)

Scoped deliberately: the whole-document question had converged, so this round judged **only** the
two new designs. Verdict **SOUND-WITH-CHANGES** — 4 HIGH, 2 MEDIUM, all folded into §4c above.

What it changed, in order of how much:

1. **The barrier state has the same defect as the receipt.** I fixed the receipt's durability and
   left `users.erasure_state` — the thing the trigger consults — as a row that tenant deletion
   deletes. Fixing a durability bug in one record while leaving its enforcement state cascading is
   half a fix. → non-cascading subject registry.
2. **Three allowances OR-ed together are not a barrier.** One statement can satisfy the permitted
   `erasing→erased` transition *and* re-enable the account in the same UPDATE. → one conjunctive
   predicate over `OLD`+`NEW`, with an unchanged-columns requirement.
3. **The guarantee had to shrink.** `challenges.confirmer_actor` holds an email and
   `episodic_memory.solution` holds one inside JSON — a canonical-column trigger cannot see either,
   and after redaction neither can anything else. The design now *states* its boundary rather than
   implying total coverage.
4. **The tenant-delete guard was in the wrong place.** The phase loop has no enclosing transaction,
   so a guard at the users phase aborts after earlier phases have already committed. → interlock at
   initiation under a shared, fixed-order advisory lock.
5. **Decision ③ caught this document.** §7b had just recorded that a retained category without an
   expiry rule is a defect; §4c then retained raw ids indefinitely. → `retention_until` + keyed
   pseudonyms.
6. **Dropping the FKs is not free** — it invalidates cascade tests, a migration-ordering assumption
   and the tenant-delete drift guard, which needs a third classification.

Two things it confirmed rather than changed: §1a's corrected RLS framing is right (protective-in-
future, not effective against a `rolsuper`/`rolbypassrls` app role), and `ON CONFLICT DO UPDATE` is
not an intrinsic bypass so long as both trigger paths apply — now a regression test, not an
assumption.

**Status after round 4:** C→G are designed and reviewed once. They are not yet filed; the next step
is a ticket split against this §4c, not another design round.
