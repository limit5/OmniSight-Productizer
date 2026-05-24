-- OP-1715 (Boreas-B A1 finding #7): DB-level dev->prod isolation.
-- Idempotent. Run against the PROD PostgreSQL (db=omnisight) as the omnisight
-- owner role. Belt to the app-level A2 env-contract guard (backend/env_contract.py):
-- even a misconfigured dev process pointed at the prod DSN cannot CONNECT.
--
-- Apply (operator, tier:X):
--   docker run --rm --network postgres-ha_pg-ha -e PGPASSWORD=<pw> \
--     omnisight-postgres-pgvector:16-alpine \
--     psql -h pg-primary -U omnisight -d omnisight -f - < scripts/db/prod-dev-db-isolation.sql
-- Verified applied + idempotent on prod 2026-05-25 (app role kept CONNECT; /readyz 200).

-- 1. The app/owner role MUST retain CONNECT (run FIRST so a re-run can never lock it out).
GRANT CONNECT ON DATABASE omnisight TO omnisight;

-- 2. PostgreSQL grants CONNECT to PUBLIC by default — revoke it so no role
--    inherits CONNECT implicitly (PUBLIC keeps only TEMP, which is harmless).
REVOKE CONNECT ON DATABASE omnisight FROM PUBLIC;

-- 3. Explicitly deny the dev role (defence-in-depth; it has no explicit grant anyway).
REVOKE CONNECT ON DATABASE omnisight FROM omnisight_dev;

-- 4. Verify (psql will print f/t): app=t, dev=f, public=f.
SELECT 'app(omnisight)'   AS role, has_database_privilege('omnisight','omnisight','CONNECT')     AS connect;
SELECT 'dev(omnisight_dev)' AS role, has_database_privilege('omnisight_dev','omnisight','CONNECT') AS connect;
SELECT 'public'           AS role, has_database_privilege('public','omnisight','CONNECT')        AS connect;
