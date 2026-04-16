"""W5 #279 — SPDX license scan for web dependencies.

Uses ``@npmcli/arborist`` (the dependency-tree loader behind ``npm``
itself) to enumerate every direct + transitive package under
``node_modules``. For each package we read the SPDX ``license`` field
from its ``package.json`` and match it against two lists:

    * ``DEFAULT_DENY_LICENSES`` — GPL / AGPL / SSPL / copyleft variants
      that contaminate the rest of the product per our licensing policy.
    * ``allowlist`` — caller-supplied SPDX expressions that override a
      deny match (for packages we've gotten explicit legal sign-off on).

The gate fails iff any package resolves to a deny-listed SPDX
expression AND is NOT on the allowlist. Packages whose license field is
missing or unparseable are collected under ``unknown_licenses`` and
reported separately — they're never auto-failed (that's a review
decision) but the report surfaces them so they don't slip through.

Fallback path
-------------
When arborist isn't installed (sandbox / fresh clone), we fall back to
walking ``node_modules`` directly and reading each ``package.json``.
This is strictly slower and misses hoisting edges that arborist
resolves, but it produces the same SPDX verdict for the majority of
trees — good enough for a compliance signal. The ``source`` field on
the report distinguishes ``arborist`` from ``walk`` so downstream
consumers know which path produced the data.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# SPDX identifiers we refuse to ship by default. The allowlist parameter
# lets a caller override specific deps (e.g. ``readline@GPL-3.0`` the
# team has cleared with legal). License matching is case-insensitive and
# tolerates "-or-later" / "-only" suffixes.
DEFAULT_DENY_LICENSES: frozenset[str] = frozenset({
    "GPL-1.0",
    "GPL-2.0",
    "GPL-3.0",
    "LGPL-2.0",
    "LGPL-2.1",
    "LGPL-3.0",
    "AGPL-1.0",
    "AGPL-3.0",
    "SSPL-1.0",
    "CC-BY-NC-1.0",
    "CC-BY-NC-2.0",
    "CC-BY-NC-3.0",
    "CC-BY-NC-4.0",
    "CC-BY-NC-SA-4.0",
    "CPAL-1.0",
    "EUPL-1.2",
    "OSL-3.0",
})


# ── Dataclasses ────────────────────────────────────────────────────

@dataclass
class PackageLicense:
    name: str
    version: str
    license: str = ""  # normalised SPDX expression or "UNKNOWN"
    path: str = ""


@dataclass
class SPDXReport:
    source: str = "mock"  # "arborist" / "walk" / "mock"
    app_path: str = ""
    total_packages: int = 0
    allowed: list[PackageLicense] = field(default_factory=list)
    denied: list[PackageLicense] = field(default_factory=list)
    unknown: list[PackageLicense] = field(default_factory=list)
    allowlist_used: list[str] = field(default_factory=list)
    deny_list: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.denied and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "app_path": self.app_path,
            "passed": self.passed,
            "total_packages": self.total_packages,
            "allowed_count": len(self.allowed),
            "denied_count": len(self.denied),
            "unknown_count": len(self.unknown),
            "denied": [asdict(p) for p in self.denied],
            "unknown": [asdict(p) for p in self.unknown],
            "allowlist_used": list(self.allowlist_used),
            "deny_list": list(self.deny_list),
            "error": self.error,
        }


# ── License normalisation ──────────────────────────────────────────

_SUFFIX_STRIP = ("-or-later", "-only", "+")


def _normalise_license(raw: Any) -> str:
    """Return the canonical SPDX id for comparison.

    npm packages can express licenses as a string, an object with
    ``type`` field, or a list of objects (deprecated SPDX expression
    form). We prefer the simplest representation and tolerate the
    others. Returns ``"UNKNOWN"`` for anything we can't parse.
    """
    if raw is None:
        return "UNKNOWN"
    if isinstance(raw, str):
        s = raw.strip()
        return s or "UNKNOWN"
    if isinstance(raw, dict):
        for key in ("spdx", "type", "id", "name"):
            if key in raw and isinstance(raw[key], str):
                return raw[key].strip() or "UNKNOWN"
        return "UNKNOWN"
    if isinstance(raw, list):
        parts = [_normalise_license(r) for r in raw]
        parts = [p for p in parts if p and p != "UNKNOWN"]
        if not parts:
            return "UNKNOWN"
        return " OR ".join(parts)
    return "UNKNOWN"


def _expand_atoms(expr: str) -> set[str]:
    """Split an SPDX expression like ``(MIT OR GPL-3.0-or-later)`` into
    the set of underlying license atoms, each stripped of the
    ``-or-later`` / ``-only`` / ``+`` suffixes so it can be matched
    against the deny/allow lists uniformly."""
    cleaned = expr.replace("(", " ").replace(")", " ")
    atoms: set[str] = set()
    for token in cleaned.split():
        t = token.strip()
        if not t:
            continue
        if t.upper() in {"AND", "OR", "WITH"}:
            continue
        for suf in _SUFFIX_STRIP:
            if t.endswith(suf):
                t = t[: -len(suf)]
                break
        atoms.add(t)
    return atoms


def _license_matches(expr: str, deny_set: set[str]) -> bool:
    """Case-insensitive atomic match against the deny list."""
    if not expr or expr == "UNKNOWN":
        return False
    deny_upper = {d.upper() for d in deny_set}
    atoms = _expand_atoms(expr)
    return any(a.upper() in deny_upper for a in atoms)


# ── arborist entry ──────────────────────────────────────────────────

_ARBORIST_SNIPPET = r"""
const { Arborist } = require('@npmcli/arborist');
(async () => {
  const arb = new Arborist({ path: process.argv[2] || process.cwd() });
  const tree = await arb.loadActual();
  const out = [];
  for (const [, node] of tree.inventory) {
    if (!node.package || node.isRoot) continue;
    out.push({
      name: node.name || '',
      version: (node.package && node.package.version) || '',
      license: node.package.license != null ? node.package.license : (node.package.licenses || ''),
      path: node.location || '',
    });
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err.message); process.exit(1); });
"""


def _run_arborist(app_path: Path, timeout: int) -> list[PackageLicense]:
    """Spawn ``node`` with the arborist snippet. Returns ``[]`` if the
    tool isn't installed or fails — caller falls back to walk()."""
    node = shutil.which("node")
    if not node:
        return []
    try:
        proc = subprocess.run(
            [node, "-e", _ARBORIST_SNIPPET, str(app_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(app_path),
        )
        if proc.returncode != 0:
            logger.info("arborist unavailable or errored: %s", proc.stderr[:200])
            return []
        payload = json.loads(proc.stdout or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("arborist run failed: %s", exc)
        return []
    except FileNotFoundError:
        return []
    result: list[PackageLicense] = []
    for entry in payload:
        result.append(
            PackageLicense(
                name=str(entry.get("name", "")),
                version=str(entry.get("version", "")),
                license=_normalise_license(entry.get("license")),
                path=str(entry.get("path", "")),
            )
        )
    return result


def _walk_node_modules(app_path: Path) -> list[PackageLicense]:
    """Fallback: walk ``node_modules`` and read each package.json."""
    nm = app_path / "node_modules"
    if not nm.is_dir():
        return []
    out: list[PackageLicense] = []
    for pkg_json in nm.rglob("package.json"):
        # skip nested ``node_modules/*/node_modules/.../tests``
        if any(p == "test" or p == "tests" or p == "__tests__"
               for p in pkg_json.parts):
            continue
        try:
            data = json.loads(pkg_json.read_text(errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        version = data.get("version")
        if not name:
            continue
        lic = data.get("license", data.get("licenses", ""))
        out.append(
            PackageLicense(
                name=str(name),
                version=str(version or ""),
                license=_normalise_license(lic),
                path=str(pkg_json.parent.relative_to(app_path)),
            )
        )
    return out


# ── Public API ────────────────────────────────────────────────────

def scan_licenses(
    app_path: Path | str,
    *,
    deny: Iterable[str] = DEFAULT_DENY_LICENSES,
    allowlist: Iterable[str] | None = None,
    timeout: int = 60,
) -> SPDXReport:
    """Enumerate the npm dependency tree under ``app_path`` and verdict
    each package against the deny list. ``allowlist`` entries are
    ``"name"`` or ``"name@license"`` strings that suppress a deny match
    for that package.

    Returns an ``SPDXReport`` whose ``.passed`` is ``False`` when any
    denied package survives the allowlist filter.
    """
    root = Path(app_path).resolve()
    report = SPDXReport(app_path=str(root))
    deny_set = set(deny)
    report.deny_list = sorted(deny_set)
    allowlist_set = {a.strip() for a in (allowlist or []) if a.strip()}
    report.allowlist_used = sorted(allowlist_set)

    if not root.is_dir():
        report.error = f"app_path '{root}' is not a directory"
        return report

    packages = _run_arborist(root, timeout=timeout)
    if packages:
        report.source = "arborist"
    else:
        packages = _walk_node_modules(root)
        if packages:
            report.source = "walk"
        else:
            report.source = "mock"

    report.total_packages = len(packages)
    for pkg in packages:
        if _license_matches(pkg.license, deny_set):
            key = pkg.name
            key_versioned = f"{pkg.name}@{pkg.license}"
            if key in allowlist_set or key_versioned in allowlist_set:
                report.allowed.append(pkg)
            else:
                report.denied.append(pkg)
        elif pkg.license == "UNKNOWN":
            report.unknown.append(pkg)
        else:
            report.allowed.append(pkg)

    return report


__all__ = [
    "DEFAULT_DENY_LICENSES",
    "SPDXReport",
    "PackageLicense",
    "scan_licenses",
]
