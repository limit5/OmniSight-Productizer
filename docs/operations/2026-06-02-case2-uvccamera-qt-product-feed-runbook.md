# Case 2 product-feed runbook — `customer:ft-c600 → limit5/UVCCamera_Qt`

**Date filed:** 2026-06-02
**Ticket:** OP-1914 (Case 2 / T2C.2)
**EPIC:** OP-1901 (Case 2 UVCCamera_Qt multi-vendor)
**Status:** operator-runnable today

---

## Why this runbook exists

Case 2 Sub-EPIC 2A + 2B + 2C.1 landed the vendor abstraction + 3 SoC
stub adapters + CI matrix on `limit5/UVCCamera_Qt` (PRs #2–#9, final
post-T2C.1 SHA = `6302bc93`). The Case 2 EPIC's closing deliverable is
to make the productizer's customer-delivery stack aware of this SHA so
the next B2 contribution against `customer:ft-c600` branches off the
correct base.

**Gap discovered during T2C.2 scope-discovery (2026-06-02):**
the originally-imagined `class ProductSource` with a `pinned_ref` field
**does not exist in source code**. It is defined ONLY in the design docs:

- `docs/operations/2026-05-30-P3-customer-delivery-layer-design.md` (P3.1
  ProductSource resolver — design)
- `docs/architecture/2026-06-02-case2-uvccamera-qt-multivendor-epic-design.md`
  (Case 2 EPIC — design references)

The runtime equivalent today is the `git_accounts` Postgres table (per
the operator memory note `project_backend_credentials_model` and
`backend/git_platform.py:77` comment block). This table holds the
`limit5/UVCCamera_Qt` repo URL + credentials, but it does NOT track a
"current good SHA" column today — every B2 contribution branches off
`main` directly via `git fetch origin main` inside `contribute_to_product`.

So "bump pinned_ref" reduces, in 2026-06-02 reality, to **two operator
verifications**:

1. **Verify** the `git_accounts` row for `customer:ft-c600` exists and
   points at `https://github.com/limit5/UVCCamera_Qt.git`.
2. **Verify** the next B2 contribution starts from the merged T2C.1 SHA
   (`6302bc93` or later) by inspecting `git log` on the worktree
   `contribute_to_product` creates.

When P3.1 ProductSource ships (separate epic), this runbook gets
replaced by a typed `ProductSource(...).set_pinned_ref(...)` call and
deleted. Until then this document is the canonical operator handoff.

---

## Step 0 — Inventory: what's in `git_accounts` today

Run against the prod backend's Postgres (read-only):

```bash
# adjust DATABASE_URL / kubectl exec for your environment
psql "$DATABASE_URL" -c "SELECT id, name, provider, url, scope, created_at \
   FROM git_accounts \
   WHERE url ILIKE '%uvccamera_qt%' OR name ILIKE '%ft-c600%' \
   ORDER BY created_at DESC;"
```

Expected output: **one row** with `name` containing `ft-c600` or `camviewpro-ro`
and `url = https://github.com/limit5/UVCCamera_Qt.git`. If the query returns
**zero rows**, jump to Step 1a. If it returns **one row**, jump to Step 1b.
If it returns **multiple rows**, surface to operator — duplicate routing risk
(see memory `feedback_jira_issuelink_direction` for similar additive-link
deadlock pattern).

---

## Step 1a — Register the row (if Step 0 found zero rows)

The backend exposes a `POST /git-accounts` endpoint (per
`backend/git_platform.py` provider router). Use the operator's
admin token:

```bash
# placeholders: ${ADMIN_TOKEN} from 1Password / vault; ${BACKEND_URL} = https://api.sora.app
curl -sS -X POST "${BACKEND_URL}/git-accounts" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ft-c600",
    "provider": "github",
    "url": "https://github.com/limit5/UVCCamera_Qt.git",
    "scope": "customer:ft-c600",
    "credential_ref": "github-camviewpro-token"
  }' | jq .
```

`credential_ref` must match the row in the credential vault that holds
the limit5 PAT (per `~/.config/omnisight/github-camviewpro-token` for
local dev mirrors; prod has its own vault entry). DO NOT inline the
token value — that would land it in the request log per the operator
rule `NEVER store API keys, tokens, or secrets in source code or commits`.

Re-run Step 0's `SELECT` to confirm the row landed.

---

## Step 1b — Re-verify URL + scope (if Step 0 found one row)

The row should already have:

| column          | expected value                                        |
|-----------------|-------------------------------------------------------|
| `provider`      | `github`                                              |
| `url`           | `https://github.com/limit5/UVCCamera_Qt.git`          |
| `scope`         | `customer:ft-c600` (NEW — see Step 1c if missing)     |
| `credential_ref`| points at the limit5 PAT vault entry                  |

If `scope` is empty or `camviewpro-ro` (a broader scope), patch it:

```bash
curl -sS -X PATCH "${BACKEND_URL}/git-accounts/${ROW_ID}" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"scope": "customer:ft-c600"}' | jq .
```

The narrower scope makes `customer:ft-c600` JIRA labels route here
deterministically (vs the broader `camviewpro-ro` which silently
mis-routed Case 1 contributions per OP-1862's root cause).

---

## Step 1c — `scope` column may not exist yet

If `PATCH` returns `400 unknown column scope`, the schema hasn't
landed the `scope` column. That's a P3.1 prerequisite. Workaround
for today: register a **second** narrow-scoped row alongside the
broader one, and let the runner pick the most-specific match per
the precedence rule documented in P3 design doc §3:

```bash
# Add a narrow-scoped row even if scope column is missing — the runner's
# Python-side `_camviewpro_project_key()` (per OP-1857) falls back to
# the row's `name` field for routing when `scope` is unavailable.
curl -sS -X POST "${BACKEND_URL}/git-accounts" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ft-c600-narrow",
    "provider": "github",
    "url": "https://github.com/limit5/UVCCamera_Qt.git",
    "credential_ref": "github-camviewpro-token"
  }' | jq .
```

When P3.1 ships its `scope` column, drop this duplicate row and use Step 1b.

---

## Step 2 — Verify the next B2 contribution starts from T2C.1's SHA

Trigger any benign Case 2 contribution (e.g. a no-op markdown tweak
ticket labelled `customer:ft-c600`). Once `contribute_to_product` runs:

```bash
# replace WORKTREE with the operator-visible runner worktree path
cd ${WORKTREE}
git log --oneline -5
git remote -v
```

Expected:
- `git remote -v` shows `origin https://github.com/limit5/UVCCamera_Qt.git`
- `git log --oneline -5` first line is the branch's first commit; second
  line is `6302bc93 Merge pull request #9 from limit5/feature/OP-1913-…`
  (the T2C.1 merge) or later.

If `git log` shows an OLDER base SHA (e.g. `6a13bfd Update GitLab token
reference`, which is pre-T2A.1), the runner's `contribute_to_product` is
caching a stale clone. Workaround: `rm -rf ${WORKTREE}` and re-trigger.
File a follow-up ticket against the runner cache layer if the stale
clone reproduces deterministically.

---

## Step 3 — Smoke test downstream consumers

For each downstream consumer of the `git_accounts` row, run its
canonical smoke test:

1. **B2 PR open** — file a tiny no-op `customer:ft-c600` ticket, watch
   the runner cycle: should branch from `main` (no `pinned_ref`
   override today), push to `limit5/UVCCamera_Qt`, open a PR.
2. **VendorAdapter probe** — once OP-1915 T2D.1 ships, run
   `python3 scripts/hil_probe.py` against the FT-C600 hardware; the
   FT-C600 row in the binary should match the FT-C600 row in the
   `git_accounts` table.
3. **CI matrix** — confirm GH Actions `build-matrix` workflow is
   visible on the limit5 repo and runs on every PR.

All three pass = Case 2 product-feed link is OPERATIONAL.

---

## What this runbook DOES NOT do

- Does NOT pin a specific SHA (`git_accounts` has no `pinned_ref` column
  yet — see Step 1c). Every B2 contribution branches from the live
  `main` tip. Pinning lands with P3.1 ProductSource.
- Does NOT auto-bump on every UVCCamera_Qt main commit. That belongs to
  a P3.1 follow-up scheduling job, not this ticket.
- Does NOT invoke `contribute_to_product` itself. Step 2 only **verifies**
  the next existing contribution lands on the right base.
- Does NOT change any source code or schema. Operator action only.

---

## Future migration (when P3.1 ProductSource ships)

This runbook gets replaced by a typed Python API:

```python
from omnisight.backend.product_source import ProductSource

ProductSource.upsert(
    customer="ft-c600",
    url="https://github.com/limit5/UVCCamera_Qt.git",
    pinned_ref="6302bc93",   # bumped per release; auto-managed by sync job
)
```

…and this file gets deleted with a commit referencing the P3.1 epic
that supersedes it. Until then this is the canonical handoff.

**P3.1 epic key:** TBD (file when P3.1 implementation starts; cross-link
both ways).

---

## Cross-references

- `docs/operations/2026-05-30-P3-customer-delivery-layer-design.md` — P3.1
  ProductSource design (the eventual canonical form)
- `docs/architecture/2026-06-02-case2-uvccamera-qt-multivendor-epic-design.md`
  — Case 2 EPIC design (which this runbook closes)
- `backend/git_platform.py:77+` — git_accounts table consumer
- Memory: `project_backend_credentials_model` (DB-side credential layout)
- Memory: `project_customer_delivery_capability_audit_2026_05_27`
  (Case 2 EPIC progress + pivot notes)
