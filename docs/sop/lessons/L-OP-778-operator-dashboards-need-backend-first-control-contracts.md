---
id: L-OP-778
ticket: OP-778
title: Operator dashboards need backend-first control contracts
date: 2026-05-08
tags: [operator-ux, release, dashboard, sse]
---

# Operator dashboards need backend-first control contracts

**Situation**: OP-778 asked for an operator deployment dashboard with
current production tag, in-flight progress, release history, rollback,
and canary controls. The ticket prompt for this run explicitly forbade
frontend changes, so implementing only visual components would have
either violated scope or produced an unverified shell.

**Fix**: Define the backend dashboard contract first:
`GET /admin/releases` returns the current tag, in-flight deploys,
milestone health, last 20 deploy/rollback rows, audit messages, and
canary state. `POST /admin/releases/rollback` and
`POST /admin/releases/canary/control` expose the operator actions, and
`release.dashboard.updated` carries real-time progress/control updates
over the existing SSE bus.

**Verification**: `backend/tests/test_release_dashboard_op778.py`
covers the snapshot projection, rollback command/audit behaviour,
SSE synthetic progress payload, and admin router contract.

**Generalisation**: For operator dashboards, ship the control contract
before the visual shell when scope is split. The UI can stay thin if
the backend response already answers "what is live?", "what is moving?",
"what happened?", and "what action was just taken?" in one pollable and
SSE-refreshable shape.
