# Ticket-filing draft — DSAR stories A and B

**Date:** 2026-07-26 · **Design:** `2026-07-26-dsar-subject-ownership-epic-design.md` (v5) ·
**Scope label:** `scope:failed-units-2026-07-25`

Only A and B were drafted here. Three adversarial rounds agreed they are safe, independently
shippable and urgent; C→G carried a CRITICAL durability finding, an unresolved trigger-privilege
design and two product decisions, and stayed in design (§8 of the design doc).

### Filed — 2026-07-26

| ticket | what | state |
|---|---|---|
| **OP-2745** | story A — containment (this draft, ticket A) | filed, `tier:X` |
| **OP-2746** | story B — human-session gate (this draft, ticket B) | filed, `tier:X` |
| **OP-2747** | META — records the four operator decisions | filed, `meta:decision-gate` |
| **OP-2748** | repoint the account UI at `/privacy/*` (decision ④) | filed, *is blocked by* OP-2746 |
| **OP-2749** | participant attribution schema (decision ①, story H) | filed, **dormant** — data-model change, takes the safe-floor position; it is what later lifts OP-2745's containment |

C, D, E and G remain unfiled **on purpose**: OP-2747 states that children are filed only where the
design is settled. Their designs are in §4c of the design doc and have not yet been reviewed.

Stage 5 (runner blind-test) is **N/A**: both tickets are `tier:X`, human-driven. The blind-test
exists to catch goal-drift in an `agent:auto` runner picking up a ticket cold; with no runner in the
loop there is nothing to drift. Recorded rather than skipped silently.

---

## Ticket A — `[OP][security][privacy] Stop per-user DSAR from reading and deleting a co-tenant's transcripts`

**type** bug · **tier** X · **areas** backend, tests, security, docs · **priority** High

### Goal
`POST /privacy/{access,portability}` return **every** meeting and transcript segment in the
subject's tenant (`privacy.py:191-201`), and `POST /privacy/erasure` **deletes** them tenant-wide
(`:317-320`). Neither table carries a user or participant column (`0249_bi0_transcripts.py:58`), so
tenant scope was chosen as "the only correct scope on read" — a documented, deliberate decision. The
consequence is not defensible either way: **one subject's self-service request discloses, and
destroys, another subject's content.** Marking the rows "tenant-scoped" in the envelope does not
cure disclosure.

Remove both tables from the three self-service per-user endpoints. Access and portability stop
returning them; erasure stops deleting them.

### Files / Paths
- `backend/routers/privacy.py` — drop the `meetings` / `transcript_segments` fetches (`:191-201`,
  `:221-222`) and the whole `_ERASURE_TENANT_DELETE_STATEMENTS` tuple (`:301-321`) from the per-user
  path
- `backend/tests/test_op2239_bi0b_retention_dsar_audit.py` — **reverse** the two assertions that
  currently affirm the behaviour being removed (module docstring lines 5-7; the erasure and
  portability cases)
- `docs/operations/` — record the resulting limitation for the receipt text

### Spec references
- Design doc §1c, §4 story A. `docs/sop/architecture-anti-patterns.md` has no matching pattern; this
  is a scope defect, not a structural one.

### Out of scope / **MUST NOT**
**MUST NOT** add participant attribution, a tenant-admin export, or any new way to obtain meeting
content. This ticket only *removes* an over-broad scope. Restoring subject access to their own
meeting content requires a participant relationship that does not exist yet (design story H) — do
not invent one here, and do not "fix" the resulting gap by widening some other endpoint.

### Acceptance
1. **Code** — neither table is read or deleted by any `/privacy` route.
   verify: `grep -nE "meetings|transcript_segments" backend/routers/privacy.py` returns only
   comments explaining the exclusion.
2. **Integration** — a same-tenant test: two users in ONE tenant; subject A's access and
   portability must not contain B's meeting or segment, and A's erasure must leave them intact.
   verify: `pytest backend/tests/test_op2239_bi0b_retention_dsar_audit.py -q`
3. **Exercised** — the two pre-existing assertions that affirm tenant-wide behaviour are reversed
   in this same change, not left failing.
4. **Go-Live** — the DSAR receipt states that tenant-scoped meeting artefacts are outside the
   per-user response, so the omission is disclosed rather than silent.

**Sequencing note:** code and tests land in **one mergeable change**, not necessarily one commit —
and if test mechanics lag, the containment still ships. "One commit or neither" would prefer
continued destructive behaviour over untested containment.

---

## Ticket B — `[OP][security][privacy] DSAR routes must require a real human session, not a bearer-minted admin`

**type** bug · **tier** X · **areas** backend, tests, security · **priority** High

### Goal
All three DSAR routes are `Depends(auth.current_user)` and nothing else (`privacy.py:358, 402,
444`). `current_user` mints a synthetic **admin** for any API-key bearer
(`auth.py:1846-1849`: `User(id=f"apikey:{key.id}", …, role="admin")`) with `csrf_token=""`, and
`csrf_check` is reachable only through `require_role` (`auth.py:1940-1946`), which `privacy.py` does
not use — so these routes have **no CSRF and accept a machine principal as an admin**.

Add `require_human_session`: a cookie-backed session, **no `Authorization` header accepted**,
`request.state.session.user_id == user.id`, CSRF enforced, and **no role floor**.

### Files / Paths
- `backend/auth.py` — new `require_human_session` dependency, independent of `current_user`'s
  bearer precedence; fails closed in open mode
- `backend/routers/privacy.py` — the three route signatures
- `backend/tests/` — new cookie-based tests (see AC2)

### Spec references
- Design doc §1b, §3, §4 story B.

### Out of scope / **MUST NOT**
**MUST NOT** use `require_operator` or `assert_human_principal`. These endpoints are **self-service
for the subject** — the docstrings say so (`privacy.py:3-11`) and the integration fixture creates
the subject as a **`viewer`** (`test_dsar_access.py:21,265`). A role floor would deny a viewer their
statutory self-service right, and `assert_human_principal` infers humanity from names and emails,
which can deny a real person. **MUST NOT** build the support-acting-for-another-subject workflow
here; that needs a distinct `actor_id` / `subject_user_id` / reason / audit design.

### Acceptance
1. **Code** — the three routes depend on `require_human_session`; a request carrying an
   `Authorization` header is rejected even when the bearer is valid.
2. **Integration** — real-cookie tests covering: viewer succeeds; missing CSRF rejected; invalid
   CSRF rejected; API-key bearer rejected; anonymous / open-mode rejected; a session whose
   `user_id` differs from the resolved user rejected.
   verify: `pytest backend/tests/ -k dsar -q`
3. **Exercised** — the existing DSAR tests currently bypass authentication by overriding
   `current_user` (`test_dsar_access.py:247`); at least one test must exercise the real dependency
   rather than an override, or the gate is unproven.
4. **Go-Live** — no first-party caller breaks. Verified precondition: the browser helper already
   uses session cookies + CSRF (`lib/api.ts:921`), and **the account UI calls
   `/auth/account/export` and `/auth/account/delete`, which do not exist in the backend at all** —
   so no working UI flow depends on these routes today.

### Known adjacent defect, deliberately NOT fixed here
The account UI calls two endpoints the backend does not implement (0 matches under
`backend/routers/`). The self-service DSAR flow is therefore not wired end to end regardless of this
gate. File separately; do not widen B to chase it.

---

## Dependency

`A` and `B` are independent and may land in either order or in parallel. Neither depends on C→G.
Both are `tier:X`, so neither is runner-pickable.
