r"""[OP-972] AUDIT-19b — Postgres anonymized snapshot pipeline (mechanism 6c).

Pins the five artifacts that ship the daily prod -> staging anonymized
snapshot restore, plus the *behaviour* of the two scripts that are cheap to
exercise without a real Postgres / docker:

  * infra/staging/anonymize-fields.yaml — the ONE declarative PII spec read
    by both Phase 1 (anonymize.sh) and Phase 2 (Greenmask). We assert its
    shape, that every Phase-1 `strategy` has a Greenmask translation, and
    that the AnonymizeMissedField pattern list is present + compilable.
  * infra/staging/anonymize.sh — Phase 1 SQL anonymizer. The behaviour
    tests actually run it: on a covered dump it must emit a `BEGIN; UPDATE
    ...; COMMIT;` epilogue with the right per-strategy SQL; on a dump with
    an *uncovered* PII-shaped column it must FAIL LOUD (exit 3,
    AnonymizeMissedField) and write nothing; a YAML table absent from the
    dump must be skipped gracefully.
  * infra/staging/snapshot-restore.sh — the daily orchestrator. We syntax-
    check it, run its prereq-failure path (exit 5, no side effects, audit
    row skipped when no DSN), and pin the pipeline contract in its text:
    pg_dump prod, call anonymize.sh, schema-drift sanity (SchemaDrift),
    rollback-safe restore (drop-and-rename / revert), `release_audit` row.
  * deploy/systemd/staging-pg-snapshot.{service,timer} — daily 02:00,
    Persistent=true, oneshot, ExecStart -> snapshot-restore.sh, optional
    EnvironmentFile for the release-audit DSN + staging knobs.

Cost: a handful of bash + python subprocesses, pytest + PyYAML (a hard repo
dep) only. No docker / systemd / Postgres required.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra" / "staging"
SYSTEMD = REPO_ROOT / "deploy" / "systemd"

FIELDS_YAML = INFRA / "anonymize-fields.yaml"
ANONYMIZE_SH = INFRA / "anonymize.sh"
RESTORE_SH = INFRA / "snapshot-restore.sh"
SERVICE_UNIT = SYSTEMD / "staging-pg-snapshot.service"
TIMER_UNIT = SYSTEMD / "staging-pg-snapshot.timer"

PHASE1_STRATEGIES = {"email_hash", "test_user", "null", "redact", "zero"}


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────


def _spec() -> dict:
    return yaml.safe_load(FIELDS_YAML.read_text(encoding="utf-8"))


def _run_anonymize(tmp_path: Path, dump_sql: str, *, fields_yaml: Path | None = None):
    in_sql = tmp_path / "in.sql"
    out_sql = tmp_path / "out.sql"
    in_sql.write_text(dump_sql)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if fields_yaml is not None:
        env["ANONYMIZE_FIELDS_YAML"] = str(fields_yaml)
    proc = subprocess.run(
        ["bash", str(ANONYMIZE_SH), str(in_sql), str(out_sql)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc, out_sql


# A prod-dump fixture whose PII-shaped columns are exactly the ones the
# shipped anonymize-fields.yaml covers (so the AnonymizeMissedField guard
# passes) — column-masked tables + the truncate:true ones.
COVERED_DUMP = textwrap.dedent(
    """\
    --
    -- PostgreSQL database dump
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
    CREATE TABLE public.tenants (
        id text NOT NULL,
        name text NOT NULL,
        plan text DEFAULT 'free'::text NOT NULL
    );
    CREATE TABLE public.tenant_invites (
        id text NOT NULL,
        tenant_id text NOT NULL,
        email text NOT NULL,
        token_hash text NOT NULL,
        role text DEFAULT 'member'::text NOT NULL
    );
    CREATE TABLE public.git_accounts (
        id text NOT NULL,
        platform text NOT NULL,
        username text DEFAULT ''::text NOT NULL,
        encrypted_token text DEFAULT ''::text NOT NULL,
        encrypted_ssh_key text DEFAULT ''::text NOT NULL,
        encrypted_webhook_secret text DEFAULT ''::text NOT NULL,
        code_verifier text DEFAULT '{}'::text NOT NULL
    );
    CREATE TABLE public.api_keys (
        id text NOT NULL,
        name text NOT NULL,
        key_hash text NOT NULL,
        key_lookup_index text,
        key_prefix text DEFAULT ''::text NOT NULL
    );
    CREATE TABLE public.sessions (
        token text NOT NULL,
        token_lookup_index text,
        user_id text NOT NULL
    );
    CREATE TABLE public.user_mfa (
        id text NOT NULL,
        user_id text NOT NULL,
        secret text DEFAULT ''::text NOT NULL,
        credential text DEFAULT ''::text NOT NULL
    );
    CREATE TABLE public.mfa_backup_codes (
        id integer NOT NULL,
        user_id text NOT NULL,
        code_hash text NOT NULL
    );
    CREATE TABLE public.audit_log (
        id bigint NOT NULL,
        actor text NOT NULL,
        action text NOT NULL
    );
    COPY public.users (id, email) FROM stdin;
    u1\talice@example.com
    \\.
    """
)


# ─────────────────────────────────────────────────────────────────────
# 1. artifacts present + executable
# ─────────────────────────────────────────────────────────────────────


def test_artifacts_exist():
    for p in (FIELDS_YAML, ANONYMIZE_SH, RESTORE_SH, SERVICE_UNIT, TIMER_UNIT):
        assert p.is_file(), f"missing artifact: {p.relative_to(REPO_ROOT)}"


def test_shell_scripts_are_executable():
    for p in (ANONYMIZE_SH, RESTORE_SH):
        mode = p.stat().st_mode
        assert mode & 0o111, f"{p.relative_to(REPO_ROOT)} is not executable"


def test_shell_scripts_have_bash_shebang_and_strict_mode():
    for p in (ANONYMIZE_SH, RESTORE_SH):
        text = p.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash"), p
        assert "set -euo pipefail" in text, p


def test_shell_scripts_parse():
    for p in (ANONYMIZE_SH, RESTORE_SH):
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{p}: {r.stderr}"


# ─────────────────────────────────────────────────────────────────────
# 2. anonymize-fields.yaml — shape + Phase-1/Phase-2 dual-readability
# ─────────────────────────────────────────────────────────────────────


def test_fields_yaml_shape():
    spec = _spec()
    assert spec.get("version") == 1
    assert set(spec.get("strategies") or []) == PHASE1_STRATEGIES, "strategies list drifted from the 5 Phase-1 strategies"

    tables = spec.get("tables")
    assert isinstance(tables, list) and tables, "tables: must be a non-empty list"
    seen_test_user_without_id = []
    for t in tables:
        assert isinstance(t, dict) and t.get("name"), f"bad table entry: {t!r}"
        if t.get("truncate"):
            assert t["truncate"] is True, f"{t.get('name')!r}: truncate must be the literal true"
            continue  # a truncated table needs no columns
        cols = t.get("columns")
        assert isinstance(cols, list) and cols, f"table {t.get('name')!r} has no columns and is not truncate:true"
        for c in cols:
            assert isinstance(c, dict) and c.get("name") and c.get("strategy"), f"bad column entry in {t.get('name')!r}: {c!r}"
            assert c["strategy"] in PHASE1_STRATEGIES, f"unknown strategy {c['strategy']!r} in {t.get('name')!r}.{c.get('name')!r}"
            if c["strategy"] == "test_user" and not t.get("id_column"):
                seen_test_user_without_id.append(f"{t.get('name')}.{c.get('name')}")
    assert not seen_test_user_without_id, f"test_user strategy needs an id_column: {seen_test_user_without_id}"
    # the obviously transient/sensitive tables must be truncated, not column-masked
    truncated = {t["name"] for t in tables if t.get("truncate")}
    for must in ("sessions", "user_mfa", "audit_log"):
        assert must in truncated, f"{must!r} should be `truncate: true` in anonymize-fields.yaml"


def test_fields_yaml_pii_patterns_present_and_compilable():
    spec = _spec()
    pats = spec.get("pii_column_patterns")
    assert isinstance(pats, list) and len(pats) >= 5, "pii_column_patterns must be a meaningful list (the AnonymizeMissedField guard)"
    for p in pats:
        re.compile(p)  # raises on a bad pattern
    # the obvious offenders must be covered
    blob = "\n".join(pats)
    for needle in ("email", "phone", "password", "token", "secret"):
        assert needle in blob, f"pii_column_patterns is missing an entry covering {needle!r}"


def test_fields_yaml_greenmask_block_covers_every_strategy():
    """Phase 2 (6g) must be a one-file swap — the Greenmask translation for
    every Phase-1 strategy lives in this same YAML."""
    spec = _spec()
    gm = spec.get("greenmask") or {}
    mapping = gm.get("strategy_to_transformer") or {}
    assert PHASE1_STRATEGIES.issubset(set(mapping)), (
        f"greenmask.strategy_to_transformer is missing translations for "
        f"{PHASE1_STRATEGIES - set(mapping)}"
    )
    for strat, xf in mapping.items():
        assert isinstance(xf, dict) and xf.get("name"), f"greenmask mapping for {strat!r} has no transformer name"


def test_every_declared_pii_column_name_is_or_could_be_pattern_matched():
    """Sanity: a column we bothered to declare in `tables:` should look like
    PII by the pattern list too (catches a typo'd column name that the
    AnonymizeMissedField guard would then silently never flag)."""
    spec = _spec()
    pats = [re.compile(p, re.IGNORECASE) for p in spec.get("pii_column_patterns") or []]
    # a few declared columns are intentionally generic (e.g. `actor`, `username`,
    # `avatar_url`) and aren't meant to match the PII *name* heuristics — those
    # are masked because we know their semantics, not their name. Only assert
    # the strongly-PII-named ones match.
    strongly_named = {"email", "token_hash", "encrypted_token", "encrypted_ssh_key",
                      "encrypted_webhook_secret", "password_hash", "oidc_subject", "code_verifier"}
    for t in spec["tables"]:
        for c in (t.get("columns") or []):
            name = c["name"]
            if name in strongly_named:
                assert any(p.fullmatch(name) or p.match(name) for p in pats), (
                    f"{t['name']}.{name} is declared PII but matches no pii_column_patterns entry"
                )


# ─────────────────────────────────────────────────────────────────────
# 3. anonymize.sh — behaviour
# ─────────────────────────────────────────────────────────────────────


def test_anonymize_happy_path_emits_update_epilogue(tmp_path):
    proc, out_sql = _run_anonymize(tmp_path, COVERED_DUMP)
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    out = out_sql.read_text()
    # the original dump must be preserved verbatim as a prefix
    assert out.startswith(COVERED_DUMP.rstrip("\n")), "anonymize.sh must not mutate the input dump body"
    # transactional epilogue
    assert "BEGIN;" in out and "COMMIT;" in out
    assert "[OP-972] AUDIT-19b" in out
    # per-strategy SQL — email_hash / test_user / null / redact
    assert re.search(r'UPDATE "public"."users" SET "email" = \'redacted-\' \|\| left\(md5\("email"::text\), 12\)', out)
    assert 'UPDATE "public"."users" SET "name" = \'Test User \' || "id"::text;' in out
    assert 'UPDATE "public"."users" SET "password_hash" = NULL;' in out
    assert 'UPDATE "public"."users" SET "oidc_subject" = NULL;' in out
    assert 'UPDATE "public"."tenants" SET "name" = \'[redacted]\';' in out
    assert 'UPDATE "public"."git_accounts" SET "encrypted_token" = NULL;' in out
    assert 'UPDATE "public"."tenant_invites" SET "token_hash" = NULL;' in out
    # truncate:true tables -> DELETE FROM (transient/sensitive: sessions, MFA, audit_log)
    assert 'DELETE FROM "public"."sessions";' in out
    assert 'DELETE FROM "public"."user_mfa";' in out
    assert 'DELETE FROM "public"."audit_log";' in out
    # at least one masking statement per declared+present column
    assert out.count("\nUPDATE ") >= 10
    assert out.count("\nDELETE FROM ") >= 3


def test_anonymize_refuses_uncovered_pii_column(tmp_path):
    """AnonymizeMissedField: a new PII-shaped column not in the spec => exit 3,
    nothing written. Leaking real PII to staging is unacceptable."""
    dump = COVERED_DUMP.replace(
        "    oidc_provider text DEFAULT ''::text NOT NULL,\n",
        "    oidc_provider text DEFAULT ''::text NOT NULL,\n    recovery_phone text,\n",
    )
    proc, out_sql = _run_anonymize(tmp_path, dump)
    assert proc.returncode == 3, f"expected exit 3, got {proc.returncode}\nstderr:\n{proc.stderr}"
    assert "AnonymizeMissedField" in proc.stderr
    assert "recovery_phone" in proc.stderr
    assert not out_sql.exists(), "anonymize.sh must not write an output file when it refuses"


def test_anonymize_skips_table_absent_from_dump(tmp_path):
    """A YAML-declared table that isn't in this dump is skipped gracefully
    (a stale spec entry is not fatal — only *missing* coverage is). Holds for
    both column-masked tables and truncate:true tables."""
    dump = textwrap.dedent(
        """\
        CREATE TABLE public.users (
            id text NOT NULL,
            email text NOT NULL,
            name text DEFAULT ''::text NOT NULL,
            role text DEFAULT 'viewer'::text NOT NULL
        );
        """
    )
    proc, out_sql = _run_anonymize(tmp_path, dump)
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    out = out_sql.read_text()
    assert "-- skip public.tenants: not present in this dump" in out
    assert "-- skip public.sessions: not present in this dump (truncate)" in out
    assert 'UPDATE "public"."users" SET "email"' in out
    assert 'UPDATE "public"."users" SET "name" = \'Test User \' || "id"::text;' in out


def test_anonymize_rejects_test_user_strategy_without_id_column(tmp_path):
    bad_yaml = tmp_path / "bad-fields.yaml"
    bad_yaml.write_text(
        textwrap.dedent(
            """\
            version: 1
            strategies: [test_user]
            tables:
              - schema: public
                name: users
                columns:
                  - { name: full_name, strategy: test_user }
            pii_column_patterns:
              - "^full_name$"
            """
        )
    )
    dump = "CREATE TABLE public.users (\n    id integer NOT NULL,\n    full_name text\n);\n"
    proc, out_sql = _run_anonymize(tmp_path, dump, fields_yaml=bad_yaml)
    assert proc.returncode == 3, f"stderr:\n{proc.stderr}"
    assert "id_column" in proc.stderr
    assert not out_sql.exists()


def test_anonymize_usage_error_without_args(tmp_path):
    r = subprocess.run(["bash", str(ANONYMIZE_SH)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "usage" in r.stderr.lower()


def test_anonymize_documents_phase2_greenmask_swap():
    text = ANONYMIZE_SH.read_text(encoding="utf-8")
    assert "greenmask.io" in text
    assert "one-file replacement" in text or "one-file replacement" in text.replace("\n", " ")


# ─────────────────────────────────────────────────────────────────────
# 4. snapshot-restore.sh — contract + prereq-failure path
# ─────────────────────────────────────────────────────────────────────


def test_restore_pipeline_contract_in_text():
    text = RESTORE_SH.read_text(encoding="utf-8")
    # 1. pg_dump prod via the prod container's own pg_dump
    assert "pg_dump" in text
    assert "PROD_PG_CONTAINER" in text and "omnisight-pg-primary" in text
    assert "--no-owner" in text and "--no-privileges" in text
    # 2. delegates anonymization to anonymize.sh (the 6c->6g seam)
    assert "anonymize.sh" in text
    # 3. schema-drift sanity (AC #5)
    assert "SchemaDrift" in text and "pg_tables" in text
    # 4. rollback-safe restore: drop-and-rename + revert (AC #6)
    assert "ALTER DATABASE" in text and "RENAME TO" in text
    assert "_prev" in text and "revert_staging" in text
    assert "RestoreInterrupted" in text
    # 5. release_audit row (AC #1) + the documented schema caveat
    assert "release_audit" in text
    assert "snapshot_restore" in text
    assert "OMNISIGHT_DATABASE_URL" in text
    # 6. staging PG reached on the AUDIT-19a host port
    assert "STAGING_PG_PORT" in text and "55432" in text
    # error catalog wired to exit codes
    for code, name in ((2, "SchemaDrift"), (3, "AnonymizeMissedField"), (4, "RestoreInterrupted")):
        assert f"exit {code}" in text, f"missing `exit {code}` ({name})"


def test_restore_prereq_failure_is_clean_exit_5(tmp_path):
    """No docker / psql reachable => exit 5, no Postgres touched, and (with
    no DSN) the `release_audit` write is skipped — logged, not fatal."""
    workdir = tmp_path / "scratch"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "DOCKER_BIN": "/bin/false",
        "PSQL_BIN": "/bin/false",
        "SNAPSHOT_WORKDIR": str(workdir),
        # OMNISIGHT_DATABASE_URL deliberately unset
    }
    r = subprocess.run(["bash", str(RESTORE_SH)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 5, f"expected exit 5, got {r.returncode}\nstdout:{r.stdout}\nstderr:{r.stderr}"
    combined = r.stdout + r.stderr
    assert "release_audit row skipped" in combined
    assert "status=prereq_failed" in combined
    # nothing should have been left in the scratch dir
    assert not workdir.exists() or not any(workdir.iterdir())


def test_restore_documents_pluggability_and_audit_caveat():
    text = RESTORE_SH.read_text(encoding="utf-8")
    # 6c -> 6g -> 6h phased plan referenced; anonymize.sh named as the seam
    assert "6g" in text and "Greenmask" in text
    # the release_audit schema caveat (why outcome=noop, not snapshot_restore)
    assert "outcome='noop'" in text or "outcome=noop" in text
    assert "best-effort" in text


# ─────────────────────────────────────────────────────────────────────
# 5. systemd units
# ─────────────────────────────────────────────────────────────────────


def _section(unit_text: str, name: str) -> str:
    m = re.search(rf"^\[{re.escape(name)}\]\s*\n(.*?)(?=^\[|\Z)", unit_text, re.S | re.M)
    assert m, f"unit is missing a [{name}] section"
    return m.group(1)


def _directives(block: str, key: str) -> list[str]:
    return re.findall(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)


def test_timer_is_daily_0200_persistent():
    text = TIMER_UNIT.read_text(encoding="utf-8")
    timer = _section(text, "Timer")
    assert _directives(timer, "Unit") == ["staging-pg-snapshot.service"]
    oncal = _directives(timer, "OnCalendar")
    assert oncal and any("02:00" in v for v in oncal), f"OnCalendar must be 02:00: {oncal}"
    assert _directives(timer, "Persistent") == ["true"], "Persistent=true required (catch up after host downtime)"
    assert _directives(_section(text, "Install"), "WantedBy") == ["timers.target"]


def test_service_unit_runs_the_restore_script_oneshot():
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    svc = _section(text, "Service")
    assert _directives(svc, "Type") == ["oneshot"]
    execs = _directives(svc, "ExecStart")
    assert len(execs) == 1 and execs[0].endswith("infra/staging/snapshot-restore.sh"), execs
    # optional EnvironmentFile for the release-audit DSN + staging knobs (the `-` => optional)
    envfiles = _directives(svc, "EnvironmentFile")
    assert any(v.startswith("-") and v.endswith("release-audit.env") for v in envfiles), envfiles
    assert any(v.startswith("-") and "staging-pg-snapshot" in v for v in envfiles), envfiles
    # carries the prod/staging connection defaults so it works out of the box
    env = "\n".join(_directives(svc, "Environment"))
    assert "PROD_PG_CONTAINER=omnisight-pg-primary" in env
    assert "STAGING_PG_PORT=55432" in env
    assert _directives(_section(text, "Install"), "WantedBy") == ["default.target"]


def test_units_reference_ticket():
    for p in (SERVICE_UNIT, TIMER_UNIT):
        assert "OP-972" in p.read_text(encoding="utf-8"), p
