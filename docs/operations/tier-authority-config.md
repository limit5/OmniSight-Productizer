# Tier Authority Config — Operator Runbook

**Audience:** governance / SRE / on-call operators who edit
`configs/governance/tier-paths.yaml`.

**Scope:** how to add, remove, or audit a path in the tier-authority
mapping that gates AI-vs-human merge authority. Everything below is
about *operating* the file. The *why* (4-layer protection model,
S/M/L/X semantics, submit-rule integration) is in
[ADR-0005](../adr/ADR-0005-tier-authority-levels.md). Read the ADR
first if you have not already.

> **Related deliverables (do not edit alongside without coordination):**
> - `configs/governance/__schema__/tier-paths.schema.json` — JSON Schema
>   the YAML must validate against
> - `backend/tests/test_tier_paths_yaml_schema.py` — commit-time gate
>   that locks the contract
> - G2 backend classifier (uses this file to compute a patchset's tier)
> - G3 Gerrit server hook (overwrites the user-set Tier label per
>   patchset upload using this file)
> - G4 audit log (records the matched glob alongside the assigned tier)

---

## 1. What this file controls

`configs/governance/tier-paths.yaml` maps *paths touched by a
patchset* to one of four AI-authority tiers:

| Tier | What it means | Merge gate |
|------|---------------|-----------|
| **S** | Safe — AI may self-+2 | AI bot +2 sufficient (24h revert window) |
| **M** | Medium — default | AI +1 + human +1 (dual-+1) |
| **L** | Large — auth/security/schema | AI +1 advisory + human +2 mandatory |
| **X** | Extreme — deploy/CI/license | Human +2 + architectural reviewer +1 |

The tier ratchets up; a reviewer can promote (S → M, M → L, L → X)
but cannot demote.

## 2. Resolution order (the load-bearing rule)

For a patchset touching paths `P1, P2, ..., Pn`:

1. **Force-upgrade scan.** If any `Pi` matches an entry in
   `tiers.x.force_upgrade_globs`, the patchset is **Tier X**. Else if
   any `Pi` matches `tiers.l.force_upgrade_globs`, the patchset is
   **Tier L**.
2. **Tier S whitelist gate.** If no force-upgrade matched and **every**
   `Pi` matches an entry in `tiers.s.whitelist_globs`, the patchset is
   **Tier S**.
3. **Tier M (default).** Otherwise — including the very common case of
   "one whitelisted file plus one un-classified file" — the patchset
   is **Tier M**.

The deny-by-default for Tier S and the unconditional bump for L/X
are the layers that defend against AI misclassification (ADR-0005 §4).

## 3. Adding a new path

### 3.1 Decide the target tier

| If the path is… | Add to… |
|---|---|
| Mechanically generated; rollback is "delete the file"; mistake is cosmetic only | `tiers.s.whitelist_globs` |
| Production code with normal review needs | *Don't add anywhere.* Tier M is the implicit fallback — adding to S or L without a concrete reason erodes the gate. |
| Any auth / crypto / security / schema surface | `tiers.l.force_upgrade_globs` |
| Anything that gates merges, ships builds, or moves money | `tiers.x.force_upgrade_globs` |

When in doubt, **promote, don't demote**. Adding to L when M would
have done is a small attention tax. Adding to S when L was correct
is a security incident.

### 3.2 Edit the YAML

Globs are POSIX-form (forward slashes), repo-relative, fnmatch with
`**` for recursive descent. Examples:

```yaml
tiers:
  l:
    force_upgrade_globs:
      - "backend/security/**"     # any file under that dir, recursive
      - "backend/auth*.py"        # auth.py, auth_helpers.py at that level
  x:
    force_upgrade_globs:
      - ".github/workflows/**"
      - "scripts/deploy-*.sh"
```

Each list must be **non-empty** (the schema rejects empty lists) and
must contain **unique** entries (the schema enforces `uniqueItems`).

### 3.3 Update anchor-glob assertions if the path replaces one

`backend/tests/test_tier_paths_yaml_schema.py::TestAdr0005AnchorGlobs`
locks the minimum-required globs from ADR-0005 + OP-803. **Adding** a
new glob is fine — the test only requires that the anchors are
present, not that the list is exactly the anchor set. **Removing** an
anchor will fail this test loudly (by design — see §4 below).

### 3.4 Run the schema test locally before pushing

```sh
pytest backend/tests/test_tier_paths_yaml_schema.py -v
```

Six test groups must pass: file presence, schema validation,
all-four-tiers-present, ADR-0005 anchor globs, glob hygiene
(no backslash / leading slash / duplicates), schema-version pin.

### 3.5 Push as a Tier X change

Editing `configs/governance/**` itself is governance / merge-gate
infrastructure, so the patchset edit is **Tier X** (human +2 +
architectural review). The submit-rule-defined gate forces this on
the merge side; the runbook side is "expect a slow review and don't
batch other unrelated work in the patch".

## 4. Removing a path

**Don't, unless you intend to weaken the gate.**

Removing a Tier L or Tier X glob means changes touching that path
will fall through to Tier M (or even S if every other touched path
is whitelisted). That is, by definition, a downgrade of the merge
gate for an entire class of changes.

If you genuinely need to remove an anchor (e.g. the path no longer
exists in the repo, or has been renamed):

1. Open an ADR amendment patchset alongside the YAML edit. The
   architectural-review tier (X) requires the human reviewer to see
   the rationale.
2. Update `backend/tests/test_tier_paths_yaml_schema.py` to drop
   the anchor from `TestAdr0005AnchorGlobs` in the **same patchset**
   as the YAML edit. The test failing is the system flagging an
   unintentional removal — disabling it without explanation defeats
   the guard.
3. Search for the glob string in the codebase
   (`git grep -F "<glob>"`) and update any consumer (G2 classifier,
   G3 hook, audit log labels) that hardcoded the old path.

If the path was *renamed*, prefer **replacing** the glob in the same
patchset (old removed, new added) rather than two separate patches —
the in-between state would let the renamed path fall to Tier M.

## 5. Auditing what tier a change would land at

Until G2 ships a `--dry-run` CLI, the manual algorithm is:

1. List the patchset's touched paths
   (`git diff --name-only origin/main...HEAD`).
2. Check each path against `tiers.x.force_upgrade_globs` (any match → X,
   stop).
3. Check each path against `tiers.l.force_upgrade_globs` (any match → L,
   stop).
4. Check whether **every** path matches `tiers.s.whitelist_globs`. If
   yes → S. If even one path is unmatched → M.

Once G2 ships, this becomes:

```sh
# Subject to API stability — see docs/sop for the eventual command
$ ./scripts/tier-classify <ref>
Tier=L  matched-rule=tiers.l.force_upgrade_globs:'backend/auth*.py'
        offending-paths=['backend/auth_oidc.py']
```

## 6. Common mistakes and how to avoid them

| Mistake | Symptom | Fix |
|---|---|---|
| Adding a path to S that touches production code paths | Production change merges with AI-only review | Move the glob to L instead |
| Forgetting to add to BOTH L and the consumer code | G2 classifier misses a new sensitive path | Always pair YAML edit with a G2 test that locks the new glob |
| Using `backend/auth_*.py` (underscore) instead of `backend/auth*.py` | `backend/auth.py` itself is not matched | Stick to the `auth*.py` form unless you genuinely want to exclude `auth.py` |
| Using a leading slash (`/deploy/**`) | Schema rejects on commit | Drop the leading slash; globs are repo-relative |
| Editing the YAML without bumping the consumer / test | Schema test passes; G2 quietly diverges | Edit consumers / tests in the same patchset |

## 7. When to bump `schema_version`

Only on an **incompatible** structural change — e.g. renaming
`whitelist_globs` to `whitelist_patterns`, splitting `tiers.l` into
sub-categories. In that case, bumping is a coordinated multi-file
patchset:

1. Update `configs/governance/__schema__/tier-paths.schema.json`
   (`schema_version.const`).
2. Update `configs/governance/tier-paths.yaml` (`schema_version:`).
3. Update the G2 loader to handle the new shape.
4. Update `backend/tests/test_tier_paths_yaml_schema.py`'s
   `TestSchemaVersionPinned::test_schema_version_is_one` to the new
   number.
5. Open an ADR amendment if the change affects the user-visible
   tier resolution semantics.

Adding a new glob, removing an obsolete glob, or rewriting an
existing glob does **not** bump `schema_version`.

## 8. Related documents

- [ADR-0005 — Tier S/M/L/X AI authority levels](../adr/ADR-0005-tier-authority-levels.md)
  — model rationale + 4-layer protection + submit-rule integration.
- [ADR-0003 — Gerrit code review](../adr/ADR-0003-gerrit-code-review.md)
  — base merge gate (AI +1 / human +2 default).
- [docs/sop/jira-ticket-conventions.md](../sop/jira-ticket-conventions.md)
  §11 — discovered-dependency protocol if a tier edit reveals an
  out-of-area constraint.
- `configs/governance/__schema__/tier-paths.schema.json` — the
  authoritative schema. Read this before editing the YAML — invalid
  documents fail at commit time.
