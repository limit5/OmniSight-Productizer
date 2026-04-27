# AS.0.3 — Account-Linking Security Policy

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Scope**: Schema scaffold (`users.auth_methods`) + canonical
> takeover-prevention policy module (`backend/account_linking.py`).
> **Not in this row**: OAuth client implementation (AS.1), MFA
> enforcement composition (handled at router layer), unlink UI (AS.7).

## 1. Threat the rule defends against (R31 in design doc §10)

The classic OAuth-link account-takeover sequence:

1. Victim has an OmniSight account `foo@x.com` with a password.
2. Attacker registers `foo@x.com` at an OAuth IdP.  Realistic
   vectors: DNS hijack of a discontinued domain, IdP signup
   flow that doesn't enforce email verification (GitHub historically
   allowed unverified primaries), domain re-purchase after the
   victim's company shut down a subdomain, social engineering an
   admin at the IdP.
3. Attacker clicks "Sign in with Google" on OmniSight.  Naive
   auto-link logic binds the attacker's IdP subject to the
   victim's user row (because the IdP-asserted email matches).
4. Attacker is now logged in as the victim.

The fix codified by AS.0.3: **before adding any new auth method
to a user that already carries `"password"`, the caller must
prove control of the password.**

## 2. Schema (alembic 0058)

Column: `users.auth_methods`

| DB | Type | Default |
|---|---|---|
| PG | `jsonb NOT NULL` | `'[]'::jsonb` |
| SQLite (dev) | `TEXT NOT NULL` (JSON-encoded) | `'[]'` |

Backfill: any existing row with `password_hash <> ''` is set to
`'["password"]'`; rows with empty `password_hash`
(invited-but-not-completed) stay at `[]`.  Operator hand-edits
are preserved (the backfill UPDATE only matches rows still at
the column DEFAULT).

Method-tag vocabulary:

| Tag | When emitted |
|---|---|
| `"password"` | User has a password set on this account. |
| `"oauth_<provider>"` | User has bound an OAuth identity for `<provider>` ∈ `{google, github, apple, microsoft}`. AS.1 emits these via `link_oauth_after_verification`. |

The vocabulary is **enforced** by `account_linking.is_valid_method`
— any caller that hand-rolls `UPDATE users SET auth_methods = ...`
bypasses this check and is, by AS.0.3 contract, a bug.

The vocabulary deliberately does NOT include `mfa_*` or
`api_key` even though the `as_0_1` inventory mentioned them as
candidate enums.  Those are second-factor / out-of-band
mechanisms with their own tables (`user_mfa`, `api_keys`); the
`auth_methods` column is the **first-factor** registry only.

## 3. Policy module API (`backend.account_linking`)

```python
# Read
get_auth_methods(conn, user_id)         -> list[str]
has_method(conn, user_id, method)       -> bool
is_oauth_only(conn, user_id)            -> bool      # case C gate

# Write — low-level
add_auth_method(conn, user_id, method)         -> list[str]
remove_auth_method(conn, user_id, method)      -> list[str]

# Takeover-prevention guard
require_password_verification_before_link(
    conn, user_id, presented_password,
) -> None  # raises PasswordRequiredForLinkError on failure

# One-shot wrapper bundling guard + add
link_oauth_after_verification(
    conn, user_id, oauth_method, presented_password,
) -> list[str]

# INSERT-path helpers
initial_methods_for_new_user(*, password, oauth_methods=())  -> list[str]
encode_methods_for_insert(methods)                           -> str
```

## 4. Router-layer integration (canonical patterns)

### 4.1 OAuth login — case A (existing password user, takeover-prone)

```python
# AS.1 OAuth callback handler — illustrative, lives in AS.1 row
existing = await find_user_by_email(conn, idp_email)
if existing and "password" in await get_auth_methods(conn, existing.id):
    # 200 with a "type your existing password to confirm" form;
    # frontend re-submits to the link endpoint with the password.
    return need_password_confirmation(existing)
# else case B (new user) or case C (oauth-only) — handled below.
```

The link endpoint that receives the confirm-password form:

```python
try:
    await link_oauth_after_verification(
        conn, user.id, "oauth_google", form.password,
    )
except PasswordRequiredForLinkError:
    raise HTTPException(401, "password verification failed")
```

### 4.2 OAuth login — case B (brand-new email)

```python
# Create the user with the OAuth method seeded from the start —
# no password verification needed because there is no existing
# password to verify against.
methods = initial_methods_for_new_user(
    password=None, oauth_methods=["oauth_google"],
)
await create_oauth_only_user(conn, idp_email, methods)
```

### 4.3 OAuth login — case C (existing OAuth-only user, no password)

```python
# Just bind the IdP session — same provider already in
# auth_methods, no takeover risk.  Future password-reset
# request is rejected via is_oauth_only() check below.
```

### 4.4 Password-reset endpoint (AS.6.1 / future)

```python
if await is_oauth_only(conn, user.id):
    raise HTTPException(
        400,
        "OAuth-only account, manage credentials at provider",
    )
```

### 4.5 Password change (existing route, AS.0.3-aware)

`backend.auth._change_password_impl` already calls
`add_auth_method(conn, user_id, "password")` after each successful
update so an invited-but-not-completed user that finally sets a
password gets `"password"` recorded in their methods array.
Idempotent for users that already had it.

## 5. What AS.0.3 deliberately leaves dormant

* **No live caller emits OAuth method tags yet** — AS.1 ships
  the OAuth client.  The helper module accepts `oauth_<provider>`
  tags, but no production INSERT path writes them in this row.
  Schema + helper are land-now-so-AS.1-can-just-call patterns.
* **No password-reset endpoint** — `/auth/reset` and
  `/auth/forgot` are allowlist ghosts (per `as_0_1` inventory
  §1.2).  AS.6.1 will land them and call `is_oauth_only` as
  the case-C gate.
* **No unlink UI** — AS.7 ships the integration-settings
  unlink button.  `remove_auth_method` is the API target
  it will hit.

## 6. Production status

* Migration: alembic 0058, dev-green only.
* Helper module: in-tree, exercised by contract tests only —
  no production caller writes OAuth tags yet.
* Backfill: existing prod tenant tenants get
  `["password"]` on every row whose `password_hash <> ''`.
* Rollback: `OMNISIGHT_AS_ENABLED=false` does NOT need to gate
  this column — the column is read-only for non-AS code paths,
  and an empty / `["password"]` array drives no behavior change
  on the password-only login flow.

**Production status: dev-only**
**Next gate**: deployed-inactive once the alembic chain
(`… → 0056 → 0058`) runs against prod PG.

## 7. Cross-references

* Design doc §3.3 — the canonical statement of the takeover rule.
* `docs/security/as_0_1_auth_surface_inventory.md` §6 row 5 —
  inventory note that prompted the explicit-false JSONB pattern.
* Risk register §10 entry **R31** — OAuth account takeover via
  email collision.
* `backend/account_linking.py` — the policy module itself.
* `backend/alembic/versions/0058_users_auth_methods.py` — schema.
