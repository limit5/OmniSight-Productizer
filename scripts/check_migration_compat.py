"""OP-765 — patchset Alembic backwards-compatibility gate.

The full DB engine matrix validates the committed migration chain. This
gate is narrower and PR-shaped:

* discover Alembic migration files changed in the patchset,
* for each changed migration, upgrade from its parent revision to that
  revision, run ``alembic downgrade -1``, and assert the schema
  fingerprint returns to the parent shape,
* reject obvious old-code/new-schema hazards before they land, and
* optionally run the previous-release image's smoke command against the
  newly migrated schema.

The ``migration:break-allowed`` override is deliberately explicit:
the label must be present, ``--approved-by sora`` must be supplied, and
a JSONL audit row is appended before the bypass succeeds.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
OVERRIDE_LABEL = "migration:break-allowed"

DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bALTER\s+TABLE\b.+\bRENAME\s+COLUMN\b", re.I | re.S),
        "column rename",
    ),
    (
        re.compile(r"\bALTER\s+TABLE\b.+\bDROP\s+COLUMN\b", re.I | re.S),
        "column drop",
    ),
    (
        re.compile(r"\bALTER\s+TABLE\b.+\bALTER\s+COLUMN\b.+\bTYPE\b", re.I | re.S),
        "column type change",
    ),
    (re.compile(r"\bDROP\s+TABLE\b", re.I), "table drop"),
    (
        re.compile(r"\bop\.alter_column\s*\([^)]*\bnew_column_name\s*=", re.I | re.S),
        "column rename",
    ),
    (re.compile(r"\bop\.drop_column\s*\(", re.I), "column drop"),
    (re.compile(r"\bop\.drop_table\s*\(", re.I), "table drop"),
]
NOT_NULL_ADD_WITHOUT_DEFAULT_RE = re.compile(
    r"\bADD\s+COLUMN\b(?P<body>[^;\n]+?\bNOT\s+NULL\b)(?![^;\n]*\bDEFAULT\b)",
    re.I | re.S,
)
OP_ADD_COLUMN_RE = re.compile(
    r"\bop\.add_column\s*\((?P<body>.*?)\)\s*(?:\n|$)",
    re.I | re.S,
)


@dataclass(frozen=True)
class MigrationSpec:
    path: Path
    revision: str
    down_revision: str | tuple[str, ...] | None


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    name: str
    reason: str
    evidence: str


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise ValueError(f"missing `{name} = ...` assignment")


def parse_migration(path: Path) -> MigrationSpec:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    revision = _literal_assignment(tree, "revision")
    down_revision = _literal_assignment(tree, "down_revision")
    if not isinstance(revision, str):
        raise ValueError(f"{_display_path(path)}: revision must be a string")
    if down_revision is not None and not isinstance(down_revision, (str, tuple, list)):
        raise ValueError(
            f"{_display_path(path)}: down_revision must be str, tuple/list, or None"
        )
    if isinstance(down_revision, list):
        down_revision = tuple(str(v) for v in down_revision)
    if isinstance(down_revision, tuple):
        down_revision = tuple(str(v) for v in down_revision)
    return MigrationSpec(path=path, revision=revision, down_revision=down_revision)


def changed_migration_files(base_ref: str, head_ref: str) -> list[Path]:
    proc = subprocess.run(
        [
            "git", "diff", "--name-only", f"{base_ref}...{head_ref}",
            "--", str(VERSIONS_DIR.relative_to(REPO_ROOT)),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or "git diff failed while discovering migrations"
        )
    files: list[Path] = []
    for line in proc.stdout.splitlines():
        path = REPO_ROOT / line
        if path.suffix == ".py" and path.name != "__init__.py" and path.exists():
            files.append(path)
    return sorted(files)


def classify_old_code_compat(source: str) -> CheckResult:
    for pattern, label in DESTRUCTIVE_PATTERNS:
        if pattern.search(source):
            return CheckResult(
                ok=False,
                name="old-code-new-schema",
                reason=f"old code + new schema regression: {label}",
                evidence="static destructive-operation scan",
            )
    if NOT_NULL_ADD_WITHOUT_DEFAULT_RE.search(source):
        return CheckResult(
            ok=False,
            name="old-code-new-schema",
            reason="old code + new schema regression: NOT NULL column add without DEFAULT",
            evidence="static additive-column scan",
        )
    for match in OP_ADD_COLUMN_RE.finditer(source):
        body = match.group("body")
        if (
            "nullable=False" in body.replace(" ", "")
            and "server_default" not in body
            and "default=" not in body
        ):
            return CheckResult(
                ok=False,
                name="old-code-new-schema",
                reason="old code + new schema regression: NOT NULL column add without DEFAULT",
                evidence="static op.add_column scan",
            )
    return CheckResult(
        ok=True,
        name="old-code-new-schema",
        reason="no static old-code/new-schema hazards detected",
        evidence="static additive-operation scan",
    )


def _sqlite_fingerprint(db_path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with sqlite3.connect(str(db_path)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
                "ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            out[table] = sorted(row[1] for row in cols)
    return out


def _postgres_fingerprint(url: str) -> dict[str, list[str]]:
    from sqlalchemy import create_engine, text

    out: dict[str, list[str]] = {}
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = [
                row[0]
                for row in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name NOT LIKE 'alembic_%' "
                    "ORDER BY table_name"
                ))
            ]
            for table in tables:
                cols = conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:table "
                        "ORDER BY column_name"
                    ),
                    {"table": table},
                )
                out[table] = sorted(row[0] for row in cols)
    finally:
        engine.dispose()
    return out


def _alembic(cmd: Sequence[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["alembic", *cmd],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def verify_downgrade_reverts(
    spec: MigrationSpec,
    *,
    engine: str,
    url: str | None,
) -> CheckResult:
    if spec.down_revision is None:
        return CheckResult(
            True,
            "downgrade-revert",
            "base migration has no parent revision",
            _display_path(spec.path),
        )
    if isinstance(spec.down_revision, tuple):
        return CheckResult(
            True,
            "downgrade-revert",
            "merge migration has multiple parents; chain sweep covers downgrade",
            _display_path(spec.path),
        )

    with tempfile.TemporaryDirectory(prefix="migration-compat-") as tmp:
        tmp_path = Path(tmp)
        env = os.environ.copy()
        env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"
        db_file = tmp_path / "compat.db"
        if engine == "sqlite":
            env["OMNISIGHT_DATABASE_PATH"] = str(db_file)
            env.pop("SQLALCHEMY_URL", None)
        else:
            if not url:
                return CheckResult(
                    False,
                    "downgrade-revert",
                    "--url is required for postgres mode",
                    _display_path(spec.path),
                )
            env["SQLALCHEMY_URL"] = url
            env.pop("OMNISIGHT_DATABASE_PATH", None)

        parent = spec.down_revision
        for phase, cmd in (
            (f"upgrade parent {parent}", ["upgrade", parent]),
            (f"upgrade revision {spec.revision}", ["upgrade", spec.revision]),
            ("downgrade -1", ["downgrade", "-1"]),
        ):
            proc = _alembic(cmd, env)
            if proc.returncode != 0:
                return CheckResult(
                    False,
                    "downgrade-revert",
                    f"{phase} failed rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()[-500:]}",
                    _display_path(spec.path),
                )
            if phase.startswith("upgrade parent"):
                before = (
                    _sqlite_fingerprint(db_file)
                    if engine == "sqlite"
                    else _postgres_fingerprint(url or "")
                )

        after = (
            _sqlite_fingerprint(db_file)
            if engine == "sqlite"
            else _postgres_fingerprint(url or "")
        )
        if before != after:
            changed = sorted(
                k for k in set(before) | set(after) if before.get(k) != after.get(k)
            )
            return CheckResult(
                False,
                "downgrade-revert",
                f"alembic downgrade -1 did not restore parent schema: {changed[:8]}",
                _display_path(spec.path),
            )
    return CheckResult(
        True,
        "downgrade-revert",
        "alembic downgrade -1 restored parent schema",
        _display_path(spec.path),
    )


def run_old_image_smoke(
    *,
    old_image: str | None,
    smoke_cmd: str | None,
    url: str | None,
) -> CheckResult:
    if not old_image and not smoke_cmd:
        return CheckResult(
            True,
            "previous-release-smoke",
            "no previous-release smoke configured; static compatibility scan used",
            "MIGRATION_COMPAT_OLD_IMAGE/MIGRATION_COMPAT_SMOKE_CMD unset",
        )
    if old_image and not url:
        return CheckResult(
            False,
            "previous-release-smoke",
            "old image smoke requires --url",
            old_image,
        )
    if old_image:
        cmd = [
            "docker", "run", "--rm", "--network", "host",
            "-e", f"SQLALCHEMY_URL={url}",
            "-e", f"OMNI_TEST_PG_URL={url}",
            old_image,
            "sh", "-lc", smoke_cmd or "pytest -q backend/tests/smoke",
        ]
        env = None
    else:
        cmd = ["sh", "-lc", smoke_cmd or ""]
        env = {
            **os.environ,
            "SQLALCHEMY_URL": url or "",
            "OMNI_TEST_PG_URL": url or "",
        }
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        return CheckResult(
            False,
            "previous-release-smoke",
            "old code + new schema regression: "
            f"smoke failed rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[-500:]}",
            old_image or smoke_cmd or "smoke command",
        )
    return CheckResult(
        True,
        "previous-release-smoke",
        "previous-release smoke passed",
        old_image or smoke_cmd or "smoke command",
    )


def _labels(raw: str) -> set[str]:
    return {part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()}


def override_allowed(*, labels: set[str], approved_by: str | None) -> bool:
    return OVERRIDE_LABEL in labels and approved_by == "sora"


def append_override_audit(
    path: Path,
    *,
    ticket: str,
    labels: set[str],
    approved_by: str,
    migrations: list[MigrationSpec],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticket": ticket,
        "event": "migration_compat_override",
        "label": OVERRIDE_LABEL,
        "approved_by": approved_by,
        "labels": sorted(labels),
        "migrations": [_display_path(m.path) for m in migrations],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _emit_ci_error(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=migration_compat_check::{message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="migration files; default discovers git diff",
    )
    parser.add_argument(
        "--base-ref",
        default=os.environ.get("GITHUB_BASE_REF") or "origin/main",
    )
    parser.add_argument("--head-ref", default=os.environ.get("GITHUB_SHA") or "HEAD")
    parser.add_argument("--engine", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--url", default=os.environ.get("MIGRATION_COMPAT_DB_URL"))
    parser.add_argument(
        "--old-image",
        default=os.environ.get("MIGRATION_COMPAT_OLD_IMAGE"),
    )
    parser.add_argument(
        "--old-code-smoke-cmd",
        default=os.environ.get("MIGRATION_COMPAT_SMOKE_CMD"),
    )
    parser.add_argument("--ticket", default=os.environ.get("JIRA_TICKET_KEY", "OP-765"))
    parser.add_argument(
        "--ticket-labels",
        default=os.environ.get("JIRA_TICKET_LABELS", ""),
    )
    parser.add_argument(
        "--approved-by",
        default=os.environ.get("MIGRATION_COMPAT_APPROVED_BY"),
    )
    parser.add_argument(
        "--audit-log",
        default=os.environ.get(
            "MIGRATION_COMPAT_AUDIT_LOG",
            "artifacts/migration-compat-overrides.jsonl",
        ),
    )
    args = parser.parse_args(argv)

    raw_files = (
        [Path(f) for f in args.files]
        if args.files
        else changed_migration_files(args.base_ref, args.head_ref)
    )
    files = [p if p.is_absolute() else (REPO_ROOT / p) for p in raw_files]
    migrations = [parse_migration(path) for path in files if path.exists()]

    report: dict[str, object] = {
        "ticket": args.ticket,
        "migrations": [_display_path(m.path) for m in migrations],
        "checks": [],
        "ok": True,
    }
    if not migrations:
        report["verdict"] = "no alembic migrations in patchset"
        print(json.dumps(report, indent=2))
        return 0

    label_set = _labels(args.ticket_labels)
    if OVERRIDE_LABEL in label_set:
        if not override_allowed(labels=label_set, approved_by=args.approved_by):
            result = CheckResult(
                False,
                "override",
                "migration:break-allowed requires sora approval",
                args.ticket,
            )
            report["checks"].append(result.__dict__)
            report["ok"] = False
            _emit_ci_error(result.reason)
            print(json.dumps(report, indent=2))
            return 1
        append_override_audit(
            Path(args.audit_log),
            ticket=args.ticket,
            labels=label_set,
            approved_by=args.approved_by or "",
            migrations=migrations,
        )
        result = CheckResult(
            True,
            "override",
            "migration:break-allowed override audit-logged",
            args.audit_log,
        )
        report["checks"].append(result.__dict__)
        report["verdict"] = "override"
        print(json.dumps(report, indent=2))
        return 0

    results: list[CheckResult] = []
    for migration in migrations:
        source = migration.path.read_text(encoding="utf-8")
        results.append(classify_old_code_compat(source))
        results.append(verify_downgrade_reverts(migration, engine=args.engine, url=args.url))

    if args.engine == "postgres" and args.url:
        proc = _alembic(
            ["upgrade", "head"],
            {
                **os.environ,
                "SQLALCHEMY_URL": args.url,
                "OMNISIGHT_SKIP_FS_MIGRATIONS": "1",
            },
        )
        if proc.returncode != 0:
            results.append(
                CheckResult(
                    False,
                    "previous-release-smoke",
                    f"upgrade head before smoke failed rc={proc.returncode}",
                    args.url,
                )
            )
        else:
            results.append(
                run_old_image_smoke(
                    old_image=args.old_image,
                    smoke_cmd=args.old_code_smoke_cmd,
                    url=args.url,
                )
            )
    else:
        results.append(
            run_old_image_smoke(
                old_image=args.old_image,
                smoke_cmd=args.old_code_smoke_cmd,
                url=args.url,
            )
        )

    for result in results:
        report["checks"].append(result.__dict__)
        if not result.ok:
            report["ok"] = False
            _emit_ci_error(result.reason)

    if report["ok"]:
        report["verdict"] = "Verified +1"
    else:
        report["verdict"] = "Verified -1"
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
