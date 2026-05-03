"""FX.7.6 — drift guard for ``scripts/check_alembic_downgrade.py``.

Two layers of coverage:

1. **Live tree clean** — the linter must report 0 violations against the
   current ``backend/alembic/versions/`` tree. If anyone re-introduces
   a silent ``def downgrade(): pass`` (or removes the
   ``# alembic-allow-noop-downgrade:`` marker from one of the 7
   grandfathered files), CI fails red.

2. **Linter behaves** — small in-process fixtures exercise each
   classification branch (clean / no-op-no-marker / marker-too-short /
   marker-OK / missing-downgrade / non-noop). This pins the linter's
   own contract so a refactor of the script can't silently weaken
   enforcement.

The two layers protect different failure modes:

* (1) catches "someone snuck in a bad migration".
* (2) catches "someone weakened the linter so future bad migrations
  won't be caught".

Both must stay green.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_alembic_downgrade.py"
VERSIONS_DIR = REPO_ROOT / "backend" / "alembic" / "versions"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_alembic_downgrade", SCRIPT_PATH
    )
    assert spec and spec.loader, "linter script not importable"
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_alembic_downgrade", mod)
    spec.loader.exec_module(mod)
    return mod


def test_live_tree_has_zero_violations() -> None:
    """The committed `backend/alembic/versions/` tree must lint clean.

    If this fails, either (a) you added a new migration with
    `def downgrade(): pass` and no marker — write real rollback SQL or
    add the `# alembic-allow-noop-downgrade: <reason>` marker; or
    (b) you removed the marker from one of the 7 grandfathered files
    (FX.7.6) — restore it.
    """
    mod = _load_module()
    violations: list[str] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        violations.extend(mod.check_file(path))
    assert violations == [], (
        "FX.7.6 alembic downgrade enforcement found violations:\n  - "
        + "\n  - ".join(violations)
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def linter():
    return _load_module()


def test_clean_downgrade_passes(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9990_clean.py",
        '''"""Clean downgrade with real SQL."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    op.execute("DROP TABLE x")
''',
    )
    assert linter.check_file(p) == []


def test_silent_pass_is_rejected(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9991_silent.py",
        '''"""Silent no-op."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    pass
''',
    )
    out = linter.check_file(p)
    assert len(out) == 1
    assert "empty / `pass`" in out[0]
    assert "alembic-allow-noop-downgrade" in out[0]


def test_marker_with_reason_passes(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9992_marked.py",
        '''"""Marked no-op with adequate reason."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    # alembic-allow-noop-downgrade: dropping x would orphan in-flight references and lose forensic data
    pass
''',
    )
    assert linter.check_file(p) == []


def test_marker_too_short_is_rejected(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9993_short.py",
        '''"""Marker too short."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    # alembic-allow-noop-downgrade: nope
    pass
''',
    )
    out = linter.check_file(p)
    assert len(out) == 1
    assert "too short" in out[0]


def test_missing_downgrade_is_rejected(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9994_missing.py",
        '''"""Missing downgrade."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")
''',
    )
    out = linter.check_file(p)
    assert len(out) == 1
    assert "missing top-level `def downgrade()`" in out[0]


def test_docstring_only_body_is_treated_as_noop(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9995_docstring.py",
        '''"""Docstring-only body counts as no-op."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    """Cannot roll this back."""
''',
    )
    out = linter.check_file(p)
    assert len(out) == 1
    assert "docstring-only" in out[0]


def test_ellipsis_body_is_treated_as_noop(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9996_ellipsis.py",
        '''"""Ellipsis body counts as no-op."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    ...
''',
    )
    out = linter.check_file(p)
    assert len(out) == 1


def test_marker_outside_function_does_not_count(linter, tmp_path) -> None:
    """The marker must live inside the downgrade body, not at module
    scope — otherwise an author could drop a single marker comment at
    the top of the file and silently bypass every future no-op check.
    """
    p = _write(
        tmp_path,
        "9997_outside.py",
        '''"""Marker at module scope must NOT count."""
# alembic-allow-noop-downgrade: this comment is at module scope and should not satisfy the check
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    pass
''',
    )
    out = linter.check_file(p)
    assert len(out) == 1
    assert "empty / `pass`" in out[0]


def test_main_returns_nonzero_on_violation(linter, tmp_path, capsys) -> None:
    p = _write(
        tmp_path,
        "9998_violator.py",
        '''"""Violation."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    pass
''',
    )
    rc = linter.main([str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FX.7.6" in err
    assert "violation(s)" in err


def test_main_returns_zero_on_clean(linter, tmp_path) -> None:
    p = _write(
        tmp_path,
        "9999_clean.py",
        '''"""Clean."""
from alembic import op


def upgrade() -> None:
    op.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")


def downgrade() -> None:
    op.execute("DROP TABLE x")
''',
    )
    assert linter.main([str(p)]) == 0
