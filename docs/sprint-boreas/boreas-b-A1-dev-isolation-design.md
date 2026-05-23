# Boreas-B A1 — isolated dev env + provable DB isolation (OP-1642) — for codex review

**Status**: DRAFT 2026-05-24 for codex security review BEFORE touching live prod PG.

## Goal
A same-host isolated DEV stack (mirror deploy/staging/) + the PROVABLE prevention that a dev
process can never mutate the prod DB — via PostgreSQL role/grant separation, not just env-string
checks (codex's Boreas-B review: "the only provable prevention if a process bypasses the shell
guard").

## Current PG facts (pg-primary)
- Roles: `omnisight` (SUPERUSER, owns + connects the prod app), `replicator` (NOT superuser, HA streaming).
- DBs: `omnisight` (prod, owner omnisight), `omnisight_test`, `postgres`.
- **`omnisight` DB datacl = NULL → PUBLIC has default CONNECT** ⇒ ANY role (incl a future
  `omnisight_dev`) can currently connect to prod. This is the hole to close.
- Prod backend connects as the `omnisight` SUPERUSER (separate concern; superuser bypasses ACL).

## Design

### Part 1 — provable prod-DB lockdown (the security crux, on pg-primary)
```sql
-- Close the PUBLIC-connect hole on the prod DB:
REVOKE CONNECT ON DATABASE omnisight FROM PUBLIC;
GRANT  CONNECT ON DATABASE omnisight TO omnisight;     -- app (also superuser, but explicit)
GRANT  CONNECT ON DATABASE omnisight TO replicator;    -- HA streaming MUST keep working
```
After this, only `omnisight` + `replicator` (+ any superuser, which bypasses ACL) can connect to
prod. A non-superuser `omnisight_dev` role CANNOT connect to the prod DB → provable dev→prod block.

### Part 2 — dev role + dev DB (own postgres container, mirror staging)
- A dedicated dev postgres container (like staging's), DB `omnisight_dev`, role `omnisight_dev`
  `LOGIN NOSUPERUSER NOCREATEDB` owning only `omnisight_dev`.
- Dev stack connects as `omnisight_dev` to its own `omnisight_dev` DB. Even if a dev process is
  MISconfigured to point at pg-primary, Part 1's REVOKE makes the connect fail closed.

### Part 3 — dev compose stack (mirror deploy/staging/)
- `deploy/dev/docker-compose.yml` — project `omnisight-dev`, own ports (e.g. 58432/19080),
  own volumes, own postgres + backend(+frontend), `OMNISIGHT_ENV=dev`.
- systemd `omnisight-dev-compose.service` (like staging), Linger, env-contract preflight.

## Safety / risk
- The REVOKE is on LIVE prod PG. Mitigations: (a) prod app is superuser → unaffected by the
  REVOKE (superuser bypasses ACL); (b) replicator gets an explicit GRANT so HA streaming keeps
  working; (c) fully reversible (`GRANT CONNECT ... TO PUBLIC`); (d) pg_dump first per SOP.
- Verify AFTER: prod /readyz still ready; replication still streaming; omnisight_dev CANNOT
  connect to prod (`psql -U omnisight_dev -d omnisight` → permission denied).

## Open questions for codex
1. Is `REVOKE CONNECT ON DATABASE omnisight FROM PUBLIC` + explicit GRANT to omnisight+replicator
   SAFE on the live HA prod (does the standby/replicator connect to the DB or only the WAL
   stream — i.e., does replicator need CONNECT on the `omnisight` DB, or is its streaming
   replication privilege separate)? Will this break pg-standby?
2. Is there any OTHER non-superuser role/consumer that connects to prod `omnisight` and would lose
   access (pgbouncer? backup? monitoring? the OP-1640 backup uses pg_dump as omnisight superuser
   — unaffected)?
3. Does the prod app being SUPERUSER undermine the isolation (should A1 also de-superuser the app
   role, or is that out of scope / too risky now)?
4. Same-host dev postgres vs reuse pg-primary-with-omnisight_dev-DB — which is the right isolation
   for "dev" (staging uses its own container)? Any reason dev shouldn't mirror that?
5. Anything that makes this REVOKE unsafe on live prod that the plan missed.
