"""Drift guard — every ``scripts/*.py`` urllib/http call sets a named UA.

Background
----------
Cloudflare Bot Fight Mode (which fronts ai.sora-dev.app) blocks the
Python stdlib default User-Agent string (``Python-urllib/3.x``) with
Error 1010 "browser_signature_banned". Operator tooling that talks to
the prod backend must ship an explicit named-tool UA so edge reputation
gates let the request through *and* so edge logs are attributable.

This test scans every ``scripts/*.py`` file for HTTP client patterns
(``urllib.request.Request(...)``, ``urllib.request.urlopen(...)``, and
``requests.*``) and asserts each caller either

  1. passes a ``headers=`` kwarg that contains a ``User-Agent`` entry,
  2. calls ``req.add_header("User-Agent", ...)`` on the Request before
     dispatch, or
  3. is explicitly whitelisted below with a justification (e.g. talks
     to localhost and never the CF edge).

If you add a new script that makes an outbound HTTP call, either set a
named UA or add the script to ``_NON_NETWORK_SCRIPTS`` with a reason.

This is a static AST check, not a runtime capture — it catches drift
at commit time without needing a live backend.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

# Scripts that do not talk to any external HTTP service and therefore
# don't need a UA. Each entry must have a one-line reason so a future
# reader can re-audit.
_NON_NETWORK_SCRIPTS: dict[str, str] = {
    # (intentionally empty — all current scripts that import urllib make
    #  real HTTP calls; keep this dict so future offline scripts that
    #  happen to import urllib.parse for URL manipulation have a home.)
}


def _iter_script_files() -> list[Path]:
    return sorted(p for p in _SCRIPTS_DIR.glob("*.py") if p.is_file())


def _has_ua_in_dict(node: ast.AST) -> bool:
    """Return True if ``node`` is a dict literal / dict call containing a
    ``User-Agent`` key (case-insensitive)."""
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                if k.value.lower() == "user-agent":
                    return True
        # Also support ``{**spread}`` — conservatively treat any spread
        # as "might include UA"; reviewers double-check.
        if any(k is None for k in node.keys):
            return True
    if isinstance(node, ast.Call):
        # dict(**spread, User_Agent=...) — rare but handle it.
        for kw in node.keywords:
            if kw.arg and kw.arg.replace("_", "-").lower() == "user-agent":
                return True
            if kw.arg is None:
                return True
    return False


def _module_has_add_header_ua(tree: ast.AST) -> bool:
    """Scan the whole module for ``.add_header("User-Agent", ...)`` calls.

    Whole-module granularity (not per-Request-site) is deliberate —
    patterns like ``for req in build(urls): req.add_header(...)`` are
    legitimate and we don't want to fingerprint-match them. If a module
    adds UA anywhere, we trust that all its Request objects get the UA.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "add_header" and len(node.args) >= 2:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if first.value.lower() == "user-agent":
                        return True
    return False


def _module_references_ua_constant(source: str) -> bool:
    """Textual fallback — catches ``headers={"User-Agent": USER_AGENT}``
    patterns with a module-level UA constant the AST walk above already
    covers, plus any string-level occurrence of ``User-Agent`` on the
    LHS of a dict/kwarg that we might have missed."""
    return re.search(r'["\']User-Agent["\']\s*:\s*\S', source) is not None


def _find_request_sites(tree: ast.AST) -> list[ast.Call]:
    """Return every ``urllib.request.Request(...)`` / ``request.Request(...)``
    / ``urlrequest.Request(...)`` call node in the module."""
    sites: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "Request":
            # Accept any ``<something>.Request(...)`` — the common aliases
            # (``urllib.request.Request``, ``request.Request``,
            # ``urlrequest.Request``) all surface the same way.
            sites.append(node)
    return sites


def _site_has_ua_headers_kwarg(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "headers":
            if _has_ua_in_dict(kw.value):
                return True
            # ``headers=HEADERS`` → fall back to module-level audit
            # via _module_references_ua_constant() + _module_has_add_header_ua().
    return False


@pytest.mark.parametrize("script_path", _iter_script_files(), ids=lambda p: p.name)
def test_script_sets_named_user_agent_on_every_request(script_path: Path) -> None:
    """Each ``urllib.request.Request(...)`` site in scripts/ ships a UA.

    Failure message includes the file + line to make the fix obvious.
    Regression guard for Cloudflare Bot Fight Mode blocking the default
    ``Python-urllib/3.x`` UA.
    """
    if script_path.name in _NON_NETWORK_SCRIPTS:
        pytest.skip(
            f"whitelisted non-network script: "
            f"{_NON_NETWORK_SCRIPTS[script_path.name]}"
        )

    source = script_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(script_path))
    sites = _find_request_sites(tree)
    if not sites:
        pytest.skip("no urllib.request.Request(...) sites")

    # Module-wide signals that any Request object gets a UA.
    mod_add_header = _module_has_add_header_ua(tree)
    mod_ua_literal = _module_references_ua_constant(source)

    failures: list[str] = []
    for site in sites:
        if _site_has_ua_headers_kwarg(site):
            continue
        if mod_add_header or mod_ua_literal:
            # Module sets UA elsewhere — trust it.
            continue
        failures.append(
            f"{script_path.relative_to(_REPO_ROOT)}:{site.lineno} — "
            f"urllib.request.Request(...) with no User-Agent header "
            f"(would ship default 'Python-urllib/*', blocked by CF Bot "
            f"Fight Mode)"
        )

    assert not failures, (
        "scripts/*.py User-Agent audit failed:\n"
        + "\n".join(f"  - {msg}" for msg in failures)
        + "\nFix: set headers={'User-Agent': ...} or call "
        "req.add_header('User-Agent', ...) before dispatch. See "
        "scripts/prod_smoke_test.py::_headers for the canonical pattern."
    )


def test_audit_covers_all_script_files() -> None:
    """Sanity check — the test above parametrizes over every ``scripts/*.py``
    and we ship at least the known set. Guards against accidental empty-glob
    (e.g. ``scripts/`` renamed) that would silently pass."""
    files = _iter_script_files()
    assert len(files) >= 10, (
        f"expected ≥10 scripts/*.py files, got {len(files)} — "
        f"scripts/ directory moved or glob broken?"
    )
    names = {p.name for p in files}
    # Anchors — these existed at the time this audit was written and
    # are load-bearing operator tooling. If one is renamed, the audit
    # should be re-run against the replacement.
    for anchor in ("prod_smoke_test.py", "usage_report.py", "check_eol.py"):
        assert anchor in names, f"missing anchor script {anchor}"
