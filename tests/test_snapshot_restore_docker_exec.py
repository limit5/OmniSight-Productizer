"""[OP-1033] snapshot-restore.sh uses container-local psql.

These tests keep the AUDIT-29e fix hermetic: a fake docker binary emulates the
Postgres containers, so the restore script can run its happy path without a
host psql binary, Docker daemon, or real database.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTORE_SH = REPO_ROOT / "infra" / "staging" / "snapshot-restore.sh"
SERVICE_UNIT = REPO_ROOT / "deploy" / "systemd" / "staging-pg-snapshot.service"


SYNTHETIC_DUMP = r"""--
-- synthetic anonymized restore fixture
--
CREATE TABLE public.users (
    id text NOT NULL,
    email text NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    password_hash text DEFAULT ''::text NOT NULL,
    oidc_provider text DEFAULT ''::text NOT NULL,
    oidc_subject text DEFAULT ''::text NOT NULL,
    role text DEFAULT 'viewer'::text NOT NULL
);
COPY public.users (id, email, name, password_hash, oidc_provider, oidc_subject, role) FROM stdin;
u1	alice@example.com	Alice	hash	oidc	subject	admin
\.
"""


def _fake_docker(tmp_path: Path) -> Path:
    docker = tmp_path / "docker"
    dump = tmp_path / "dump.sql"
    dump.write_text(SYNTHETIC_DUMP)
    docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"

            case "$*" in
              *" pg_isready "*)
                exit 0
                ;;
              *" pg_dump "*)
                cat "${FAKE_DOCKER_DUMP:?}"
                exit 0
                ;;
              *"SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"*)
                printf 'users\\n'
                exit 0
                ;;
              *"SELECT 1 FROM pg_database WHERE datname = 'omnisight_staging'"*)
                exit 0
                ;;
              *"SELECT 1 FROM pg_database WHERE datname = 'omnisight_staging_prev'"*)
                exit 0
                ;;
              *"SELECT 1"*)
                printf '1\\n'
                exit 0
                ;;
              *"information_schema.tables"*)
                printf '1\\n'
                exit 0
                ;;
              *"email IS NOT NULL"*)
                printf '0\\n'
                exit 0
                ;;
              *"pg_terminate_backend"*|*"DROP DATABASE"*|*"ALTER DATABASE"*|*"CREATE DATABASE"*)
                exit 0
                ;;
              *" psql "*)
                cat > "${FAKE_DOCKER_LOADED:?}"
                exit 0
                ;;
            esac
            exit 0
            """
        ).lstrip()
    )
    docker.chmod(0o755)
    return docker


def test_snapshot_restore_happy_path_uses_docker_exec_without_host_psql(tmp_path: Path):
    log = tmp_path / "docker.log"
    loaded = tmp_path / "loaded.sql"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DOCKER_BIN": str(_fake_docker(tmp_path)),
        "PSQL_BIN": str(tmp_path / "missing-host-psql"),
        "PROD_PG_CONTAINER": "prod-pg",
        "STAGING_PG_CONTAINER": "staging-pg",
        "SNAPSHOT_WORKDIR": str(tmp_path / "scratch"),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_LOADED": str(loaded),
        "FAKE_DOCKER_DUMP": str(tmp_path / "dump.sql"),
    }

    proc = subprocess.run(
        ["bash", str(RESTORE_SH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    docker_calls = log.read_text()
    assert "exec -i -e PGPASSWORD= staging-pg psql -U omnisight -d omnisight_staging" in docker_calls
    assert "missing-host-psql" not in docker_calls
    assert loaded.exists(), "anonymized dump should be streamed into container psql stdin"
    loaded_sql = loaded.read_text()
    assert "redacted-" in loaded_sql
    assert "UPDATE \"public\".\"users\"" in loaded_sql
    assert "status=ok" in proc.stderr


def test_snapshot_restore_contract_has_no_host_psql_dependency():
    text = RESTORE_SH.read_text()
    assert 'command -v "$PSQL_BIN"' not in text
    assert ' "$PSQL_BIN"' not in text
    assert "docker exec staging-postgres-1 psql" in text
    assert "STAGING_PG_CONTAINER" in text

    unit = SERVICE_UNIT.read_text()
    assert "STAGING_PG_CONTAINER=staging-postgres-1" in unit
