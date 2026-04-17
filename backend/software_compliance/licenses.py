"""X4 #300 — Multi-ecosystem SPDX license scan.

Parallel to ``backend/web_compliance/spdx.py`` (which is npm-only) but
covers every ecosystem an X-series software skill can ship:

    ecosystem  │ preferred CLI                                 │ fallback
    ───────────┼───────────────────────────────────────────────┼────────────────────────
    cargo      │ ``cargo-license`` (JSON)                      │ walk ``Cargo.lock``
    go         │ ``go-licenses report`` (CSV)                  │ parse ``go.mod`` (*)
    pip        │ ``pip-licenses`` (JSON)                       │ walk site-packages
    npm        │ ``license-checker`` (JSON)                    │ walk ``node_modules``
    maven      │ ``mvn license:aggregate-download-licenses``   │ parse ``pom.xml`` (*)

Each adapter is **optional** — missing tools make the adapter return
``source="mock"`` with no packages (caller treats as ``skip``).

The public ``scan_licenses()`` auto-detects the ecosystem from marker
files (``Cargo.toml`` / ``go.mod`` / ``requirements.txt`` / ``pyproject``
/ ``package.json`` / ``pom.xml`` / ``build.gradle.kts``) or takes an
explicit ecosystem hint.

Denylist / allowlist semantics match W5 ``web_compliance/spdx.py`` so
downstream audit consumers can compare bundle rows across verticals.

(*) ``go.mod`` doesn't record licenses natively — the fallback only
lists module ids with ``license="UNKNOWN"``. Same for ``pom.xml`` /
``build.gradle.kts``: the fallback lists groupId/artifactId/version
as ``UNKNOWN`` rows so the gate still reports them under ``unknown``
rather than failing them blind.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# SPDX identifiers we refuse to ship by default. Same rationale as
# web_compliance.spdx.DEFAULT_DENY_LICENSES — kept in sync by design so
# audit consumers can join the two bundles on license id.
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


ECOSYSTEMS: tuple[str, ...] = ("cargo", "go", "pip", "npm", "maven")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PackageLicense:
    """One dependency resolved to its SPDX license expression."""

    name: str
    version: str
    license: str = ""
    ecosystem: str = ""
    path: str = ""


@dataclass
class LicenseReport:
    """Per-ecosystem scan result."""

    ecosystem: str = ""
    source: str = "mock"  # "cargo-license" / "go-licenses" / "pip-licenses" / "license-checker" / "walk" / "mock"
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
            "ecosystem": self.ecosystem,
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SPDX normalisation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SUFFIX_STRIP = ("-or-later", "-only", "+")


def _normalise_license(raw: Any) -> str:
    """Coerce whatever a package manifest gives us into a comparable
    SPDX expression. Accepts str / dict / list / None. Unknown shapes
    collapse to ``"UNKNOWN"`` so the gate can never silently pass a
    package whose license we failed to parse."""
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
    """Split an SPDX expression into underlying license atoms for
    set-membership comparisons against deny/allow lists."""
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
    if not expr or expr == "UNKNOWN":
        return False
    deny_upper = {d.upper() for d in deny_set}
    atoms = _expand_atoms(expr)
    return any(a.upper() in deny_upper for a in atoms)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ecosystem detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MARKER_FILES: dict[str, tuple[str, ...]] = {
    "cargo": ("Cargo.toml",),
    "go": ("go.mod",),
    "pip": ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"),
    "npm": ("package.json",),
    "maven": ("pom.xml", "build.gradle.kts", "build.gradle"),
}


def detect_ecosystem(app_path: Path) -> Optional[str]:
    """Return the first ecosystem whose marker file exists in ``app_path``.
    Precedence: cargo > go > pip > npm. ``None`` when nothing matches."""
    for eco, markers in _MARKER_FILES.items():
        for m in markers:
            if (app_path / m).exists():
                return eco
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Subprocess helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_tool(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timeout after {timeout}s: {exc}"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  cargo ecosystem
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scan_cargo(app_path: Path, timeout: int) -> tuple[list[PackageLicense], str]:
    """Try ``cargo-license --json`` first; fall back to walking Cargo.lock."""
    if shutil.which("cargo-license"):
        rc, out, err = _run_tool(
            ["cargo-license", "--json"], cwd=app_path, timeout=timeout
        )
        if rc == 0 and out.strip():
            try:
                payload = json.loads(out)
            except json.JSONDecodeError as exc:
                logger.info("cargo-license JSON parse failed: %s", exc)
                payload = None
            if isinstance(payload, list):
                pkgs: list[PackageLicense] = []
                for e in payload:
                    pkgs.append(
                        PackageLicense(
                            name=str(e.get("name", "")),
                            version=str(e.get("version", "")),
                            license=_normalise_license(e.get("license")),
                            ecosystem="cargo",
                        )
                    )
                return pkgs, "cargo-license"
        else:
            logger.info("cargo-license failed rc=%s err=%s", rc, err[:200])

    lock = app_path / "Cargo.lock"
    if lock.exists():
        return _parse_cargo_lock(lock), "walk"
    return [], "mock"


_CARGO_PKG_BLOCK_RE = re.compile(
    r"\[\[package\]\]\s*\n"
    r"((?:(?!\[\[).*\n)+)",
    re.MULTILINE,
)
_CARGO_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=\s*\"([^\"]*)\"", re.MULTILINE)


def _parse_cargo_lock(lock_path: Path) -> list[PackageLicense]:
    """Minimal Cargo.lock parser — we only need name/version. License is
    not recorded in the lockfile so every row ends up as ``UNKNOWN``.
    Real license data requires ``cargo-license`` or crate-metadata fetch.
    """
    try:
        text = lock_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out: list[PackageLicense] = []
    for block in _CARGO_PKG_BLOCK_RE.findall(text):
        kv = dict(_CARGO_KV_RE.findall(block))
        name = kv.get("name", "")
        version = kv.get("version", "")
        if not name:
            continue
        out.append(
            PackageLicense(
                name=name,
                version=version,
                license="UNKNOWN",
                ecosystem="cargo",
            )
        )
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  go ecosystem
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scan_go(app_path: Path, timeout: int) -> tuple[list[PackageLicense], str]:
    if shutil.which("go-licenses"):
        rc, out, err = _run_tool(
            ["go-licenses", "report", "./...", "--template={{.Name}},{{.Version}},{{.LicenseName}}\n"],
            cwd=app_path,
            timeout=timeout,
        )
        if rc == 0 and out.strip():
            pkgs: list[PackageLicense] = []
            reader = csv.reader(io.StringIO(out))
            for row in reader:
                if len(row) < 3:
                    continue
                name, version, lic = row[0].strip(), row[1].strip(), row[2].strip()
                if not name:
                    continue
                pkgs.append(
                    PackageLicense(
                        name=name,
                        version=version,
                        license=_normalise_license(lic),
                        ecosystem="go",
                    )
                )
            if pkgs:
                return pkgs, "go-licenses"

    mod = app_path / "go.mod"
    if mod.exists():
        return _parse_go_mod(mod), "walk"
    return [], "mock"


_GO_REQUIRE_RE = re.compile(
    r"^\s*(?:require\s+)?([^\s]+)\s+(v[0-9][^\s]*)", re.MULTILINE
)


def _parse_go_mod(mod_path: Path) -> list[PackageLicense]:
    """Extract ``require`` lines from go.mod. No license info is
    available — every row is ``UNKNOWN`` until go-licenses runs."""
    try:
        text = mod_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[PackageLicense] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            in_block = True
            continue
        if stripped == ")" and in_block:
            in_block = False
            continue
        if stripped.startswith("//") or not stripped:
            continue
        if stripped.startswith("module ") or stripped.startswith("go ") or \
           stripped.startswith("toolchain "):
            continue
        # Block or single-line form: "<module> <version>" or
        # "require <module> <version>"
        m = _GO_REQUIRE_RE.match(line)
        if not m:
            continue
        mod, ver = m.group(1), m.group(2)
        key = (mod, ver)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PackageLicense(
                name=mod,
                version=ver,
                license="UNKNOWN",
                ecosystem="go",
            )
        )
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  pip ecosystem
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scan_pip(app_path: Path, timeout: int) -> tuple[list[PackageLicense], str]:
    if shutil.which("pip-licenses"):
        rc, out, _ = _run_tool(
            ["pip-licenses", "--format=json", "--with-system"],
            cwd=app_path,
            timeout=timeout,
        )
        if rc == 0 and out.strip():
            try:
                payload = json.loads(out)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                pkgs: list[PackageLicense] = []
                for e in payload:
                    pkgs.append(
                        PackageLicense(
                            name=str(e.get("Name", e.get("name", ""))),
                            version=str(e.get("Version", e.get("version", ""))),
                            license=_normalise_license(
                                e.get("License") or e.get("license")
                            ),
                            ecosystem="pip",
                        )
                    )
                return pkgs, "pip-licenses"

    for marker in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"):
        path = app_path / marker
        if path.exists():
            return _parse_pip_manifest(path), "walk"
    return [], "mock"


_REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]+\])?\s*(?:[<>=!~]{1,2}\s*([A-Za-z0-9_.\-+*]+))?",
)
_PYPROJECT_DEP_RE = re.compile(
    r"[\"']([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]+\])?\s*(?:[<>=!~]{1,2}\s*([A-Za-z0-9_.\-+*]+))?[\"']"
)


def _parse_pip_manifest(path: Path) -> list[PackageLicense]:
    """Extract dependency names (and versions when pinned) from
    requirements.txt / pyproject.toml / setup.py / setup.cfg. License
    is UNKNOWN for every row — only pip-licenses (which inspects the
    installed environment) has that data."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    pkgs: dict[str, PackageLicense] = {}
    if path.name == "requirements.txt":
        for line in text.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            m = _REQ_LINE_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            ver = m.group(2) or ""
            pkgs[name] = PackageLicense(
                name=name, version=ver, license="UNKNOWN", ecosystem="pip"
            )
    elif path.name == "pyproject.toml":
        # crude regex extraction — works for PEP 621 and Poetry styles.
        for m in _PYPROJECT_DEP_RE.finditer(text):
            name = m.group(1)
            if name.lower() in {"python"}:
                continue
            ver = m.group(2) or ""
            if name not in pkgs:
                pkgs[name] = PackageLicense(
                    name=name, version=ver, license="UNKNOWN", ecosystem="pip"
                )
    else:
        # setup.py / setup.cfg — pull install_requires lines.
        for m in re.finditer(
            r"[\"']([A-Za-z0-9_.\-]+)[\"']", text
        ):
            name = m.group(1)
            if len(name) < 2 or "." in name and "_" not in name and "-" not in name:
                # Skip obvious non-package strings (domains, etc.).
                if "." in name and name.count(".") >= 1:
                    continue
            pkgs.setdefault(
                name,
                PackageLicense(
                    name=name, version="", license="UNKNOWN", ecosystem="pip"
                ),
            )
    return list(pkgs.values())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  npm ecosystem
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _scan_npm(app_path: Path, timeout: int) -> tuple[list[PackageLicense], str]:
    if shutil.which("license-checker"):
        rc, out, _ = _run_tool(
            ["license-checker", "--json", "--production"],
            cwd=app_path,
            timeout=timeout,
        )
        if rc == 0 and out.strip():
            try:
                payload = json.loads(out)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                pkgs: list[PackageLicense] = []
                for key, e in payload.items():
                    if "@" in key[1:]:
                        at = key.rfind("@")
                        name, version = key[:at], key[at + 1:]
                    else:
                        name, version = key, ""
                    pkgs.append(
                        PackageLicense(
                            name=name,
                            version=version,
                            license=_normalise_license(
                                (e or {}).get("licenses")
                            ),
                            ecosystem="npm",
                            path=str((e or {}).get("path", "")),
                        )
                    )
                return pkgs, "license-checker"

    # fallback: walk node_modules package.json
    nm = app_path / "node_modules"
    if nm.is_dir():
        return _walk_node_modules(app_path), "walk"
    pkg_json = app_path / "package.json"
    if pkg_json.exists():
        # No install yet — at least enumerate the direct deps.
        return _parse_package_json_direct(pkg_json), "walk"
    return [], "mock"


def _walk_node_modules(app_path: Path) -> list[PackageLicense]:
    nm = app_path / "node_modules"
    if not nm.is_dir():
        return []
    out: list[PackageLicense] = []
    for pkg_json in nm.rglob("package.json"):
        if any(p in ("test", "tests", "__tests__") for p in pkg_json.parts):
            continue
        try:
            data = json.loads(pkg_json.read_text(errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        if not name:
            continue
        lic = data.get("license", data.get("licenses", ""))
        out.append(
            PackageLicense(
                name=str(name),
                version=str(data.get("version") or ""),
                license=_normalise_license(lic),
                ecosystem="npm",
                path=str(pkg_json.parent.relative_to(app_path)),
            )
        )
    return out


def _parse_package_json_direct(path: Path) -> list[PackageLicense]:
    try:
        data = json.loads(path.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[PackageLicense] = []
    for field_name in ("dependencies", "devDependencies", "peerDependencies"):
        for name, ver in (data.get(field_name) or {}).items():
            out.append(
                PackageLicense(
                    name=str(name),
                    version=str(ver or ""),
                    license="UNKNOWN",
                    ecosystem="npm",
                )
            )
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  maven ecosystem (X9 #305)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Regexes for the pom.xml / build.gradle.kts fallback. We only walk
# direct `<dependency>` / `implementation("…")` entries — recursive
# transitive resolution needs the real Maven / Gradle daemon, which
# the preferred-CLI path covers (Mojohaus license plugin runs a full
# resolve). The fallback exists so a CI without an operating `mvn`
# still reports something rather than blind-passing.
_POM_DEP_RE = re.compile(
    r"<dependency>\s*"
    r"(?:<groupId>(?P<group>[^<]+)</groupId>\s*)?"
    r"(?:<artifactId>(?P<artifact>[^<]+)</artifactId>\s*)?"
    r"(?:<version>(?P<version>[^<]+)</version>\s*)?"
    r"(?:<scope>[^<]+</scope>\s*)?"
    r"(?:<type>[^<]+</type>\s*)?"
    r"(?:<optional>[^<]+</optional>\s*)?"
    r"</dependency>",
    re.DOTALL,
)
_GRADLE_DEP_RE = re.compile(
    r"(?:implementation|api|runtimeOnly|compileOnly|testImplementation)"
    r"\s*\(\s*\"(?P<coord>[^\"]+)\"\s*\)"
)


def _scan_maven(app_path: Path, timeout: int) -> tuple[list[PackageLicense], str]:
    """Prefer ``mvn license:aggregate-download-licenses`` (Mojohaus plugin)
    for a full dependency tree with license metadata; fall back to
    parsing ``pom.xml`` / ``build.gradle.kts`` as ``UNKNOWN`` rows so
    the gate still reports on CI hosts without ``mvn``.
    """
    if shutil.which("mvn") and (app_path / "pom.xml").exists():
        rc, _out, err = _run_tool(
            [
                "mvn", "-B", "-q",
                "org.codehaus.mojo:license-maven-plugin:2.4.0:aggregate-download-licenses",
            ],
            cwd=app_path,
            timeout=timeout,
        )
        # The plugin writes its JSON/XML summary to
        # target/generated-resources/licenses.xml. When the plugin ran
        # clean we parse that file; otherwise we fall through to the
        # pom.xml walker.
        xml_report = app_path / "target" / "generated-resources" / "licenses.xml"
        if rc == 0 and xml_report.is_file():
            try:
                return _parse_maven_licenses_xml(xml_report), "mvn-license-plugin"
            except Exception as exc:  # noqa: BLE001 — fall through
                logger.info("mvn license plugin XML parse failed: %s", exc)
        else:
            logger.info("mvn license plugin failed rc=%s err=%s", rc, err[:200])

    pom = app_path / "pom.xml"
    if pom.exists():
        return _parse_pom_xml(pom), "walk"

    for gradle_manifest in ("build.gradle.kts", "build.gradle"):
        candidate = app_path / gradle_manifest
        if candidate.exists():
            return _parse_gradle_build(candidate), "walk"

    return [], "mock"


def _parse_pom_xml(pom_path: Path) -> list[PackageLicense]:
    """Minimal pom.xml parser. Pulls direct `<dependency>` blocks
    only — deep transitive resolution needs real Maven."""
    try:
        text = pom_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out: list[PackageLicense] = []
    seen: set[tuple[str, str]] = set()
    for m in _POM_DEP_RE.finditer(text):
        group = (m.group("group") or "").strip()
        artifact = (m.group("artifact") or "").strip()
        version = (m.group("version") or "").strip()
        if not artifact:
            continue
        name = f"{group}:{artifact}" if group else artifact
        key = (name, version)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PackageLicense(
                name=name,
                version=version,
                license="UNKNOWN",
                ecosystem="maven",
            )
        )
    return out


def _parse_gradle_build(build_path: Path) -> list[PackageLicense]:
    """Minimal build.gradle[.kts] parser. Pulls string-coordinate deps
    (``implementation("group:artifact:version")``) — does NOT handle
    BOMs, platforms, or Kotlin `libs.xxx` version catalogs. License
    is always ``UNKNOWN`` — full resolution needs the Gradle daemon.
    """
    try:
        text = build_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out: list[PackageLicense] = []
    seen: set[tuple[str, str]] = set()
    for m in _GRADLE_DEP_RE.finditer(text):
        coord = m.group("coord").strip()
        parts = coord.split(":")
        if len(parts) < 2:
            continue
        name = f"{parts[0]}:{parts[1]}"
        version = parts[2] if len(parts) >= 3 else ""
        key = (name, version)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PackageLicense(
                name=name,
                version=version,
                license="UNKNOWN",
                ecosystem="maven",
            )
        )
    return out


def _parse_maven_licenses_xml(xml_path: Path) -> list[PackageLicense]:
    """Parse the Mojohaus license-maven-plugin aggregated XML.

    Shape (relevant subset)::

        <licenseSummary>
          <dependencies>
            <dependency>
              <groupId>…</groupId>
              <artifactId>…</artifactId>
              <version>…</version>
              <licenses>
                <license><name>Apache 2.0</name></license>
              </licenses>
            </dependency>
          </dependencies>
        </licenseSummary>
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    out: list[PackageLicense] = []
    for dep in root.iter("dependency"):
        group = (dep.findtext("groupId") or "").strip()
        artifact = (dep.findtext("artifactId") or "").strip()
        version = (dep.findtext("version") or "").strip()
        if not artifact:
            continue
        licenses_el = dep.find("licenses")
        raw: Any = "UNKNOWN"
        if licenses_el is not None:
            names = [
                (lic.findtext("name") or "").strip()
                for lic in licenses_el.iter("license")
            ]
            names = [n for n in names if n]
            if names:
                raw = names if len(names) > 1 else names[0]
        out.append(
            PackageLicense(
                name=f"{group}:{artifact}" if group else artifact,
                version=version,
                license=_normalise_license(raw),
                ecosystem="maven",
            )
        )
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCANNERS = {
    "cargo": _scan_cargo,
    "go": _scan_go,
    "pip": _scan_pip,
    "npm": _scan_npm,
    "maven": _scan_maven,
}


def scan_licenses(
    app_path: Path | str,
    *,
    ecosystem: Optional[str] = None,
    deny: Iterable[str] = DEFAULT_DENY_LICENSES,
    allowlist: Iterable[str] | None = None,
    timeout: int = 120,
) -> LicenseReport:
    """Enumerate dependencies of ``app_path`` and verdict each license.

    ``ecosystem`` forces a specific scanner. When ``None``, auto-detect
    from marker files. Returns a ``LicenseReport`` whose ``.passed`` is
    ``False`` when any denied package survives the allowlist filter.

    An allowlist entry is either the package ``name`` or a composite
    ``name@license`` string (matches either exactly).
    """
    root = Path(app_path).resolve()
    report = LicenseReport(app_path=str(root))
    deny_set = set(deny)
    report.deny_list = sorted(deny_set)
    allowlist_set = {a.strip() for a in (allowlist or []) if a.strip()}
    report.allowlist_used = sorted(allowlist_set)

    if not root.is_dir():
        report.error = f"app_path '{root}' is not a directory"
        return report

    eco = ecosystem or detect_ecosystem(root)
    if eco is None:
        report.error = "no ecosystem marker file found (Cargo.toml / go.mod / pyproject.toml / requirements.txt / package.json)"
        report.ecosystem = ""
        return report
    if eco not in _SCANNERS:
        report.error = f"unknown ecosystem {eco!r} (supported: {ECOSYSTEMS})"
        report.ecosystem = eco
        return report
    report.ecosystem = eco

    try:
        packages, source = _SCANNERS[eco](root, timeout)
    except Exception as exc:  # pragma: no cover — defensive
        report.error = f"scanner {eco} crashed: {exc}"
        return report
    report.source = source
    report.total_packages = len(packages)

    for pkg in packages:
        if _license_matches(pkg.license, deny_set):
            key = pkg.name
            key_versioned = f"{pkg.name}@{pkg.license}"
            if key in allowlist_set or key_versioned in allowlist_set:
                report.allowed.append(pkg)
            else:
                report.denied.append(pkg)
        elif not pkg.license or pkg.license == "UNKNOWN":
            report.unknown.append(pkg)
        else:
            report.allowed.append(pkg)

    return report


__all__ = [
    "DEFAULT_DENY_LICENSES",
    "ECOSYSTEMS",
    "LicenseReport",
    "PackageLicense",
    "detect_ecosystem",
    "scan_licenses",
]
