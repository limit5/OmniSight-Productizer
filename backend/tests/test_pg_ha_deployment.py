"""G4 #3 — PostgreSQL primary + hot-standby deployment contract tests.

TODO row 1362:
    部署 primary + hot standby（`streaming replication`、
    `synchronous_commit=on` 可設）

Pins the artefacts under ``deploy/postgres-ha/`` that enable streaming
replication between an OmniSight PostgreSQL primary and a hot standby,
with the ``synchronous_commit`` knob exposed to operators as a
documented flip-switch (default async, opt-in sync).

Deliverables locked by this file:

    * ``deploy/postgres-ha/postgresql.primary.conf`` — primary config
      declaring ``wal_level=replica``, ``max_wal_senders>=2``,
      ``synchronous_commit`` + ``synchronous_standby_names`` knobs.
    * ``deploy/postgres-ha/postgresql.standby.conf`` — standby config
      with ``hot_standby=on``, ``hot_standby_feedback=on``, sane
      ``wal_receiver_timeout``.
    * ``deploy/postgres-ha/pg_hba.conf`` — ``scram-sha-256`` everywhere
      (no ``md5``, no remote ``trust``), with a ``replication`` row for
      the ``replicator`` role.
    * ``deploy/postgres-ha/docker-compose.yml`` — two services
      ``pg-primary`` + ``pg-standby`` on ``postgres:16-alpine``, bind
      mounts for the three config files, named volumes for durable data,
      standby depends on primary health.
    * ``deploy/postgres-ha/init-primary.sh`` — creates the
      ``replicator`` role with ``REPLICATION`` privilege and the
      ``omnisight_standby_slot`` physical replication slot.
    * ``deploy/postgres-ha/init-standby.sh`` — waits for primary,
      ``pg_basebackup --wal-method=stream --write-recovery-conf --slot=``,
      writes ``standby.signal`` and ``primary_conninfo``, execs postgres.
    * ``deploy/postgres-ha/.env.example`` — documents all env knobs and
      the synchronous-replication operator switch.
    * ``scripts/pg_ha_verify.py`` — programmatic verifier (75+ checks).

Sibling contracts:
    * test_alembic_pg_live_upgrade.py — G4 #1 live Postgres migration run
    * test_alembic_pg_compat.py       — G4 #1 SQLite→PG shim
    * test_db_url.py                  — G4 #2 DATABASE_URL abstraction
    * test_bluegreen_atomic_switch.py — G3 deploy primitive style
"""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = PROJECT_ROOT / "deploy" / "postgres-ha"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "pg_ha_verify.py"

PRIMARY_CONF = DEPLOY_DIR / "postgresql.primary.conf"
STANDBY_CONF = DEPLOY_DIR / "postgresql.standby.conf"
HBA_CONF = DEPLOY_DIR / "pg_hba.conf"
COMPOSE_YML = DEPLOY_DIR / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY_DIR / ".env.example"
INIT_PRIMARY = DEPLOY_DIR / "init-primary.sh"
INIT_STANDBY = DEPLOY_DIR / "init-standby.sh"

# Put scripts/ on the import path so we can exercise the verifier in-process.
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


# ---------------------------------------------------------------------------
# (1) Deploy-directory physical shape
# ---------------------------------------------------------------------------


class TestDeployDirectoryShape:
    def test_deploy_dir_exists(self) -> None:
        assert DEPLOY_DIR.is_dir(), (
            f"deploy/postgres-ha/ missing at {DEPLOY_DIR}. Row 1362 requires "
            "a self-contained compose bundle for the HA pair."
        )

    @pytest.mark.parametrize(
        "path",
        [PRIMARY_CONF, STANDBY_CONF, HBA_CONF, COMPOSE_YML,
         ENV_EXAMPLE, INIT_PRIMARY, INIT_STANDBY],
    )
    def test_artifact_file_exists(self, path: Path) -> None:
        assert path.is_file(), f"missing deploy artefact: {path}"

    def test_verify_script_exists(self) -> None:
        assert VERIFY_SCRIPT.is_file(), (
            f"scripts/pg_ha_verify.py missing at {VERIFY_SCRIPT}. "
            "Verifier is the programmatic contract check for this bundle."
        )

    def test_init_primary_is_executable(self) -> None:
        mode = INIT_PRIMARY.stat().st_mode
        assert mode & stat.S_IXUSR, (
            "init-primary.sh must be executable (chmod +x) so "
            "/docker-entrypoint-initdb.d runs it"
        )

    def test_init_standby_is_executable(self) -> None:
        mode = INIT_STANDBY.stat().st_mode
        assert mode & stat.S_IXUSR, (
            "init-standby.sh must be executable (chmod +x) so the "
            "compose entrypoint can invoke it"
        )

    def test_env_example_does_not_contain_real_secrets(self) -> None:
        # .env.example is committed — any plausible secret here is a leak.
        text = ENV_EXAMPLE.read_text()
        # Our template uses the literal sentinel `CHANGE_ME_STRONG...`.
        # Assert BOTH password fields hit the sentinel, not a real string.
        assert "CHANGE_ME_STRONG_PASSWORD" in text
        assert "CHANGE_ME_STRONG_REPL_PASSWORD" in text


# ---------------------------------------------------------------------------
# (2) postgresql.primary.conf contract
# ---------------------------------------------------------------------------


class TestPrimaryConf:
    @pytest.fixture(scope="class")
    def conf(self) -> dict[str, str]:
        from pg_ha_verify import parse_pg_conf
        return parse_pg_conf(PRIMARY_CONF.read_text())

    def test_listen_addresses_is_wildcard(self, conf) -> None:
        assert conf.get("listen_addresses") == "*"

    def test_wal_level_is_replica(self, conf) -> None:
        # `replica` is the minimum for streaming + hot standby. Accept
        # `logical` as a strict superset, but the default must be `replica`
        # to avoid gratuitous WAL bloat on boot-1 deploys.
        assert conf.get("wal_level") in {"replica", "logical"}

    def test_max_wal_senders_at_least_two(self, conf) -> None:
        v = conf.get("max_wal_senders")
        assert v is not None and v.isdigit() and int(v) >= 2, (
            f"max_wal_senders must be >=2 (got {v!r}); one for the stream "
            "plus headroom for pg_basebackup."
        )

    def test_max_replication_slots_at_least_one(self, conf) -> None:
        v = conf.get("max_replication_slots")
        assert v is not None and v.isdigit() and int(v) >= 1

    def test_hot_standby_is_on(self, conf) -> None:
        # Harmless on primary; enables promote-in-place without config swap.
        assert conf.get("hot_standby", "").lower() == "on"

    def test_synchronous_commit_declared(self, conf) -> None:
        assert "synchronous_commit" in conf, (
            "primary MUST declare synchronous_commit so the operator knob "
            "is discoverable; value may be on/off/local/remote_write/remote_apply"
        )

    def test_synchronous_commit_is_valid_value(self, conf) -> None:
        v = conf["synchronous_commit"].lower()
        assert v in {"on", "off", "local", "remote_write", "remote_apply"}

    def test_synchronous_standby_names_declared(self, conf) -> None:
        # Even if empty (async default), the knob must be IN the file so
        # operators find it without hunting; G4 #3 requirement is that
        # synchronous_commit is "可設" i.e. operator-settable — the
        # combined knob is (synchronous_commit ∧ synchronous_standby_names).
        assert "synchronous_standby_names" in conf

    def test_wal_keep_size_is_positive(self, conf) -> None:
        # Primary must keep some WAL for brief standby disconnects.
        v = conf.get("wal_keep_size", "")
        assert v and not v.startswith("0"), (
            f"wal_keep_size must be positive to survive standby flaps "
            f"(got {v!r})"
        )

    def test_archive_mode_defaults_off(self, conf) -> None:
        # Opt-in for PITR (G4 #5 runbook) — not mandatory for G4 #3.
        assert conf.get("archive_mode", "off").lower() == "off"

    def test_timezone_utc(self, conf) -> None:
        # Align with audit_log timestamps which are stored UTC.
        assert conf.get("timezone", "").lower() == "utc"

    def test_log_replication_commands_on(self, conf) -> None:
        # Crucial for debugging standby hand-off / resync.
        assert conf.get("log_replication_commands", "").lower() == "on"


# ---------------------------------------------------------------------------
# (3) postgresql.standby.conf contract
# ---------------------------------------------------------------------------


class TestStandbyConf:
    @pytest.fixture(scope="class")
    def conf(self) -> dict[str, str]:
        from pg_ha_verify import parse_pg_conf
        return parse_pg_conf(STANDBY_CONF.read_text())

    def test_listen_addresses_is_wildcard(self, conf) -> None:
        assert conf.get("listen_addresses") == "*"

    def test_hot_standby_is_on(self, conf) -> None:
        # Without this the standby refuses client connections.
        assert conf.get("hot_standby", "").lower() == "on"

    def test_hot_standby_feedback_is_on(self, conf) -> None:
        # Without feedback the primary can VACUUM away rows the standby
        # still needs for ongoing reads → ERROR 40001 on the standby.
        assert conf.get("hot_standby_feedback", "").lower() == "on"

    def test_wal_receiver_timeout_positive(self, conf) -> None:
        from pg_ha_verify import _parse_timespan_seconds
        secs = _parse_timespan_seconds(conf.get("wal_receiver_timeout", "0"))
        assert secs is not None and secs > 0

    def test_max_standby_streaming_delay_positive(self, conf) -> None:
        from pg_ha_verify import _parse_timespan_seconds
        secs = _parse_timespan_seconds(conf.get("max_standby_streaming_delay", "0"))
        assert secs is not None and secs > 0

    def test_wal_level_replica(self, conf) -> None:
        # Keep symmetric so the standby can be promoted and act as a
        # new sender to a cascading follower.
        assert conf.get("wal_level") in {"replica", "logical"}

    def test_timezone_utc(self, conf) -> None:
        assert conf.get("timezone", "").lower() == "utc"

    def test_no_primary_conninfo_in_static_conf(self, conf) -> None:
        # primary_conninfo MUST be written at runtime by init-standby.sh
        # into postgresql.auto.conf (with the live password). Hard-coding
        # it here would leak the password into the repo. We check the
        # PARSED conf (so comments that reference the knob by name don't
        # trip this test).
        assert "primary_conninfo" not in conf, (
            "primary_conninfo must NOT be set as a value in the static "
            "standby conf — it contains the replication password and is "
            "written at runtime by init-standby.sh into postgresql.auto.conf"
        )


# ---------------------------------------------------------------------------
# (4) pg_hba.conf contract
# ---------------------------------------------------------------------------


class TestHba:
    @pytest.fixture(scope="class")
    def rows(self) -> list:
        from pg_ha_verify import parse_hba
        return parse_hba(HBA_CONF.read_text())

    def test_replication_row_for_replicator_present(self, rows) -> None:
        repl = [r for r in rows if r.database == "replication" and r.user == "replicator"]
        assert len(repl) >= 1

    def test_replication_row_uses_scram(self, rows) -> None:
        repl = [r for r in rows if r.database == "replication" and r.user == "replicator"]
        assert all(r.method == "scram-sha-256" for r in repl)

    def test_no_md5_anywhere(self, rows) -> None:
        # md5 was deprecated in PG 14 and leaks password material on wire.
        md5 = [r for r in rows if r.method == "md5"]
        assert not md5, f"md5 rows are unsafe: {[r.raw for r in md5]}"

    def test_no_remote_trust(self, rows) -> None:
        bad = [r for r in rows if r.type != "local" and r.method == "trust"]
        assert not bad, f"remote trust rows are unsafe: {[r.raw for r in bad]}"

    def test_app_connection_row_present(self, rows) -> None:
        # The backend MUST be able to connect as `all` user to `all` db
        # (the Alembic migrations touch 29 tables + tenant schemas).
        app_rows = [r for r in rows if r.database == "all" and r.type in {"host", "hostssl"}]
        assert len(app_rows) >= 1

    def test_ipv6_replication_row_present(self, rows) -> None:
        # Dual-stack networks are common in k8s deploys (G5 HA-05);
        # we ship a symmetric IPv6 replication row to avoid surprises
        # when the pair is later replatformed.
        ipv6_repl = [
            r for r in rows
            if r.database == "replication" and "::" in r.address
        ]
        assert len(ipv6_repl) >= 1


# ---------------------------------------------------------------------------
# (5) docker-compose.yml contract
# ---------------------------------------------------------------------------


class TestCompose:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return COMPOSE_YML.read_text()

    def test_has_pg_primary_service(self, text) -> None:
        assert "pg-primary:" in text

    def test_has_pg_standby_service(self, text) -> None:
        assert "pg-standby:" in text

    def test_uses_postgres_16_alpine(self, text) -> None:
        # Matches G4 #1 test_alembic_pg_live_upgrade.py image choice.
        assert "postgres:16-alpine" in text

    def test_primary_mounts_primary_conf(self, text) -> None:
        assert "./postgresql.primary.conf:/etc/postgresql/postgresql.conf" in text

    def test_standby_mounts_standby_conf(self, text) -> None:
        assert "./postgresql.standby.conf:/etc/postgresql/postgresql.conf" in text

    def test_both_mount_pg_hba(self, text) -> None:
        # Same pg_hba on both containers keeps policy symmetric.
        # The compose file has this entry twice (once per service).
        assert text.count("./pg_hba.conf:/etc/postgresql/pg_hba.conf") >= 2

    def test_primary_mounts_init_script(self, text) -> None:
        # Primary uses docker-entrypoint-initdb.d (first-boot-only path).
        assert "./init-primary.sh:/docker-entrypoint-initdb.d/" in text

    def test_standby_overrides_entrypoint_with_init_script(self, text) -> None:
        # Standby CANNOT use initdb.d (data dir is seeded from pg_basebackup,
        # not initdb), so the init-standby.sh script runs as the entrypoint.
        assert "init-standby.sh" in text
        assert "entrypoint:" in text

    def test_standby_depends_on_primary_health(self, text) -> None:
        assert "depends_on:" in text
        assert "pg-primary:" in text  # dependency target
        assert "condition: service_healthy" in text

    def test_named_volumes_present(self, text) -> None:
        assert "omnisight-pg-primary:" in text
        assert "omnisight-pg-standby:" in text

    def test_replication_slot_env_knob_wired(self, text) -> None:
        assert "REPLICATION_SLOT_NAME" in text

    def test_replication_application_name_env_wired(self, text) -> None:
        # Key to flipping sync replication — primary matches this against
        # synchronous_standby_names.
        assert "REPLICATION_APPLICATION_NAME" in text

    def test_both_healthchecks_use_pg_isready(self, text) -> None:
        # Two services → expect `pg_isready` to appear at least twice.
        assert text.count("pg_isready") >= 2

    def test_standby_host_port_distinct_from_primary(self, text) -> None:
        # The compose file exposes primary on 5432 and standby on 5433
        # by default, avoiding host-port collisions for operator psql.
        assert "PG_PRIMARY_HOST_PORT:-5432" in text
        assert "PG_STANDBY_HOST_PORT:-5433" in text

    def test_passwords_sourced_from_env(self, text) -> None:
        # No hard-coded passwords in compose. The `:?` error-if-unset form
        # gives operators an immediate failure when .env is missing.
        assert "POSTGRES_PASSWORD:?" in text
        assert "REPLICATION_PASSWORD:?" in text

    def test_primary_runs_postgres_with_config_file(self, text) -> None:
        # Bypasses the image default config in favor of our mounted file.
        assert 'config_file=/etc/postgresql/postgresql.conf' in text


# ---------------------------------------------------------------------------
# (6) init-primary.sh contract
# ---------------------------------------------------------------------------


class TestInitPrimary:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return INIT_PRIMARY.read_text()

    def test_shebang(self, text) -> None:
        assert text.startswith("#!/usr/bin/env bash"), (
            "init-primary.sh must start with #!/usr/bin/env bash for portability"
        )

    def test_strict_mode(self, text) -> None:
        assert "set -euo pipefail" in text

    def test_creates_replicator_role(self, text) -> None:
        assert "CREATE ROLE" in text
        assert "REPLICATION" in text

    def test_replication_role_is_idempotent(self, text) -> None:
        # Must guard the CREATE ROLE with a pg_roles existence check so a
        # container restart with a rewound PGDATA doesn't explode.
        assert "pg_roles" in text

    def test_creates_physical_replication_slot(self, text) -> None:
        assert "pg_create_physical_replication_slot" in text

    def test_slot_creation_is_idempotent(self, text) -> None:
        assert "pg_replication_slots" in text

    def test_slot_name_is_canonical(self, text) -> None:
        # Must match init-standby.sh REPLICATION_SLOT_NAME default.
        assert "omnisight_standby_slot" in text

    def test_uses_on_error_stop(self, text) -> None:
        # Without this a bad SQL statement would be silently skipped and
        # the role/slot would never get created.
        assert "-v ON_ERROR_STOP=1" in text or "ON_ERROR_STOP=1" in text

    def test_password_is_not_echoed_raw(self, text) -> None:
        # Only a sha256 fingerprint may be logged — never the raw password.
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("echo") and "REPLICATION_PASSWORD" in s:
                assert "sha256" in s, (
                    f"raw password echo detected: {s!r}. Only SHA-256 "
                    "fingerprints may be logged."
                )

    def test_fails_closed_when_password_missing(self, text) -> None:
        # `: "${REPLICATION_PASSWORD:?...}"` makes the script exit if the
        # env var is unset — we want a fail-closed guard, not a silent
        # default that could create a role with no password.
        assert 'REPLICATION_PASSWORD:?' in text


# ---------------------------------------------------------------------------
# (7) init-standby.sh contract
# ---------------------------------------------------------------------------


class TestInitStandby:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return INIT_STANDBY.read_text()

    def test_shebang(self, text) -> None:
        assert text.startswith("#!/usr/bin/env bash")

    def test_strict_mode(self, text) -> None:
        assert "set -euo pipefail" in text

    def test_waits_for_primary(self, text) -> None:
        assert "pg_isready" in text

    def test_runs_pg_basebackup(self, text) -> None:
        assert "pg_basebackup" in text

    def test_uses_wal_stream(self, text) -> None:
        assert "--wal-method=stream" in text

    def test_writes_recovery_conf(self, text) -> None:
        # `-R` creates the initial standby.signal + primary_conninfo.
        assert "--write-recovery-conf" in text

    def test_uses_named_slot(self, text) -> None:
        # Required so primary retains WAL through the clone window.
        assert "--slot=" in text

    def test_creates_standby_signal(self, text) -> None:
        # Even after basebackup, the script must idempotently touch
        # standby.signal so a manual PGDATA edit doesn't orphan the file.
        assert "standby.signal" in text

    def test_writes_primary_conninfo_with_application_name(self, text) -> None:
        # application_name is the key for synchronous_standby_names match
        # on the primary — without it sync replication cannot be turned on.
        assert "primary_conninfo" in text
        assert "application_name=" in text

    def test_sets_permissions_on_auto_conf(self, text) -> None:
        # postgresql.auto.conf contains the replication password in
        # plaintext; mode 0600 is mandatory.
        assert "chmod 0600" in text

    def test_execs_postgres_directly(self, text) -> None:
        # `exec` lets SIGTERM from docker stop land on postgres directly.
        assert "exec postgres" in text

    def test_passes_config_file(self, text) -> None:
        assert "config_file=" in text
        assert "hba_file=" in text

    def test_empties_pgdata_before_basebackup(self, text) -> None:
        # A partial/aborted first-boot clone could leave garbage in PGDATA.
        # pg_basebackup REQUIRES an empty target directory or it aborts.
        assert "rm -rf" in text

    def test_wait_timeout_is_configurable(self, text) -> None:
        # Slow disks / cold-start CI can take >30 s before primary is
        # ready. The timeout must be an env knob so operators can extend.
        assert "STANDBY_BASEBACKUP_TIMEOUT" in text

    def test_fails_closed_on_missing_password(self, text) -> None:
        assert 'REPLICATION_PASSWORD:?' in text


# ---------------------------------------------------------------------------
# (8) .env.example contract
# ---------------------------------------------------------------------------


class TestEnvExample:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return ENV_EXAMPLE.read_text()

    @pytest.mark.parametrize(
        "key",
        [
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "REPLICATION_USER",
            "REPLICATION_PASSWORD",
            "PRIMARY_HOST",
            "PRIMARY_PORT",
            "REPLICATION_SLOT_NAME",
            "REPLICATION_APPLICATION_NAME",
            "PG_PRIMARY_HOST_PORT",
            "PG_STANDBY_HOST_PORT",
            "STANDBY_BASEBACKUP_TIMEOUT",
        ],
    )
    def test_declares_knob(self, text, key) -> None:
        import re
        assert re.search(rf"^{re.escape(key)}=", text, re.MULTILINE), (
            f".env.example must declare {key}=... on its own line"
        )

    def test_documents_synchronous_standby_knob(self, text) -> None:
        # Operators should find the sync-rep flip-switch in .env.example
        # without having to read the postgresql.primary.conf source.
        assert "synchronous_standby_names" in text

    def test_does_not_contain_real_password(self, text) -> None:
        # Template uses the obvious sentinel; no real secret may leak here.
        assert "CHANGE_ME_STRONG_PASSWORD" in text
        assert "CHANGE_ME_STRONG_REPL_PASSWORD" in text
        # Don't let someone accidentally commit a real credential
        # masquerading as a placeholder. Any 16+ char-long hex/base64-ish
        # string that isn't the sentinel is suspicious.
        for line in text.splitlines():
            if line.startswith("POSTGRES_PASSWORD=") or line.startswith("REPLICATION_PASSWORD="):
                _, _, val = line.partition("=")
                assert "CHANGE_ME" in val, (
                    f"password line must still be a sentinel: {line!r}"
                )


# ---------------------------------------------------------------------------
# (9) Parser unit tests — parse_pg_conf / parse_hba
# ---------------------------------------------------------------------------


class TestParsePgConf:
    def test_simple_key_value(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("foo = bar") == {"foo": "bar"}

    def test_case_insensitive_keys(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("WAL_LEVEL = replica") == {"wal_level": "replica"}

    def test_quoted_string_values_stripped(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("log_line_prefix = '%t [%p] '") == {
            "log_line_prefix": "%t [%p] "
        }

    def test_empty_quoted_string_preserved(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("synchronous_standby_names = ''") == {
            "synchronous_standby_names": "",
        }

    def test_comment_lines_ignored(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("# a comment\nfoo = bar\n# another") == {"foo": "bar"}

    def test_trailing_inline_comment_stripped(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("port = 5432 # default") == {"port": "5432"}

    def test_blank_lines_ignored(self) -> None:
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("\n\nfoo = bar\n\n") == {"foo": "bar"}

    def test_last_wins_on_duplicate_key(self) -> None:
        # Postgres resolves duplicate keys with last-wins; we mirror that.
        from pg_ha_verify import parse_pg_conf
        assert parse_pg_conf("foo = a\nfoo = b") == {"foo": "b"}


class TestParseHba:
    def test_local_row_shape(self) -> None:
        from pg_ha_verify import parse_hba
        rows = parse_hba("local all all trust")
        assert len(rows) == 1
        assert rows[0].type == "local"
        assert rows[0].database == "all"
        assert rows[0].user == "all"
        assert rows[0].address == ""
        assert rows[0].method == "trust"

    def test_host_row_shape(self) -> None:
        from pg_ha_verify import parse_hba
        rows = parse_hba("host replication replicator 10.0.0.0/8 scram-sha-256")
        assert len(rows) == 1
        assert rows[0].type == "host"
        assert rows[0].database == "replication"
        assert rows[0].user == "replicator"
        assert rows[0].address == "10.0.0.0/8"
        assert rows[0].method == "scram-sha-256"

    def test_comments_and_blanks_ignored(self) -> None:
        from pg_ha_verify import parse_hba
        rows = parse_hba("# header\n\nlocal all all trust\n# trailer\n")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# (10) End-to-end: `python3 scripts/pg_ha_verify.py` green in-tree
# ---------------------------------------------------------------------------


class TestVerifierEndToEnd:
    def test_verifier_runs_clean_on_committed_state(self) -> None:
        # The verifier MUST exit 0 on a fresh checkout so CI catches
        # drift introduced by future edits to the HA bundle.
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 0, (
            f"scripts/pg_ha_verify.py exited {proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )
        # Human-friendly output must end with OK.
        assert "Result:  OK" in proc.stdout

    def test_verifier_json_mode_is_well_formed(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        import json
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert isinstance(payload["checks"], list)
        assert len(payload["checks"]) >= 60, (
            "verifier should exercise >=60 contract points; got "
            f"{len(payload['checks'])}"
        )
        assert all(c["ok"] is True for c in payload["checks"])

    def test_verifier_detects_missing_wal_level(self, tmp_path: Path) -> None:
        # Copy the deploy bundle to a tmp dir and corrupt the primary
        # conf — the verifier must flag it.
        import shutil
        work = tmp_path / "postgres-ha"
        shutil.copytree(DEPLOY_DIR, work)
        primary = work / "postgresql.primary.conf"
        text = primary.read_text()
        # Break wal_level by removing it entirely.
        import re
        broken = re.sub(r"^wal_level\s*=.*$", "# wal_level removed for test",
                         text, flags=re.MULTILINE)
        primary.write_text(broken)

        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--deploy-dir", str(work)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 1, (
            "verifier must exit 1 when wal_level is missing; got "
            f"{proc.returncode}\nSTDOUT:\n{proc.stdout}"
        )
        assert "primary.wal_level" in proc.stdout

    def test_verifier_detects_md5_in_hba(self, tmp_path: Path) -> None:
        import shutil
        work = tmp_path / "postgres-ha"
        shutil.copytree(DEPLOY_DIR, work)
        hba = work / "pg_hba.conf"
        hba.write_text(hba.read_text() + "\nhost all all 0.0.0.0/0 md5\n")

        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--deploy-dir", str(work)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 1
        assert "hba.no_md5" in proc.stdout

    def test_verifier_detects_remote_trust(self, tmp_path: Path) -> None:
        import shutil
        work = tmp_path / "postgres-ha"
        shutil.copytree(DEPLOY_DIR, work)
        hba = work / "pg_hba.conf"
        hba.write_text(hba.read_text() + "\nhost all all 10.0.0.0/8 trust\n")

        proc = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), "--deploy-dir", str(work)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 1
        assert "hba.no_remote_trust" in proc.stdout


# ---------------------------------------------------------------------------
# (11) Structural invariants — slot / application name / user symmetry
# ---------------------------------------------------------------------------


class TestSymmetry:
    """Compose env + init scripts + .env.example must agree on slot name
    and application_name — a mismatch means replication silently misroutes.
    """

    def test_slot_name_symmetric_across_artefacts(self) -> None:
        # Same slot name must appear in init-primary.sh, init-standby.sh
        # (or at least be the env default), and in docker-compose.yml /
        # .env.example as the default for REPLICATION_SLOT_NAME.
        slot = "omnisight_standby_slot"
        assert slot in INIT_PRIMARY.read_text()
        assert slot in INIT_STANDBY.read_text()
        assert slot in COMPOSE_YML.read_text()
        assert slot in ENV_EXAMPLE.read_text()

    def test_application_name_symmetric(self) -> None:
        app = "omnisight_standby"
        assert app in INIT_STANDBY.read_text()
        assert app in COMPOSE_YML.read_text()
        assert app in ENV_EXAMPLE.read_text()
        # Primary conf must document the canonical name too (even if
        # synchronous_standby_names is empty by default, the COMMENT must
        # teach the operator what name to use).
        assert app in PRIMARY_CONF.read_text()

    def test_replicator_user_consistent(self) -> None:
        user = "replicator"
        assert user in INIT_PRIMARY.read_text()
        assert user in HBA_CONF.read_text()
        assert f"REPLICATION_USER={user}" in ENV_EXAMPLE.read_text()

    def test_postgres_image_version_pinned(self) -> None:
        # Compose pins postgres:16-alpine matching G4 #1 live-upgrade
        # test image. Drift would mean HA tests and migration tests run
        # on different versions.
        assert "postgres:16-alpine" in COMPOSE_YML.read_text()

    def test_sync_commit_operator_documentation_chain(self) -> None:
        # The G4 #3 requirement "synchronous_commit=on 可設" means the
        # operator knob must be discoverable via THREE entry points:
        #   1. The primary conf declares synchronous_commit explicitly.
        #   2. The primary conf declares synchronous_standby_names (the
        #      lever that turns sync ON alongside synchronous_commit).
        #   3. The .env.example documents how to flip sync replication on.
        primary_text = PRIMARY_CONF.read_text()
        env_text = ENV_EXAMPLE.read_text()
        assert "synchronous_commit" in primary_text
        assert "synchronous_standby_names" in primary_text
        assert "synchronous_standby_names" in env_text
