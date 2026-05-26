"""Family ⑦ (v2-⑦-Reproduce-401) — failing CI test + path-gating-middleware audit.

This module is the Family ⑦ *gate*: it (1) statically audits every
path-gating middleware in ``backend/main.py`` and ``backend/auth_baseline.py``
— enumerating ≥5 and discovering any 6th — and (2) reproduces the
2026-05-14 ``/health → 401`` drift. The single-source refactor (OP-1752)
landed, so the reproduction is now a HARD pass (de-xfailed in OP-1760).

Spec: ``docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md``
  * §1   — the drift-class definition this test pins down.
  * §1.1 — the forensic walk (the five allowlists × ``/health``) that
           ``test_audit_named_exempt_sets_*`` reproduces as assertions.
  * §5   — the consumer list (the five path-gating middlewares) that
           ``test_audit_*`` enumerates.
  * §7   — the drift contract / 6th-middleware discovery (here as a
           pre-fix snapshot; ``v2-⑦-ContractTest`` turns it into the
           permanent static-AST CI guard).
  * §8.1 — the transition contract (xfail-now → pass-after-fix); now
           resolved to a hard pass.

READ-ONLY MANDATE (ticket MUST NOT): this test only *reads* the
middlewares and their ``*_EXEMPT`` / ``ALLOWLIST`` sets — via static
``ast`` parsing and value extraction. It never imports-and-mutates,
never edits ``auth_baseline.py``, and never alters an exempt set. The
fix lives in ``v2-⑦-2bc``; this ticket reproduces + audits only.

The drift, in one line: the four ``main.py`` gates normalise the request
path through ``_api_relative_path`` (which strips *both* the ``/api/v1``
and ``/api/v2`` prefixes) before consulting their exempt sets, so they
correctly exempt ``/api/v2/health``. ``auth_baseline`` instead matches the
*raw* path against a hand-listed allowlist that enumerates only ``/health``
and ``/api/v1/health`` — so ``/api/v2/health`` is NOT allowlisted and, under
``OMNISIGHT_AUTH_BASELINE_MODE=enforce``, gets a 401. The OP-1131 / bc28bd65
"add ``/health``" one-liner (the Path-A band-aid that §3.1 rejected)
patched the bare-``/health`` symptom but left the ``/api/v2/*`` probe
surface drifting — which this test reproduces on live, current code.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import auth_baseline
from backend.api_versioning import api_relative_path

# ── Source-of-truth locations (audited READ-ONLY) ──────────────────────
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_MAIN_PY = _BACKEND_DIR / "main.py"
_AUTH_BASELINE_PY = _BACKEND_DIR / "auth_baseline.py"
# v2-⑦-1bc/2bc land this single-source module; its presence is the
# sentinel that tells the forensic-snapshot audit "we are now in the
# post-refactor world, the private sets are expected to be gone".
_ALLOWLIST_MODULE = _BACKEND_DIR / "middleware_allowlist.py"

# The five path-gating middlewares enumerated by the Family ⑦ spec §5.
# (Listed by callable name; file is informational.) The audit asserts
# the static scan rediscovers EXACTLY this set — a 6th surfaces as a
# diff against this baseline (spec §5 note + §7.3).
KNOWN_PATH_GATING_MIDDLEWARES = {
    ("main.py", "_rate_limit_gate"),
    ("main.py", "_must_change_password_gate"),
    ("main.py", "_graceful_shutdown_gate"),
    ("main.py", "_bootstrap_gate"),
    ("auth_baseline.py", "auth_baseline"),
}

# The five private allowlist/exempt sets named in the ticket's "Fix / files".
# `_BOOTSTRAP_EXEMPT_*` is split into its two literal members.
_MAIN_EXEMPT_SETS = (
    "_RATE_LIMIT_EXEMPT",
    "_PASSWORD_CHANGE_EXEMPT",
    "_BOOTSTRAP_EXEMPT_REL",
    "_BOOTSTRAP_EXEMPT_RAW",
    "_GRACEFUL_SHUTDOWN_EXEMPT_RAW",
)
_AUTH_BASELINE_SET = "AUTH_BASELINE_ALLOWLIST"

# Cheap process-pulse / readiness probe family. Spec §1.1 / OP-1131.
HEALTH_FAMILY = frozenset({"/health", "/healthz", "/livez", "/readyz"})


# ───────────────────────────────────────────────────────────────────────
#  Static-AST helpers (no app import; no runtime side effects — spec §7.2)
# ───────────────────────────────────────────────────────────────────────
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _iter_middleware_funcs(tree: ast.Module):
    """Yield every function decorated with ``@app.middleware("http")``.

    Walks the whole tree (not just module top level) so the
    ``auth_baseline`` middleware nested inside ``install()`` is found.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "middleware"
            ):
                yield node
                break


def _classify_middleware(node) -> tuple[bool, bool, set[str]]:
    """Return ``(path_aware, gates_by_path, allowlist_symbols)`` for a
    middleware function body.

    A middleware "gates by path" when it (a) inspects ``request.url.path``
    or normalises via ``_api_relative_path`` AND (b) makes an allow/deny
    decision driven by an allowlist: a set-membership compare (``in`` /
    ``not in``), a call to an ``*_is_exempt`` / ``*_allowed`` / ``is_public``
    helper, or a reference to an ``*_EXEMPT`` / ``*_ALLOWLIST`` name. This
    is deliberately source-of-truth-agnostic: it matches both today's
    private sets AND the post-2bc ``is_public()`` call, so the
    enumeration stays correct across the refactor.
    """
    path_aware = False
    gating = False
    symbols: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "path":
            if isinstance(n.value, ast.Attribute) and n.value.attr == "url":
                path_aware = True  # request.url.path
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            fid = n.func.id
            if fid == "_api_relative_path":
                path_aware = True
            if fid.endswith(("_is_exempt", "_allowed")) or fid == "is_public":
                gating = True
                symbols.add(fid)
        if isinstance(n, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in n.ops
        ):
            gating = True
        if isinstance(n, ast.Name) and ("EXEMPT" in n.id or n.id.endswith("ALLOWLIST")):
            symbols.add(n.id)
    return path_aware, gating, symbols


def _discover_path_gating_middlewares() -> dict[tuple[str, str], set[str]]:
    """Statically enumerate path-gating middlewares across both files.

    Returns ``{(filename, callable_name): {allowlist symbols}}``.
    """
    found: dict[tuple[str, str], set[str]] = {}
    for path in (_MAIN_PY, _AUTH_BASELINE_PY):
        tree = _parse(path)
        for fn in _iter_middleware_funcs(tree):
            path_aware, gating, symbols = _classify_middleware(fn)
            if path_aware and gating:
                found[(path.name, fn.name)] = symbols
    return found


def _module_level_literal(tree: ast.Module, name: str):
    """Return the literal value of a module-level ``name = <literal>``
    (or annotated assignment), or ``None`` if no such binding exists.

    Uses ``ast.literal_eval`` — purely read-only; the module is never
    imported or executed.
    """
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):  # pragma: no cover - defensive
                return None
    return None


# ───────────────────────────────────────────────────────────────────────
#  §5 — audit: enumerate the path-gating middlewares (≥5) + discover a 6th
# ───────────────────────────────────────────────────────────────────────
def test_audit_enumerates_at_least_five_path_gating_middlewares():
    """AC: the test enumerates ≥5 path-gating middlewares (spec §5)."""
    found = _discover_path_gating_middlewares()
    names = sorted(f"{f}:{n}" for (f, n) in found)
    assert len(found) >= 5, (
        "Family ⑦ audit expected ≥5 path-gating middlewares across "
        f"main.py + auth_baseline.py; found {len(found)}: {names}"
    )


def test_audit_discovers_no_undocumented_sixth_middleware():
    """Discovery gate (spec §5 note, §7.3): the statically rediscovered
    set of path-gating middlewares must equal the documented five.

    A 6th path-gating middleware added later (its own ``_NEW_GATE_EXEMPT``
    or an inline ``request.url.path`` allow/deny) breaks this assertion
    with its name — that is the discovery. Per spec §5.270 the new
    consumer is then added to ``v2-⑦-2bc``'s refactor list. Removing one
    of the five also trips here.
    """
    found = set(_discover_path_gating_middlewares())
    extra = found - KNOWN_PATH_GATING_MIDDLEWARES
    missing = KNOWN_PATH_GATING_MIDDLEWARES - found
    assert not extra, (
        "Discovered a path-gating middleware not on the Family ⑦ "
        f"consumer list (spec §5): {sorted(extra)}. Add it to "
        "v2-⑦-2bc's is_public() refactor and to "
        "KNOWN_PATH_GATING_MIDDLEWARES."
    )
    assert not missing, (
        f"A documented path-gating middleware vanished: {sorted(missing)}. "
        "If it was intentionally removed, update the baseline."
    )


def test_audit_named_exempt_sets_present_and_exempt_health():
    """Forensic walk of spec §1.1: every named allowlist/exempt set
    listed in the ticket exempts the ``/health`` probe family.

    Snapshot of the PRE-fix world. Once ``v2-⑦-2bc`` deletes the private
    sets (spec §5.2) and lands ``backend/middleware_allowlist.py``, this
    forensic snapshot no longer applies and is superseded by
    ``v2-⑦-ContractTest`` (§7); the sentinel branch below keeps this
    test green across that transition instead of fragmenting the signal.
    """
    main_tree = _parse(_MAIN_PY)
    auth_tree = _parse(_AUTH_BASELINE_PY)
    post_refactor = _ALLOWLIST_MODULE.exists()

    def _members(value) -> set[str]:
        return set(value) if value is not None else set()

    audited: dict[str, set[str]] = {}
    for name in _MAIN_EXEMPT_SETS:
        audited[name] = _members(_module_level_literal(main_tree, name))
    audited[_AUTH_BASELINE_SET] = _members(
        _module_level_literal(auth_tree, _AUTH_BASELINE_SET)
    )

    if not post_refactor:
        # Pre-fix: all five named sets must exist (non-empty) — this is
        # the audit the ticket's "Fix / files" calls for.
        absent = [name for name, members in audited.items() if not members]
        assert not absent, (
            "Expected these named allowlist/exempt sets to be present "
            f"and non-empty in the pre-refactor source: {absent}"
        )

    # Every set that still exists must exempt at least one health-family
    # probe path (spec §1.1 forensic table: all five show ✅ for /health).
    for name, members in audited.items():
        if not members:
            continue  # migrated to is_public() post-2bc — out of scope here
        assert members & HEALTH_FAMILY, (
            f"{name} exempts no health-family probe path "
            f"({sorted(HEALTH_FAMILY)}); the §1.1 forensic invariant is "
            "violated."
        )


def test_audit_auth_baseline_allowlist_is_the_lone_v2_disagreer():
    """The residual drift, pinned: ``AUTH_BASELINE_ALLOWLIST`` enumerates
    bare ``/health`` and ``/api/v1/health`` but NOT ``/api/v2/health``.

    This is the exact gap the OP-1131 / bc28bd65 Path-A band-aid left
    open (spec §3.1): it added ``/health`` (fixing the bare-path 2026-05-14
    symptom) but never the ``/api/v2`` probe surface. Read-only AST
    extraction; skipped once the allowlist has migrated out (post-2bc).
    """
    allowlist = _module_level_literal(_parse(_AUTH_BASELINE_PY), _AUTH_BASELINE_SET)
    if allowlist is None:
        pytest.skip(
            f"{_AUTH_BASELINE_SET} no longer a literal in auth_baseline.py "
            "— migrated to middleware_allowlist.py (post v2-⑦-2bc)."
        )
    members = set(allowlist)
    assert "/health" in members  # bc28bd65 band-aid present
    assert "/api/v1/health" in members
    assert "/api/v2/health" not in members, (
        "If /api/v2/health is now in AUTH_BASELINE_ALLOWLIST the residual "
        "drift was patched here directly — confirm this is the intended "
        "v2-⑦ fix and update this characterization test."
    )


# ───────────────────────────────────────────────────────────────────────
#  Drift mechanism — stable facts (pass now AND after 2bc)
# ───────────────────────────────────────────────────────────────────────
def test_v2_health_normalizes_to_public_health_path():
    """``_api_relative_path`` (the normaliser the four main.py gates use)
    strips the ``/api/v2`` prefix, so ``/api/v2/health`` resolves to the
    public ``/health`` — and every main.py exempt set already contains
    that normalised form. auth_baseline is therefore the *lone* gate that
    would 401 the v2 probe: the four others would let it through.

    Asserts only api-versioning + main.py facts (neither touched by
    Family ⑦), so it is stable across the 2bc refactor.
    """
    for prefix in ("/api/v1", "/api/v2"):
        assert api_relative_path(f"{prefix}/health", "/api/v1") == "/health"

    main_tree = _parse(_MAIN_PY)
    # The four main.py gates normalise then test membership; each set
    # contains the normalised "/health", so /api/v2/health is exempt there.
    for name in ("_RATE_LIMIT_EXEMPT", "_GRACEFUL_SHUTDOWN_EXEMPT_RAW"):
        members = _module_level_literal(main_tree, name)
        if members is None:
            continue
        assert "/health" in set(members), f"{name} should exempt /health"


# ───────────────────────────────────────────────────────────────────────
#  §8.1 — the 401 reproduction: now a HARD pass (fix landed via OP-1752,
#  marker de-xfailed in OP-1760)
# ───────────────────────────────────────────────────────────────────────
def _make_enforce_app() -> FastAPI:
    """A bare app whose ONLY middleware is ``auth_baseline`` — so any 401
    is unambiguously attributable to the allowlist drift. Mirrors
    ``include_versioned_router`` by mounting the liveness payload under
    both ``/api/v1`` and ``/api/v2`` (plus bare ``/health``).
    """
    app = FastAPI()

    async def _live():
        return {"status": "online"}

    for path in ("/health", "/api/v1/health", "/api/v2/health"):
        app.add_api_route(path, _live, methods=["GET"])

    auth_baseline.install(app)
    return app


async def test_v2_health_401_drift_reproduction(monkeypatch):
    """Reproduces the 2026-05-14 ``/health → 401`` drift class on the live
    ``/api/v2/health`` probe surface.

    Asserts the *correct* (post-fix) behaviour: an unauthenticated probe
    of a health endpoint under ``enforce`` mode must NOT be rejected with
    401. The fix landed via OP-1752 — ``auth_baseline`` now routes through
    the single ``is_public()`` source, which normalises the ``/api/v2``
    prefix → 200. This is a HARD pass (the ``xfail`` marker was removed in
    OP-1760 once the reproduction reliably xpassed on develop-tip).
    """
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    # Keep the gate hermetic: no DB. The middleware consults the umbrella
    # _has_valid_session (the documented monkeypatch hook); force "no
    # session" so the only thing standing between request and handler is
    # the allowlist decision.
    async def _no_session(_req):
        return False

    monkeypatch.setattr(auth_baseline, "_has_valid_session", _no_session)

    app = _make_enforce_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v2/health")

    assert resp.status_code != 401, (
        "/api/v2/health was 401'd under enforce mode — the Family ⑦ "
        "allowlist drift. Expected the probe to be treated as public "
        f"(got {resp.status_code}; detail={resp.text!r})."
    )
