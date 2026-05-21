# Release-train hotfix stop-the-line policy (RT-18 / ADR-0040)

**Status**: Accepted 2026-05-22. Operator policy for the single-trunk Release Train (ADR-0040). Resolves ADR-0040 BLOCKING design hole #3 ("hotfix isolation gap").

## The problem this addresses

Under the single-trunk model (`develop` only; no `release/*` stabilisation branch),
a release is a candidate built from a chosen `develop` SHA and promoted by digest.
**A hotfix cut from `develop` tip therefore ships every commit landed on `develop`
since the last release — not just the fix.** Without a rule, an urgent prod fix
would drag along whatever unvetted work is sitting on `develop`.

## Policy (in priority order)

**P1 — Keep `develop` always releasable (the real fix).** Frequent trains +
per-SHA green-cut + feature flags mean `develop` tip is almost always shippable,
so a hotfix is just "cut the next train from develop tip". This is the default and
the preferred state. If this holds, no special handling is needed.

**P2 — If `develop` is NOT releasable and you need ONLY the fix, STOP THE LINE.**
When a prod-critical bug must ship but `develop` carries unshipped/unsafe commits
since the last release:
1. **Freeze merges to `develop`** (stop-the-line) — no new feature submits until the
   hotfix train ships. Announce it.
2. **Choose isolation path:**
   - **(a) Revert-unready (preferred):** revert or feature-flag-OFF the unsafe
     `develop` commits, land the fix, cut the train from the now-clean `develop` tip.
   - **(b) Build-from-last-release exception:** build a candidate from the **last
     released commit** (the SHA behind the current prod `vX.Y.Z`) with **only the
     fix cherry-picked** onto a detached build context (`CANDIDATE_SHA` =
     last-release SHA + fix). Validate on staging, promote to `vX.Y.(Z+1)`. This is
     a deliberate, audited exception — record why P2(a) was not used.
3. **Resume the line** after the hotfix train is promoted.

**P3 — Never bypass the gates for speed.** A hotfix tag is still a normal train:
it MUST pass the per-SHA green gate, staging digest validation, and the
expand/contract migration gate (RT-04*/RT-05*/RT-11). "Urgent" does not waive the
gates; it raises the priority of getting through them. Emergency override =
audited human break-glass only (RT-22), never an unattended skip.

## Owner & decision

The **release manager / operator** decides P1 vs P2 and, within P2, (a) vs (b),
and records the decision + reason on the hotfix release META. No unattended
automation makes this choice.

## Relations
- ADR-0040 (single-trunk release train) §"BLOCKING design holes" #3 + §"Decisions LOCKED".
- RT-22 (force/break-glass policy implementation) — the audited override path.
- RT-04*/RT-05*/RT-11 — the gates a hotfix train must still pass.
