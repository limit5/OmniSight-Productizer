"""G4 #3 — Static verifier for the PostgreSQL HA deploy bundle.

Parses the config files under ``deploy/postgres-ha/`` and asserts the
contract that primary + hot standby streaming replication depends on:

    * postgresql.primary.conf declares ``wal_level = replica``,
      ``max_wal_senders`` >= 2, ``hot_standby = on``, the configurable
      ``synchronous_commit`` knob, and the operator-facing
      ``synchronous_standby_names`` knob (default empty = async).
    * postgresql.standby.conf declares ``hot_standby = on``,
      ``hot_standby_feedback = on``, and a sensible
      ``wal_receiver_timeout``.
    * pg_hba.conf allows the ``replicator`` role to open replication
      connections and uses ``scram-sha-256`` (never ``md5`` or
      ``trust``) for non-local rows.
    * docker-compose.yml wires the two services, bind-mounts the three
      config files, names the replication slot + application name
      symmetrically, and exposes the expected host ports.
    * init-primary.sh creates the replicator role + the physical slot.
    * init-standby.sh waits for the primary, runs ``pg_basebackup``,
      writes ``standby.signal``, and execs postgres with the correct
      config file.

Used by ``backend/tests/test_pg_ha_deployment.py`` as a programmatic
contract check, and runnable on the command line as:

    python3 scripts/pg_ha_verify.py           # exit 0 on green, 1 on drift
    python3 scripts/pg_ha_verify.py --json    # machine-readable report

Pure stdlib — no psycopg2, no docker client. The verifier lives next to
the other ``scripts/*_verify`` / ``scripts/scan_*`` tools (G4 #1 pattern).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "postgres-ha"

PRIMARY_CONF = DEPLOY_DIR / "postgresql.primary.conf"
STANDBY_CONF = DEPLOY_DIR / "postgresql.standby.conf"
HBA_CONF = DEPLOY_DIR / "pg_hba.conf"
COMPOSE_YML = DEPLOY_DIR / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY_DIR / ".env.example"
INIT_PRIMARY = DEPLOY_DIR / "init-primary.sh"
INIT_STANDBY = DEPLOY_DIR / "init-standby.sh"

# ---------------------------------------------------------------------------
# Config-file parsing (Postgres key-value format)
# ---------------------------------------------------------------------------

# A postgresql.conf line is either:
#   key = value         (value may be quoted, unquoted, or have a trailing comment)
#   # comment
#   <blank>
# The LHS is case-insensitive in postgres; we lowercase on parse.
_PG_CONF_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<key>[A-Za-z_][A-Za-z0-9_]*)
    \s*=\s*
    (?P<value>
        '(?:[^']|'')*'              # single-quoted string (possibly empty)
        | "(?:[^"]|"")*"            # double-quoted string
        | [^#\s]+                   # bare token
    )
    \s*
    (?:\#.*)?$                      # optional trailing comment
    """,
    re.VERBOSE,
)


def parse_pg_conf(text: str) -> dict[str, str]:
    """Parse a postgresql.conf style text into a dict.

    Quoted values are returned WITHOUT surrounding quotes so callers can
    compare against literal expected values. Comments and blank lines
    are skipped. Last-write-wins if a key is declared twice (mirrors
    Postgres' own behaviour).
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PG_CONF_LINE_RE.match(raw)
        if not m:
            continue
        key = m.group("key").lower()
        val = m.group("value")
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1].replace("''", "'")
        elif val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        out[key] = val
    return out


# ---------------------------------------------------------------------------
# pg_hba.conf parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HbaRow:
    """A single non-comment row of pg_hba.conf."""

    type: str            # local | host | hostssl | ...
    database: str        # all | replication | specific db
    user: str            # all | specific role
    address: str         # "" (local), CIDR, or hostname
    method: str          # trust | scram-sha-256 | md5 | ...
    raw: str


def parse_hba(text: str) -> list[HbaRow]:
    rows: list[HbaRow] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "local":
            # local  DATABASE  USER  METHOD [options...]
            if len(parts) < 4:
                continue
            rows.append(HbaRow("local", parts[1], parts[2], "", parts[3], line))
        else:
            # host(ssl|nossl|gssenc)  DATABASE  USER  ADDRESS  METHOD [options...]
            if len(parts) < 5:
                continue
            rows.append(HbaRow(parts[0], parts[1], parts[2], parts[3], parts[4], line))
    return rows


# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    ok: bool
    checks: list[dict] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        if not passed:
            self.ok = False
        self.checks.append({"name": name, "ok": passed, "detail": detail})


# ---------------------------------------------------------------------------
# Per-artifact verifiers
# ---------------------------------------------------------------------------


def verify_primary_conf(text: str, result: CheckResult) -> None:
    conf = parse_pg_conf(text)

    def require(key: str, predicate, detail_fmt: str) -> None:
        raw = conf.get(key)
        passed = raw is not None and predicate(raw)
        result.record(
            f"primary.{key}",
            passed,
            "" if passed else detail_fmt.format(actual=raw),
        )

    require(
        "listen_addresses",
        lambda v: v == "*" or "0.0.0.0" in v,
        "must accept remote replica connections (got {actual!r})",
    )
    require(
        "wal_level",
        lambda v: v.lower() == "replica" or v.lower() == "logical",
        "must be 'replica' or 'logical' for streaming replication (got {actual!r})",
    )
    require(
        "max_wal_senders",
        lambda v: v.isdigit() and int(v) >= 2,
        "must be >=2 to allow concurrent basebackup + stream (got {actual!r})",
    )
    require(
        "max_replication_slots",
        lambda v: v.isdigit() and int(v) >= 1,
        "must be >=1 to hold omnisight_standby_slot (got {actual!r})",
    )
    require(
        "hot_standby",
        lambda v: v.lower() == "on",
        "symmetric config requires hot_standby=on on primary too (got {actual!r})",
    )
    require(
        "synchronous_commit",
        lambda v: v.lower() in {"on", "off", "local", "remote_write", "remote_apply"},
        "synchronous_commit must be one of the documented values (got {actual!r})",
    )
    # synchronous_standby_names MUST be present as an explicit knob so
    # operator knows how to flip sync replication on. Default empty is fine.
    result.record(
        "primary.synchronous_standby_names.declared",
        "synchronous_standby_names" in conf,
        "primary config must declare synchronous_standby_names (even if empty) "
        "so operators can flip sync replication on without hunting for the knob",
    )


def verify_standby_conf(text: str, result: CheckResult) -> None:
    conf = parse_pg_conf(text)

    def require(key: str, predicate, detail_fmt: str) -> None:
        raw = conf.get(key)
        passed = raw is not None and predicate(raw)
        result.record(
            f"standby.{key}",
            passed,
            "" if passed else detail_fmt.format(actual=raw),
        )

    require(
        "listen_addresses",
        lambda v: v == "*" or "0.0.0.0" in v,
        "standby must accept app read-only connections (got {actual!r})",
    )
    require(
        "hot_standby",
        lambda v: v.lower() == "on",
        "hot_standby must be 'on' so standby accepts reads (got {actual!r})",
    )
    require(
        "hot_standby_feedback",
        lambda v: v.lower() == "on",
        "hot_standby_feedback=on keeps VACUUM horizon consistent (got {actual!r})",
    )
    require(
        "wal_receiver_timeout",
        lambda v: _parse_timespan_seconds(v) is not None and _parse_timespan_seconds(v) > 0,
        "wal_receiver_timeout must be a positive duration (got {actual!r})",
    )


def verify_hba(text: str, result: CheckResult) -> None:
    rows = parse_hba(text)

    # 1. At least one row authorises the replicator role for replication.
    repl_rows = [r for r in rows if r.database == "replication" and r.user == "replicator"]
    result.record(
        "hba.replication_row_present",
        len(repl_rows) >= 1,
        "pg_hba.conf must have a row authorising 'replicator' for the "
        "'replication' pseudo-database",
    )

    # 2. Replication rows must use scram-sha-256 (md5 and trust are
    #    disallowed for anything remote).
    repl_secure = all(r.method == "scram-sha-256" for r in repl_rows)
    result.record(
        "hba.replication_uses_scram",
        repl_secure and len(repl_rows) >= 1,
        "replication rows must use scram-sha-256 (md5 and trust leak "
        "credentials; md5 was deprecated in PG 14)",
    )

    # 3. No non-local `trust` rows (local socket trust is acceptable).
    remote_trust = [
        r for r in rows
        if r.type != "local" and r.method == "trust"
    ]
    result.record(
        "hba.no_remote_trust",
        not remote_trust,
        f"remote 'trust' rows are unsafe: {[r.raw for r in remote_trust]}",
    )

    # 4. No md5 rows at all (deprecated since PG 14).
    md5_rows = [r for r in rows if r.method == "md5"]
    result.record(
        "hba.no_md5",
        not md5_rows,
        f"md5 auth is deprecated, use scram-sha-256 instead: {[r.raw for r in md5_rows]}",
    )

    # 5. At least one app-level row (non-replication) for the app to connect.
    app_rows = [r for r in rows if r.database == "all" and r.type in {"host", "hostssl"}]
    result.record(
        "hba.app_row_present",
        len(app_rows) >= 1,
        "pg_hba.conf must have at least one 'host all ...' row so the "
        "OmniSight backend can connect",
    )


def verify_compose(text: str, result: CheckResult) -> None:
    # Light-touch structural checks — we don't depend on pyyaml.
    required_tokens = {
        "compose.has_pg_primary_service": "pg-primary:",
        "compose.has_pg_standby_service": "pg-standby:",
        "compose.uses_postgres_16": "postgres:16-alpine",
        "compose.mounts_primary_conf": "./postgresql.primary.conf:/etc/postgresql/postgresql.conf",
        "compose.mounts_standby_conf": "./postgresql.standby.conf:/etc/postgresql/postgresql.conf",
        "compose.mounts_hba": "./pg_hba.conf:/etc/postgresql/pg_hba.conf",
        "compose.mounts_init_primary": "./init-primary.sh:/docker-entrypoint-initdb.d/",
        "compose.mounts_init_standby": "./init-standby.sh:/usr/local/bin/init-standby.sh",
        "compose.standby_entrypoint_runs_init": "init-standby.sh",
        "compose.standby_depends_on_primary": "pg-primary:",
        "compose.volume_primary_named": "omnisight-pg-primary",
        "compose.volume_standby_named": "omnisight-pg-standby",
        "compose.uses_replication_slot_env": "REPLICATION_SLOT_NAME",
        "compose.uses_replication_app_name_env": "REPLICATION_APPLICATION_NAME",
        "compose.primary_healthcheck": "pg_isready",
        "compose.standby_healthcheck": "pg_isready",
        "compose.primary_host_port_knob": "PG_PRIMARY_HOST_PORT",
        "compose.standby_host_port_knob": "PG_STANDBY_HOST_PORT",
    }
    for name, token in required_tokens.items():
        result.record(name, token in text, f"missing token in docker-compose.yml: {token!r}")

    # service_healthy dependency must be present so standby doesn't try
    # pg_basebackup before the primary is up.
    result.record(
        "compose.standby_waits_for_primary_healthy",
        "condition: service_healthy" in text,
        "standby must wait for pg-primary's health check (condition: service_healthy)",
    )


def verify_env_example(text: str, result: CheckResult) -> None:
    required_keys = [
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
    ]
    for key in required_keys:
        result.record(
            f"env.{key}",
            re.search(rf"^{re.escape(key)}=", text, re.MULTILINE) is not None,
            f".env.example must declare {key}=... so operators know the knob exists",
        )

    # The synchronous-replication operator hint must be present so a
    # freshly-deployed operator finds the durability knob on day one.
    result.record(
        "env.documents_synchronous_standby_knob",
        "synchronous_standby_names" in text,
        ".env.example must mention synchronous_standby_names so operators "
        "know how to flip sync replication on",
    )


def verify_init_primary(text: str, result: CheckResult) -> None:
    must_contain = {
        "init_primary.uses_strict_mode": "set -euo pipefail",
        "init_primary.creates_replicator_role": "CREATE ROLE",
        "init_primary.grants_replication_priv": "REPLICATION",
        "init_primary.creates_physical_slot": "pg_create_physical_replication_slot",
        "init_primary.slot_name_matches_env": "omnisight_standby_slot",
        "init_primary.guards_existing_role": "pg_roles",
        "init_primary.guards_existing_slot": "pg_replication_slots",
        "init_primary.uses_scram_via_psql": "psql",
    }
    for name, token in must_contain.items():
        result.record(name, token in text, f"init-primary.sh missing: {token!r}")

    # Password MUST NOT be echoed directly to stdout. We scan each
    # `echo` line and flag any that mention REPLICATION_PASSWORD without
    # first passing through sha256sum / openssl / cut (fingerprint use
    # is fine; raw echo is not).
    bad_echo_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("echo"):
            continue
        if "REPLICATION_PASSWORD" in stripped and "sha256" not in stripped:
            bad_echo_lines.append(stripped)
    result.record(
        "init_primary.does_not_echo_password",
        not bad_echo_lines,
        f"init-primary.sh must not echo REPLICATION_PASSWORD directly: {bad_echo_lines}",
    )


def verify_init_standby(text: str, result: CheckResult) -> None:
    must_contain = {
        "init_standby.uses_strict_mode": "set -euo pipefail",
        "init_standby.waits_for_primary": "pg_isready",
        "init_standby.runs_pg_basebackup": "pg_basebackup",
        "init_standby.wal_method_stream": "--wal-method=stream",
        "init_standby.writes_recovery_conf": "--write-recovery-conf",
        "init_standby.uses_named_slot": "--slot=",
        "init_standby.touches_standby_signal": "standby.signal",
        "init_standby.writes_primary_conninfo": "primary_conninfo",
        "init_standby.sets_application_name": "application_name=",
        "init_standby.execs_postgres": "exec postgres",
        "init_standby.passes_config_file": "config_file=",
    }
    for name, token in must_contain.items():
        result.record(name, token in text, f"init-standby.sh missing: {token!r}")


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _parse_timespan_seconds(value: str) -> float | None:
    """Parse a postgres timespan literal (`60s`, `5min`, `1h`, bare int ms)."""
    v = value.strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(ms|s|min|h|d)?$", v)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "ms"
    if unit == "ms":
        return n / 1000.0
    if unit == "s":
        return n
    if unit == "min":
        return n * 60.0
    if unit == "h":
        return n * 3600.0
    if unit == "d":
        return n * 86400.0
    return None


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def run_all_checks(deploy_dir: Path = DEPLOY_DIR) -> CheckResult:
    result = CheckResult(ok=True)

    paths = {
        "primary_conf": deploy_dir / "postgresql.primary.conf",
        "standby_conf": deploy_dir / "postgresql.standby.conf",
        "hba": deploy_dir / "pg_hba.conf",
        "compose": deploy_dir / "docker-compose.yml",
        "env_example": deploy_dir / ".env.example",
        "init_primary": deploy_dir / "init-primary.sh",
        "init_standby": deploy_dir / "init-standby.sh",
    }
    for name, p in paths.items():
        result.record(f"file.{name}.exists", p.is_file(), f"expected at {p}")

    if not result.ok:
        return result

    verify_primary_conf(paths["primary_conf"].read_text(), result)
    verify_standby_conf(paths["standby_conf"].read_text(), result)
    verify_hba(paths["hba"].read_text(), result)
    verify_compose(paths["compose"].read_text(), result)
    verify_env_example(paths["env_example"].read_text(), result)
    verify_init_primary(paths["init_primary"].read_text(), result)
    verify_init_standby(paths["init_standby"].read_text(), result)

    return result


def _format_human(result: CheckResult) -> str:
    lines = []
    for check in result.checks:
        mark = "OK  " if check["ok"] else "FAIL"
        lines.append(f"  [{mark}] {check['name']}")
        if not check["ok"] and check["detail"]:
            lines.append(f"        → {check['detail']}")
    lines.append("")
    lines.append(f"Summary: {sum(1 for c in result.checks if c['ok'])}/{len(result.checks)} checks passed")
    lines.append("Result:  " + ("OK" if result.ok else "FAIL"))
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--deploy-dir",
        default=str(DEPLOY_DIR),
        help="path to deploy/postgres-ha/ (default: repo-relative)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = run_all_checks(Path(args.deploy_dir))
    if args.json:
        print(json.dumps({"ok": result.ok, "checks": result.checks}, indent=2))
    else:
        print(_format_human(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
