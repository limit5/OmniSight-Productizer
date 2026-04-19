#!/usr/bin/env python3
"""check_auth_coverage.py — static audit of FastAPI route-level auth.

Walks `backend/routers/*.py`, parses every `@router.<verb>(...)` handler,
and classifies it by whether it has auth. Three categories:

    AUTH       — handler signature has a `Depends(...)` or the enclosing
                 APIRouter(dependencies=[Depends(...)]) has one
    ALLOWLIST  — path prefix matches `AUTH_BASELINE_ALLOWLIST` in
                 backend/auth_baseline.py (public on purpose)
    UNGATED    — neither → security risk, should be fixed

Usage:
    scripts/check_auth_coverage.py                # print full report
    scripts/check_auth_coverage.py --check        # exit 1 if any UNGATED
    scripts/check_auth_coverage.py --md OUT.md    # write a markdown report
    scripts/check_auth_coverage.py --json         # machine output

Intended uses:
  * One-shot audit report (generate the initial classification the
    S2-9 allowlist needs).
  * CI gate (`--check`): any new handler without auth AND whose path
    isn't on the allowlist fails the build. Paired with code review,
    this prevents the "accidentally-anonymous endpoint" regression.

This script is intentionally AST-only: it never imports the routers
themselves. Avoids pulling in DB / LLM / docker side-effects, and
makes the check faster + more reliable in CI.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# --- Resolve repo root ---------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
ROUTERS_DIR = _REPO / "backend" / "routers"

# --- Pull the allowlist straight from the middleware module, so we can't
#     drift. Loaded via AST parse (no import) to stay side-effect-free.
def _load_allowlist() -> tuple[str, ...]:
    src = (_REPO / "backend" / "auth_baseline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
           and node.target.id == "AUTH_BASELINE_ALLOWLIST":
            if isinstance(node.value, ast.Tuple):
                out: list[str] = []
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        out.append(elt.value)
                return tuple(out)
    raise RuntimeError("could not find AUTH_BASELINE_ALLOWLIST in backend/auth_baseline.py")


ALLOWLIST = _load_allowlist()

# --- Data model ----------------------------------------------------------

@dataclass
class Handler:
    router_file: str        # e.g. "backend/routers/system.py"
    verb: str               # "get" | "post" | ...
    path: str               # e.g. "/system/debug"
    full_path: str          # prefix + path, e.g. "/api/v1/system/debug"
    line: int
    has_auth: bool          # handler-level Depends on an auth-looking dep
    router_has_auth: bool   # APIRouter(dependencies=[...]) present on the router
    allowlisted: bool

    @property
    def category(self) -> str:
        if self.has_auth or self.router_has_auth:
            return "AUTH"
        if self.allowlisted:
            return "ALLOWLIST"
        return "UNGATED"


@dataclass
class FileAudit:
    path: str
    prefix: str
    router_deps: list[str] = field(default_factory=list)  # names of deps at router level
    handlers: list[Handler] = field(default_factory=list)


# --- AST inspection ------------------------------------------------------

_AUTH_FN_HINTS = (
    "current_user", "require_role", "require_viewer", "require_operator",
    "require_admin", "require_tenant", "check_llm_quota",
    # CSRF dep is auth-adjacent but not auth alone; do not include here
)


def _name_or_attr(n: ast.expr) -> str:
    """Flatten `foo.bar.baz` into string for matching."""
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return f"{_name_or_attr(n.value)}.{n.attr}"
    if isinstance(n, ast.Call):
        return _name_or_attr(n.func)
    return ""


def _is_auth_dep(call: ast.Call) -> bool:
    """Return True if the Call node looks like `Depends(<auth-fn>)`."""
    name = _name_or_attr(call)
    if not (name == "Depends" or name.endswith(".Depends")):
        return False
    if not call.args:
        return False
    arg0 = call.args[0]
    dep_name = _name_or_attr(arg0)
    tail = dep_name.rsplit(".", 1)[-1]
    return any(tail == h or tail.startswith(h + "(") for h in _AUTH_FN_HINTS)


def _default_is_auth_depends(default: ast.expr | None) -> bool:
    return isinstance(default, ast.Call) and _is_auth_dep(default)


def _router_prefix(tree: ast.Module) -> tuple[str, list[str]]:
    """Scan for `router = APIRouter(prefix="/xxx", dependencies=[...])`."""
    prefix = ""
    router_deps: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "router"):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            if not _name_or_attr(call).endswith("APIRouter"):
                continue
            for kw in call.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = kw.value.value or ""
                elif kw.arg == "dependencies" and isinstance(kw.value, ast.List):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Call) and _is_auth_dep(elt):
                            router_deps.append(_name_or_attr(elt))
    return prefix, router_deps


def _decorator_verb_path(dec: ast.expr) -> tuple[str, str] | None:
    """If dec is @router.VERB(path, ...), return (verb, path). Else None."""
    if not isinstance(dec, ast.Call):
        return None
    name = _name_or_attr(dec.func)
    if not name.startswith("router."):
        return None
    verb = name.split(".", 1)[1]
    if verb not in {"get", "post", "put", "patch", "delete"}:
        return None
    path = ""
    if dec.args and isinstance(dec.args[0], ast.Constant):
        path = dec.args[0].value or ""
    return verb, path


def _handler_has_auth_dep(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """True if any parameter default is `Depends(<auth-fn>)`, OR if the
    decorator has `dependencies=[Depends(<auth-fn>)]`."""
    # Parameter defaults
    args = fn.args
    all_defaults = list(args.defaults) + list(args.kw_defaults or [])
    for d in all_defaults:
        if _default_is_auth_depends(d):
            return True
    # Decorator-level dependencies=[...]
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "dependencies" and isinstance(kw.value, ast.List):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Call) and _is_auth_dep(elt):
                            return True
                # Support @router.get(..., dependencies=_REQUIRE_ADMIN) — the
                # name reference case. We know `_REQUIRE_ADMIN` in system.py
                # holds [Depends(require_role("admin"))]. Match any variable
                # name that looks like an auth-deps list by convention.
                if kw.arg == "dependencies" and isinstance(kw.value, ast.Name):
                    if "ADMIN" in kw.value.id.upper() or "AUTH" in kw.value.id.upper() \
                       or "REQUIRE" in kw.value.id.upper():
                        return True
    return False


def audit_file(path: Path) -> FileAudit:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    prefix, router_deps = _router_prefix(tree)
    audit = FileAudit(
        path=str(path.relative_to(_REPO)),
        prefix=prefix,
        router_deps=router_deps,
    )
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            vp = _decorator_verb_path(dec)
            if not vp:
                continue
            verb, rel_path = vp
            full = f"/api/v1{prefix}{rel_path}" if not prefix.startswith("/api") else f"{prefix}{rel_path}"
            # system's prefix is literally `/system`, so the full path should
            # be `/api/v1/system/...`. Routers with no prefix currently mount
            # at /api/v1 directly via main.py's include_router prefix.
            has_auth = _handler_has_auth_dep(node)
            allowlisted = any(full.startswith(ap) for ap in ALLOWLIST)
            audit.handlers.append(Handler(
                router_file=audit.path,
                verb=verb,
                path=rel_path,
                full_path=full,
                line=dec.lineno,
                has_auth=has_auth,
                router_has_auth=bool(router_deps),
                allowlisted=allowlisted,
            ))
            break  # one handler per def
    return audit


def walk_routers() -> Iterator[FileAudit]:
    for p in sorted(ROUTERS_DIR.glob("*.py")):
        if p.name in {"__init__.py", "_pagination.py"}:
            continue
        try:
            yield audit_file(p)
        except Exception as exc:
            print(f"WARN: failed to audit {p}: {exc}", file=sys.stderr)


# --- Report formatting ---------------------------------------------------

def _counts(audits: Iterable[FileAudit]) -> dict:
    total = 0
    by_cat: dict[str, int] = {"AUTH": 0, "ALLOWLIST": 0, "UNGATED": 0}
    for a in audits:
        for h in a.handlers:
            total += 1
            by_cat[h.category] += 1
    return {"total": total, **by_cat}


def render_markdown(audits: list[FileAudit]) -> str:
    counts = _counts(audits)
    lines = [
        "# Auth Coverage Audit",
        "",
        f"Generated by `scripts/check_auth_coverage.py` against HEAD.",
        "",
        f"- Total handlers: **{counts['total']}**",
        f"- With auth (handler Depends OR router-level dependencies): **{counts['AUTH']}**",
        f"- On the allowlist (public on purpose): **{counts['ALLOWLIST']}**",
        f"- UNGATED (no auth, not allowlisted): **{counts['UNGATED']}** ← fix these",
        "",
        "## UNGATED handlers (fix list)",
        "",
        "| Verb | Path | File:Line |",
        "|---|---|---|",
    ]
    ungated_any = False
    for a in audits:
        for h in a.handlers:
            if h.category != "UNGATED":
                continue
            ungated_any = True
            lines.append(
                f"| {h.verb.upper()} | `{h.full_path}` | `{h.router_file}:{h.line}` |"
            )
    if not ungated_any:
        lines.append("| — | _(none)_ | — |")
    lines.append("")
    lines.append("## Per-router summary")
    lines.append("")
    lines.append("| Router | Prefix | Handlers | AUTH | ALLOWLIST | UNGATED |")
    lines.append("|---|---|---|---|---|---|")
    for a in audits:
        cats = {"AUTH": 0, "ALLOWLIST": 0, "UNGATED": 0}
        for h in a.handlers:
            cats[h.category] += 1
        lines.append(
            f"| `{a.path.split('/')[-1][:-3]}` | `{a.prefix}` | "
            f"{len(a.handlers)} | {cats['AUTH']} | {cats['ALLOWLIST']} | "
            f"{cats['UNGATED']} |"
        )
    return "\n".join(lines) + "\n"


def render_json(audits: list[FileAudit]) -> str:
    out = {
        "counts": _counts(audits),
        "ungated": [
            {
                "verb": h.verb, "path": h.full_path,
                "file": h.router_file, "line": h.line,
            }
            for a in audits for h in a.handlers
            if h.category == "UNGATED"
        ],
    }
    return json.dumps(out, indent=2, ensure_ascii=False)


def render_text(audits: list[FileAudit]) -> str:
    counts = _counts(audits)
    lines = [
        "Auth Coverage Audit",
        "===================",
        f"Total:    {counts['total']}",
        f"AUTH:     {counts['AUTH']}",
        f"ALLOWED:  {counts['ALLOWLIST']}",
        f"UNGATED:  {counts['UNGATED']}  ← fix these",
        "",
        "UNGATED handlers:",
    ]
    for a in audits:
        for h in a.handlers:
            if h.category == "UNGATED":
                lines.append(f"  {h.verb.upper():6} {h.full_path:50} {h.router_file}:{h.line}")
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true",
                   help="exit 1 if any UNGATED handlers exist (for CI)")
    p.add_argument("--md", metavar="OUT",
                   help="write markdown report to OUT")
    p.add_argument("--json", action="store_true",
                   help="emit JSON report instead of plain text")
    args = p.parse_args()

    audits = list(walk_routers())

    if args.md:
        Path(args.md).write_text(render_markdown(audits), encoding="utf-8")
        print(f"wrote {args.md}")
    elif args.json:
        print(render_json(audits))
    else:
        print(render_text(audits))

    if args.check:
        ungated = _counts(audits)["UNGATED"]
        if ungated:
            print(f"\n[check] {ungated} UNGATED handlers — fail")
            return 1
        print("\n[check] OK — every handler has auth or is on the allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
