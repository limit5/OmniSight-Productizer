"""S2-9 (#354) — tests for scripts/check_auth_coverage.py.

The auditor is a CI gate AND a one-shot reporting tool, so it has to
be trustworthy:

  * classification (AUTH / ALLOWLIST / UNGATED) must be correct on
    the common router shapes we ship today
  * the allowlist loader must not silently lose entries if someone
    refactors backend/auth_baseline.py
  * baseline file round-trip (--update-baseline → --check-baseline)
    must be an identity

We exercise the classifier with tiny fixture routers written to
tmp_path, not by calling the full CLI. `walk_routers()` hardcodes
the real `backend/routers/` directory, but `audit_file(Path)` is
a pure function we can point at any file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# sys.path shim — the script lives under `scripts/`, not an installed
# package. Import by file path.
_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_auth_coverage.py"

import importlib.util
_spec = importlib.util.spec_from_file_location("check_auth_coverage", _SCRIPT)
assert _spec and _spec.loader
cac = importlib.util.module_from_spec(_spec)
# Register before exec so `@dataclass` can resolve cls.__module__ during
# class construction (dataclasses peek at sys.modules[cls.__module__]).
sys.modules["check_auth_coverage"] = cac
_spec.loader.exec_module(cac)


# ─── allowlist loader ──────────────────────────────────────────────


def test_allowlist_loader_nonempty_and_matches_module():
    """The AST loader in the script must return the same tuple as
    `from backend.auth_baseline import AUTH_BASELINE_ALLOWLIST`."""
    from backend.auth_baseline import AUTH_BASELINE_ALLOWLIST

    ast_loaded = cac._load_allowlist()
    assert isinstance(ast_loaded, tuple)
    assert len(ast_loaded) >= 10, "suspiciously small — loader bug?"
    assert set(ast_loaded) == set(AUTH_BASELINE_ALLOWLIST)


# ─── classifier on fixture routers ─────────────────────────────────


def _write_router(tmp_path: Path, name: str, body: str,
                  monkeypatch=None) -> Path:
    """Write a fixture router file into tmp_path and point the script's
    `_REPO` anchor at tmp_path so `audit_file`'s `relative_to(_REPO)`
    succeeds."""
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setattr(cac, "_REPO", tmp_path)
    return p


def test_classifier_flags_no_auth_handler_as_ungated(tmp_path, monkeypatch):
    body = '''\
from fastapi import APIRouter
router = APIRouter(prefix="/fake")

@router.get("/list")
async def list_items():
    return []
'''
    p = _write_router(tmp_path, "fake.py", body, monkeypatch)
    audit = cac.audit_file(p)
    assert len(audit.handlers) == 1
    h = audit.handlers[0]
    assert h.full_path == "/api/v1/fake/list"
    assert h.category == "UNGATED"


def test_classifier_detects_handler_level_depends_current_user(tmp_path, monkeypatch):
    body = '''\
from fastapi import APIRouter, Depends
from backend import auth
router = APIRouter(prefix="/fake")

@router.get("/me")
async def me(user=Depends(auth.current_user)):
    return {"id": user.id}
'''
    p = _write_router(tmp_path, "fake.py", body, monkeypatch)
    h = cac.audit_file(p).handlers[0]
    assert h.category == "AUTH"


def test_classifier_detects_router_level_dependencies(tmp_path, monkeypatch):
    body = '''\
from fastapi import APIRouter, Depends
from backend import auth
router = APIRouter(prefix="/fake", dependencies=[Depends(auth.require_admin)])

@router.get("/write")
async def write():
    return {}
'''
    p = _write_router(tmp_path, "fake.py", body, monkeypatch)
    h = cac.audit_file(p).handlers[0]
    assert h.category == "AUTH"


def test_classifier_detects_per_endpoint_deps_list_name(tmp_path, monkeypatch):
    """system.py uses `@router.get(..., dependencies=_REQUIRE_ADMIN)`.
    The name-reference heuristic (`ADMIN`/`AUTH`/`REQUIRE` in the var
    name) must keep classifying that as AUTH."""
    body = '''\
from fastapi import APIRouter, Depends
from backend import auth
_REQUIRE_ADMIN = [Depends(auth.require_role("admin"))]
router = APIRouter(prefix="/fake")

@router.post("/admin", dependencies=_REQUIRE_ADMIN)
async def admin_op():
    return {}
'''
    p = _write_router(tmp_path, "fake.py", body, monkeypatch)
    h = cac.audit_file(p).handlers[0]
    assert h.category == "AUTH"


def test_classifier_marks_allowlisted_path_as_allowlist(tmp_path, monkeypatch):
    """A handler on `/api/v1/auth/login` is allowlisted; even without
    a Depends it should classify as ALLOWLIST, not UNGATED."""
    body = '''\
from fastapi import APIRouter
router = APIRouter(prefix="/auth")

@router.post("/login")
async def login():
    return {"ok": True}
'''
    p = _write_router(tmp_path, "auth_fake.py", body, monkeypatch)
    h = cac.audit_file(p).handlers[0]
    assert h.full_path == "/api/v1/auth/login"
    assert h.category == "ALLOWLIST"


def test_classifier_ignores_non_router_functions(tmp_path, monkeypatch):
    body = '''\
from fastapi import APIRouter
router = APIRouter(prefix="/fake")

def helper():
    return 1

@router.get("/ping")
async def ping():
    return "pong"
'''
    p = _write_router(tmp_path, "fake.py", body, monkeypatch)
    handlers = cac.audit_file(p).handlers
    assert len(handlers) == 1
    assert handlers[0].path == "/ping"


# ─── baseline round-trip via CLI ────────────────────────────────────


def test_baseline_update_then_check_is_identity(tmp_path):
    """Running --update-baseline and then --check-baseline on the
    generated file must pass — no diff, exit 0."""
    out = tmp_path / "baseline.txt"
    up = subprocess.run(
        [sys.executable, str(_SCRIPT), "--update-baseline", str(out)],
        check=False, capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stderr
    assert out.exists()

    chk = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check-baseline", str(out)],
        check=False, capture_output=True, text=True,
    )
    assert chk.returncode == 0, chk.stderr + "\n---\n" + chk.stdout
    assert "UNGATED set matches baseline" in chk.stdout


def test_baseline_check_fails_when_line_removed(tmp_path):
    """If the baseline is missing a currently-UNGATED line, that means
    a handler appeared without auth — CI must fail."""
    out = tmp_path / "baseline.txt"
    subprocess.run(
        [sys.executable, str(_SCRIPT), "--update-baseline", str(out)],
        check=True, capture_output=True, text=True,
    )
    lines = out.read_text().splitlines()
    # Drop the first non-comment line — simulate a handler added after
    # the baseline was committed.
    kept: list[str] = []
    dropped = False
    for ln in lines:
        if not dropped and ln and not ln.startswith("#"):
            dropped = True
            continue
        kept.append(ln)
    out.write_text("\n".join(kept) + "\n")

    chk = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check-baseline", str(out)],
        check=False, capture_output=True, text=True,
    )
    assert chk.returncode == 1
    assert "NEW UNGATED handler" in chk.stdout


def test_baseline_check_tolerates_resolved_handler(tmp_path):
    """If a handler on the baseline now has auth (removed from UNGATED),
    the check must NOT fail — it only warns 'consider refreshing'."""
    out = tmp_path / "baseline.txt"
    subprocess.run(
        [sys.executable, str(_SCRIPT), "--update-baseline", str(out)],
        check=True, capture_output=True, text=True,
    )
    # Append a fake line so the baseline has a "resolved" entry that
    # doesn't exist live. `set - set` will report this as "moved off
    # the UNGATED list" and should NOT fail.
    with out.open("a", encoding="utf-8") as f:
        f.write("GET /api/v1/does_not_exist_but_once_was\n")

    chk = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check-baseline", str(out)],
        check=False, capture_output=True, text=True,
    )
    assert chk.returncode == 0, chk.stdout
    assert "moved off the UNGATED list" in chk.stdout
