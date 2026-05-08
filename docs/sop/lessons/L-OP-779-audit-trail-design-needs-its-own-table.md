---
id: L-OP-779
ticket: OP-779
title: Change-management audit trail needs its own table, not the tenant audit chain
date: 2026-05-08
tags: [audit, compliance, deploy, chain, schema]
---

# Change-management audit trail needs its own table, not the tenant audit chain

**Situation**: Sprint D D18 added an operator approval workflow with
mandatory reason text and a 1-year-retention compliance log of every
deploy / rollback / SLO breach / operator action. The first design
sketch reused `backend/audit.py`'s per-tenant hash chain by writing
deploy events under a synthetic `t-system` tenant. This was rejected
during review.

**Fix**: A dedicated `deploy_audit` table and `backend/deploy_audit.py`
module — same hash-chain primitive, single-linear chain (no per-tenant
branching, since deploys are global), tighter `CHECK (kind IN …)`
constraint to keep the four event classes from drifting, and its own
admin router (`/admin/deploy-audit` JSON + `.csv` + `/verify`) so
compliance reviewers do not have to reach across tenant boundaries.

**Verification**:
- `backend/alembic/versions/0204_deploy_audit.py` ships the schema
  with two indexes (`(ts DESC)` and `(kind, ts DESC)`) covering the
  two query shapes (1y window scan + per-kind filter).
- `backend/tests/test_alembic_0204_deploy_audit.py` and
  `backend/tests/test_deploy_audit_op779.py` cover the migration
  contract, the chain hash, the kind/status CHECK constraints, the
  reason-required validation for operator-initiated kinds, the CSV
  export shape, and the chain-verify tampering detector.

**Generalisation**: When a new audit surface has a different
**scope** (global vs tenant), a different **retention contract**
(1y vs forever), or a different **review audience** (external
compliance vs internal admin), give it its own table even if the
chain primitive is identical. Reusing the existing chain saves a
migration file but pushes the disambiguation cost onto every future
reader and every export tool — which is exactly the surface that
compliance review optimises against.

A concrete tell: if the synthetic-tenant trick (`t-system` /
`t-deploy`) shows up in the design, that's the cue that the event
class is global and a separate table is the right shape. The hash
chain is cheap to instantiate; cross-tenant audit pollution is not.
