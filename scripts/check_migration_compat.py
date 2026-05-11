"""OP-765 + OP-865 — patchset Alembic backwards-compatibility gate.

The full DB engine matrix validates the committed migration chain. This
gate is narrower and PR-shaped:

* discover Alembic migration files changed in the patchset,
* for each changed migration, upgrade from its parent revision to that
  revision, run ``alembic downgrade -1``, and assert the schema
  fingerprint returns to the parent shape,
* reject obvious old-code/new-schema hazards before they land, and
* optionally run the previous-release image's smoke command against the
  newly migrated schema.

OP-865 layers an AST-based per-migration compat enforcer on top:

* every changed migration must declare a ``backwards-compat:`` docstring
  tag from ``{safe, breaking, deprecation-window-1of2,
  deprecation-window-2of2}``;
* ``op.drop_column`` is only allowed inside ``deprecation-window-2of2``
  whose parent migration tagged ``deprecation-window-1of2`` actually
  renamed the column to a ``_deprecated`` shadow;
* renames must use ``op.alter_column(new_column_name=...)``; drop+add of
  a same-table column in one revision is rejected as an old-code break;
* adding a NOT NULL column without a server-side default is rejected;
* adding an enum value at a non-tail position (``ALTER TYPE ... ADD
  VALUE 'X' BEFORE/AFTER 'Y'``) is rejected — Postgres only safely
  appends at the tail in a single-statement migration;
* migrations tagged ``breaking`` only ship when the commit carries a
  ``migration:approved-breaking`` trailer.

Two overrides exist:

* legacy OP-765 ``migration:break-allowed`` label + ``--approved-by sora``
  — bypasses every gate; appends a JSONL audit row.
* OP-865 ``migration:approved-breaking`` commit trailer — only bypasses
  the per-migration ``breaking`` tag check (still has to pass the other
  AST checks unless those individually permit the operation).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
OVERRIDE_LABEL = "migration:break-allowed"
APPROVED_BREAKING_TRAILER = "migration:approved-breaking"

VALID_COMPAT_TAGS = (
    "safe",
    "breaking",
    "deprecation-window-1of2",
    "deprecation-window-2of2",
)

# Tag formats accepted in the docstring. Capture group 1 is the tag value.
# OP-765 shipped ``Backwards-compat: <value>`` in the template; OP-865 prefers
# lowercase ``backwards-compat: value`` — accept either to keep older migrations
# rendered before the template update still valid.
COMPAT_TAG_RE = re.compile(
    r"^[ \t]*[Bb]ackwards-compat[ \t]*:[ \t]*([A-Za-z0-9_-]+)",
    re.MULTILINE,
)


class MigrationBreakingChangeRefused(Exception):
    """Migration tagged ``breaking`` without ``migration:approved-breaking``."""


class MigrationMetadataMissing(Exception):
    """Migration docstring lacks the ``backwards-compat:`` tag."""


class MigrationEnumNonTail(Exception):
    """Enum value added at a non-tail position (``BEFORE``/``AFTER``)."""

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


# ── OP-865 AST-based compat enforcer ─────────────────────────────────


def _docstring(tree: ast.Module) -> str:
    return ast.get_docstring(tree) or ""


def parse_compat_tag(source: str) -> str | None:
    """Return the ``backwards-compat:`` value from the module docstring.

    The tag must live in the module docstring (not a random comment) so a
    forgotten import block cannot accidentally satisfy the check. Returns
    ``None`` when no tag is present.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    docstring = _docstring(tree)
    if not docstring:
        return None
    match = COMPAT_TAG_RE.search(docstring)
    if not match:
        return None
    value = match.group(1).strip().lower()
    # The mako template ships ``<safe|requires-coordinated-deploy>`` as a
    # placeholder. Treat the literal placeholder as "missing" so an
    # un-filled-in migration is loudly rejected.
    if value.startswith("<") or value == "":
        return None
    return value


def check_compat_metadata(source: str) -> CheckResult:
    tag = parse_compat_tag(source)
    if tag is None:
        return CheckResult(
            ok=False,
            name="compat-metadata",
            reason=(
                "MigrationMetadataMissing: docstring must declare "
                "`backwards-compat: " + "|".join(VALID_COMPAT_TAGS) + "`"
            ),
            evidence="module-level docstring scan",
        )
    if tag not in VALID_COMPAT_TAGS:
        return CheckResult(
            ok=False,
            name="compat-metadata",
            reason=(
                f"MigrationMetadataMissing: unknown tag value `{tag}` — "
                "must be one of " + "|".join(VALID_COMPAT_TAGS)
            ),
            evidence="module-level docstring scan",
        )
    return CheckResult(
        ok=True,
        name="compat-metadata",
        reason=f"backwards-compat: {tag}",
        evidence="module-level docstring scan",
    )


def _iter_op_calls(tree: ast.Module) -> list[ast.Call]:
    """Yield every ``op.<func>(...)`` call in the module AST."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "op":
                calls.append(node)
    return calls


def _call_first_string(call: ast.Call, kw_name: str | None = None) -> str | None:
    if kw_name is not None:
        for kw in call.keywords:
            if kw.arg == kw_name and isinstance(kw.value, ast.Constant):
                value = kw.value.value
                if isinstance(value, str):
                    return value
        return None
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        if isinstance(value, str):
            return value
    return None


@dataclass(frozen=True)
class _DropEvent:
    table: str
    column: str


@dataclass(frozen=True)
class _AddEvent:
    table: str
    column: str


def _drop_column_events(tree: ast.Module) -> list[_DropEvent]:
    events: list[_DropEvent] = []
    for call in _iter_op_calls(tree):
        if isinstance(call.func, ast.Attribute) and call.func.attr == "drop_column":
            args: list[str] = []
            for a in call.args[:2]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    args.append(a.value)
            if len(args) == 2:
                events.append(_DropEvent(table=args[0], column=args[1]))
    return events


def _add_column_events(tree: ast.Module) -> list[_AddEvent]:
    events: list[_AddEvent] = []
    for call in _iter_op_calls(tree):
        if isinstance(call.func, ast.Attribute) and call.func.attr == "add_column":
            table = _call_first_string(call)
            column: str | None = None
            for a in call.args[1:]:
                if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute):
                    if a.func.attr == "Column":
                        column = _call_first_string(a)
                        break
            if table and column:
                events.append(_AddEvent(table=table, column=column))
    return events


def _alter_column_renames(tree: ast.Module) -> list[tuple[str, str, str]]:
    """Return ``(table, old_name, new_name)`` for each op.alter_column rename."""
    out: list[tuple[str, str, str]] = []
    for call in _iter_op_calls(tree):
        if isinstance(call.func, ast.Attribute) and call.func.attr == "alter_column":
            table = _call_first_string(call) or ""
            old_name = ""
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                value = call.args[1].value
                if isinstance(value, str):
                    old_name = value
            new_name = _call_first_string(call, kw_name="new_column_name")
            if table and old_name and new_name:
                out.append((table, old_name, new_name))
    return out


def check_rename_pattern(source: str) -> CheckResult:
    """Reject drop+add of a same-table column in a single revision.

    The prescribed rename is ``op.alter_column(table, "old",
    new_column_name="new")``. Drop-then-add is destructive for any
    consumer still reading the old name during deploy.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return CheckResult(
            False,
            "rename-pattern",
            "could not parse migration for AST scan",
            "ast.parse",
        )
    drops = _drop_column_events(tree)
    adds = _add_column_events(tree)
    suspicious: list[tuple[str, str, str]] = []
    for d in drops:
        for a in adds:
            if a.table == d.table and a.column != d.column:
                suspicious.append((d.table, d.column, a.column))
    if suspicious:
        first = suspicious[0]
        return CheckResult(
            False,
            "rename-pattern",
            (
                "MigrationBreakingChangeRefused: drop+add of column on "
                f"table `{first[0]}` ({first[1]} → {first[2]}) — use "
                "`op.alter_column(..., new_column_name=...)` to rename "
                "in place"
            ),
            "AST drop_column + add_column on same table",
        )
    return CheckResult(
        True,
        "rename-pattern",
        "no drop+add rename detected (alter_column or no rename)",
        "AST scan",
    )


_ENUM_NON_TAIL_RE = re.compile(
    r"\bALTER\s+TYPE\b[^;]+?\bADD\s+VALUE\b[^;]*?\b(BEFORE|AFTER)\b",
    re.I | re.S,
)


def check_enum_tail_position(source: str) -> CheckResult:
    """Reject ``ALTER TYPE ... ADD VALUE ... BEFORE/AFTER``.

    Postgres can append an enum value at the tail in a single statement,
    but ``BEFORE``/``AFTER`` positioning rewrites the catalog ordering
    and cannot be wrapped in a transaction with other DDL. Treat any
    positional ADD VALUE as non-tail.
    """
    match = _ENUM_NON_TAIL_RE.search(source)
    if match:
        return CheckResult(
            False,
            "enum-tail",
            (
                "MigrationEnumNonTail: ALTER TYPE ... ADD VALUE ... "
                f"{match.group(1).upper()} forbidden — append at tail "
                "(no BEFORE/AFTER) for compat-safe deploy"
            ),
            "regex ALTER TYPE positional ADD VALUE scan",
        )
    return CheckResult(
        True,
        "enum-tail",
        "no positional ADD VALUE statements found",
        "regex scan",
    )


def _revision_index(versions_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not versions_dir.exists():
        return out
    for path in versions_dir.glob("*.py"):
        if path.name == "__init__.py":
            continue
        try:
            spec = parse_migration(path)
        except Exception:
            continue
        out[spec.revision] = path
    return out


def check_drop_column_deprecation(
    spec: MigrationSpec,
    *,
    versions_dir: Path = VERSIONS_DIR,
) -> CheckResult:
    """``op.drop_column`` only allowed in deprecation-window-2of2 chain.

    The chain shape:

      Revision N-1 (deprecation-window-1of2):
        op.alter_column("t", "col", new_column_name="col_deprecated")
      Revision N (deprecation-window-2of2):
        op.drop_column("t", "col_deprecated")
    """
    source = spec.path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return CheckResult(
            False,
            "drop-deprecation-window",
            "could not parse migration for AST scan",
            "ast.parse",
        )
    drops = _drop_column_events(tree)
    if not drops:
        return CheckResult(
            True,
            "drop-deprecation-window",
            "no op.drop_column calls",
            "AST scan",
        )
    tag = parse_compat_tag(source)
    if tag != "deprecation-window-2of2":
        return CheckResult(
            False,
            "drop-deprecation-window",
            (
                "MigrationBreakingChangeRefused: op.drop_column requires "
                "`backwards-compat: deprecation-window-2of2` — see "
                "docs/operations/migration-deprecation-runbook.md"
            ),
            f"drop targets: {[(d.table, d.column) for d in drops]}",
        )
    parent_rev = spec.down_revision
    if not isinstance(parent_rev, str):
        return CheckResult(
            False,
            "drop-deprecation-window",
            "MigrationBreakingChangeRefused: deprecation-window-2of2 must have a single parent",
            f"down_revision: {parent_rev!r}",
        )
    parent_path = _revision_index(versions_dir).get(parent_rev)
    if parent_path is None:
        return CheckResult(
            False,
            "drop-deprecation-window",
            f"MigrationBreakingChangeRefused: parent revision `{parent_rev}` not found",
            str(versions_dir),
        )
    parent_source = parent_path.read_text(encoding="utf-8")
    parent_tag = parse_compat_tag(parent_source)
    if parent_tag != "deprecation-window-1of2":
        return CheckResult(
            False,
            "drop-deprecation-window",
            (
                "MigrationBreakingChangeRefused: parent revision "
                f"`{parent_rev}` must be tagged "
                "`backwards-compat: deprecation-window-1of2`"
            ),
            f"parent tag: {parent_tag!r}",
        )
    try:
        parent_tree = ast.parse(parent_source)
    except SyntaxError:
        return CheckResult(
            False,
            "drop-deprecation-window",
            "could not parse parent migration for AST scan",
            str(parent_path),
        )
    parent_renames = _alter_column_renames(parent_tree)
    for drop in drops:
        if not drop.column.endswith("_deprecated"):
            return CheckResult(
                False,
                "drop-deprecation-window",
                (
                    "MigrationBreakingChangeRefused: drop_column target "
                    f"`{drop.table}.{drop.column}` must end with "
                    "`_deprecated` (renamed in window-1of2)"
                ),
                _display_path(spec.path),
            )
        matched = any(
            t == drop.table and new == drop.column
            for (t, _old, new) in parent_renames
        )
        if not matched:
            return CheckResult(
                False,
                "drop-deprecation-window",
                (
                    "MigrationBreakingChangeRefused: parent revision "
                    f"`{parent_rev}` does not rename a column to "
                    f"`{drop.table}.{drop.column}`"
                ),
                _display_path(parent_path),
            )
    return CheckResult(
        True,
        "drop-deprecation-window",
        "drop_column targets all resolve through deprecation-window-1of2 parent",
        _display_path(spec.path),
    )


def parse_commit_trailers(commit_message: str) -> dict[str, list[str]]:
    """Parse ``Key: value`` trailers from the last paragraph of a commit message.

    Returns a dict mapping lowercased keys to lists of values (a single
    trailer key may appear multiple times). Empty / missing commit
    messages yield an empty dict.
    """
    out: dict[str, list[str]] = {}
    if not commit_message:
        return out
    text = commit_message.rstrip()
    paragraphs = re.split(r"\n\s*\n", text)
    if not paragraphs:
        return out
    last = paragraphs[-1]
    for line in last.splitlines():
        m = re.match(r"^([A-Za-z][A-Za-z0-9_.:-]*)[ \t]*:[ \t]*(.+?)[ \t]*$", line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        out.setdefault(key, []).append(m.group(2).strip())
    return out


def has_approved_breaking_trailer(commit_message: str) -> bool:
    """True iff the commit message carries a ``migration:approved-breaking`` trailer.

    Accepts both ``migration:approved-breaking: true`` (key/value form)
    and the bare ``migration:approved-breaking`` line (presence form),
    matching the GitHub trailer conventions used elsewhere in OmniSight.
    """
    if not commit_message:
        return False
    trailers = parse_commit_trailers(commit_message)
    if APPROVED_BREAKING_TRAILER in trailers:
        return True
    # Bare-presence form: a line that is exactly the trailer key.
    last = re.split(r"\n\s*\n", commit_message.rstrip())[-1]
    for line in last.splitlines():
        if line.strip().lower() == APPROVED_BREAKING_TRAILER:
            return True
    return False


def check_breaking_trailer(source: str, commit_message: str | None) -> CheckResult:
    tag = parse_compat_tag(source)
    if tag != "breaking":
        return CheckResult(
            True,
            "breaking-trailer",
            f"backwards-compat: {tag} does not require approved-breaking trailer",
            "tag scan",
        )
    if commit_message and has_approved_breaking_trailer(commit_message):
        return CheckResult(
            True,
            "breaking-trailer",
            "migration:approved-breaking trailer present",
            "commit message",
        )
    return CheckResult(
        False,
        "breaking-trailer",
        (
            "MigrationBreakingChangeRefused: migration tagged "
            "`backwards-compat: breaking` requires a "
            "`migration:approved-breaking` commit trailer"
        ),
        "commit message",
    )


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
    parser.add_argument(
        "--commit-message",
        default=os.environ.get("MIGRATION_COMPAT_COMMIT_MESSAGE"),
        help=(
            "commit message text for `migration:approved-breaking` trailer "
            "detection; defaults to MIGRATION_COMPAT_COMMIT_MESSAGE env or "
            "the head commit's message"
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

    commit_message = args.commit_message
    if commit_message is None:
        try:
            head = subprocess.run(
                ["git", "log", "-1", "--pretty=%B", args.head_ref],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if head.returncode == 0:
                commit_message = head.stdout
        except (OSError, subprocess.SubprocessError):
            commit_message = None

    results: list[CheckResult] = []
    for migration in migrations:
        source = migration.path.read_text(encoding="utf-8")
        results.append(check_compat_metadata(source))
        results.append(check_rename_pattern(source))
        results.append(check_enum_tail_position(source))
        results.append(check_drop_column_deprecation(migration))
        results.append(check_breaking_trailer(source, commit_message))
        # OP-765 ``classify_old_code_compat`` rejects all destructive ops
        # unconditionally — its philosophy is "old code shouldn't see
        # new schema". OP-865 carves out a documented exit through the
        # deprecation-window-* + breaking-trailer flow; only apply the
        # old-code/new-schema scan to ``safe`` tagged migrations and to
        # any migration whose tag could not be parsed (defensive: we
        # already failed compat-metadata, but the OP-765 scan is the
        # second wall).
        tag = parse_compat_tag(source)
        if tag in (None, "safe"):
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
