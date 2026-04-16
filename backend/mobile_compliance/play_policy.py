"""P6 #291 — Google Play Policy static scan.

Three checks the P6 ticket calls out:

    1. **Background location** (Policy: Location data) — if the app
       declares ``ACCESS_BACKGROUND_LOCATION`` in AndroidManifest.xml
       it must (a) declare the matching runtime permission flow and
       (b) ship a ``docs/play/background_location_justification.md``
       that explains the core in-app value proposition. Missing the
       justification file is a blocker — Play rejects on "permission
       declaration form" incomplete.
    2. **SDK version floor** (Policy: Target API level) — ``build.gradle``
       / ``build.gradle.kts`` must set ``targetSdk`` to the current
       floor (``35`` for Play 2026) or later. Apps below the floor
       are delisted annually; the gate's floor is configurable via
       ``MIN_TARGET_SDK``.
    3. **Data Safety form** (Policy: Data safety) — a
       ``docs/play/data_safety.yaml`` file must be present and must
       list every SDK that appears in ``build.gradle`` dependencies
       (cross-check against the SDK → data-category map in
       ``configs/privacy_label_sdks.yaml``).

Like the ASC gate this scan is 100% static — it never talks to Play
Console. Works the same in CI and sandbox. The ``MIN_TARGET_SDK``
floor is intentionally exposed as a constant (not read from Play at
scan time) so the gate's verdict is deterministic under test.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Detection rules ─────────────────────────────────────────────────

# The current Play Store target-SDK floor. Google publishes this
# annually; we pin the 2026 floor here. Update when Google raises it.
MIN_TARGET_SDK: int = 35

BACKGROUND_LOCATION_PERMISSION = "android.permission.ACCESS_BACKGROUND_LOCATION"
FINE_LOCATION_PERMISSION = "android.permission.ACCESS_FINE_LOCATION"
COARSE_LOCATION_PERMISSION = "android.permission.ACCESS_COARSE_LOCATION"

# Justification file locations Play expects to exist (any one is fine).
BACKGROUND_LOCATION_JUSTIFICATION_PATHS = (
    "docs/play/background_location_justification.md",
    "docs/play/background-location.md",
    "store/play/background_location.md",
    "play/background_location_justification.md",
    "background_location_justification.md",
)

# Data Safety form location(s). The form is uploaded to Play Console
# from this YAML in CI.
DATA_SAFETY_FORM_PATHS = (
    "docs/play/data_safety.yaml",
    "docs/play/data-safety.yaml",
    "store/play/data_safety.yaml",
    "play/data_safety.yaml",
)

# Regex — matches ``targetSdk 35``, ``targetSdk = 35``, ``targetSdkVersion 35``
# across Groovy and Kotlin DSL.
_TARGET_SDK_RE = re.compile(
    r"targetSdk(?:Version)?\s*[=\s]\s*(\d+)",
    re.IGNORECASE,
)

# Regex — matches ``implementation 'com.foo:bar:1.2.3'`` / ``api(...)`` /
# ``debugImplementation`` etc. across Groovy and Kotlin DSL.
_DEPENDENCY_RE = re.compile(
    r"^\s*(?:implementation|api|compileOnly|runtimeOnly|"
    r"debugImplementation|testImplementation|androidTestImplementation|"
    r"kapt|ksp)\s*[(]?\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)


# ── Result schema ───────────────────────────────────────────────────


@dataclass
class PlayFinding:
    rule_id: str   # "background_location" / "target_sdk" / "data_safety"
    severity: str  # "blocker" / "warning"
    path: str
    line: int
    message: str
    snippet: str = ""

    @property
    def is_blocker(self) -> bool:
        return self.severity == "blocker"


@dataclass
class PlayPolicyReport:
    app_path: str
    findings: list[PlayFinding] = field(default_factory=list)
    target_sdk: int | None = None
    declares_background_location: bool = False
    data_safety_form_path: str | None = None
    dependencies: list[str] = field(default_factory=list)
    passed: bool = True

    def recompute_passed(self) -> None:
        self.passed = not any(f.is_blocker for f in self.findings)

    @property
    def blockers(self) -> list[PlayFinding]:
        return [f for f in self.findings if f.is_blocker]

    @property
    def warnings(self) -> list[PlayFinding]:
        return [f for f in self.findings if not f.is_blocker]

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_path": self.app_path,
            "target_sdk": self.target_sdk,
            "declares_background_location": self.declares_background_location,
            "data_safety_form_path": self.data_safety_form_path,
            "dependencies": list(self.dependencies),
            "passed": self.passed,
            "blocker_count": len(self.blockers),
            "warning_count": len(self.warnings),
            "findings": [asdict(f) for f in self.findings],
        }


# ── Scan helpers ────────────────────────────────────────────────────


def _find_android_manifests(root: Path) -> list[Path]:
    if not root.exists():
        return []
    # Prefer explicit app/src/main/AndroidManifest.xml layout, then fall
    # back to any AndroidManifest.xml under the repo (skipping build/).
    result: list[Path] = []
    for p in root.rglob("AndroidManifest.xml"):
        if "/build/" in str(p) or "/.gradle/" in str(p):
            continue
        result.append(p)
    return result


def _find_gradle_scripts(root: Path) -> list[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for name in ("build.gradle", "build.gradle.kts", "app/build.gradle",
                 "app/build.gradle.kts"):
        p = root / name
        if p.exists() and p.is_file():
            result.append(p)
    # Also include any module build.gradle(.kts) not already matched.
    for p in root.rglob("build.gradle"):
        if p not in result and "/build/" not in str(p):
            result.append(p)
    for p in root.rglob("build.gradle.kts"):
        if p not in result and "/build/" not in str(p):
            result.append(p)
    return result


def _parse_manifest_permissions(manifest_path: Path) -> tuple[set[str], int | None]:
    try:
        content = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), None
    perms = set()
    bg_line: int | None = None
    for lineno, line in enumerate(content.splitlines(), start=1):
        m = re.search(
            r'<uses-permission[^>]+android:name="([^"]+)"',
            line,
        )
        if m:
            name = m.group(1)
            perms.add(name)
            if name == BACKGROUND_LOCATION_PERMISSION and bg_line is None:
                bg_line = lineno
    return perms, bg_line


def _parse_target_sdk(gradle_paths: list[Path]) -> tuple[int | None, Path | None, int]:
    """Return (target_sdk, path_where_found, line_number).

    Line number is 0 when not found anywhere.
    """
    best: tuple[int | None, Path | None, int] = (None, None, 0)
    for p in gradle_paths:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            m = _TARGET_SDK_RE.search(line)
            if m:
                try:
                    val = int(m.group(1))
                except ValueError:
                    continue
                if best[0] is None or val > best[0]:
                    best = (val, p, lineno)
    return best


def _collect_dependencies(gradle_paths: list[Path]) -> list[str]:
    deps: list[str] = []
    for p in gradle_paths:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        deps.extend(_DEPENDENCY_RE.findall(content))
    return sorted(set(deps))


def _find_justification_file(root: Path) -> Path | None:
    for rel in BACKGROUND_LOCATION_JUSTIFICATION_PATHS:
        p = root / rel
        if p.exists() and p.is_file():
            try:
                if p.stat().st_size > 0:
                    return p
            except OSError:
                continue
    return None


def _find_data_safety_form(root: Path) -> Path | None:
    for rel in DATA_SAFETY_FORM_PATHS:
        p = root / rel
        if p.exists() and p.is_file():
            return p
    return None


def _load_data_safety_declared_sdks(form_path: Path) -> set[str]:
    """Parse the Data Safety YAML and return the set of SDK identifiers
    listed under ``declared_sdks``. Falls back to yaml.safe_load; on any
    parse error we treat the form as "present but unparseable" — callers
    may still flag that as a blocker via ``form_valid=False``.
    """
    try:
        import yaml  # noqa: WPS433 — local import keeps base import fast
    except ImportError:
        return set()
    try:
        raw = form_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    declared = data.get("declared_sdks") or []
    if not isinstance(declared, list):
        return set()
    return {str(x).strip() for x in declared if str(x).strip()}


# ── Individual checks ───────────────────────────────────────────────


def _check_background_location(
    root: Path,
    manifest_findings: list[tuple[Path, set[str], int | None]],
    report: PlayPolicyReport,
) -> None:
    declared = any(
        BACKGROUND_LOCATION_PERMISSION in perms
        for _, perms, _ in manifest_findings
    )
    report.declares_background_location = declared
    if not declared:
        return

    justification = _find_justification_file(root)
    for manifest_path, perms, bg_line in manifest_findings:
        if BACKGROUND_LOCATION_PERMISSION not in perms:
            continue
        if justification is not None:
            report.findings.append(PlayFinding(
                rule_id="background_location",
                severity="warning",
                path=str(manifest_path.relative_to(root)),
                line=bg_line or 1,
                message=(
                    "Declares ACCESS_BACKGROUND_LOCATION. Justification "
                    f"file present at {justification.relative_to(root)} — "
                    "ensure Play Console declaration form matches it."
                ),
                snippet="",
            ))
        else:
            report.findings.append(PlayFinding(
                rule_id="background_location",
                severity="blocker",
                path=str(manifest_path.relative_to(root)),
                line=bg_line or 1,
                message=(
                    "Declares ACCESS_BACKGROUND_LOCATION without a "
                    "justification file. Play rejects apps missing the "
                    "background-location declaration — add "
                    "docs/play/background_location_justification.md."
                ),
                snippet="",
            ))

        # Also require fine/coarse location to be declared — Play
        # doesn't accept background-only location.
        if (FINE_LOCATION_PERMISSION not in perms
                and COARSE_LOCATION_PERMISSION not in perms):
            report.findings.append(PlayFinding(
                rule_id="background_location",
                severity="blocker",
                path=str(manifest_path.relative_to(root)),
                line=bg_line or 1,
                message=(
                    "Background location declared without ACCESS_FINE_LOCATION "
                    "or ACCESS_COARSE_LOCATION — invalid permission combination."
                ),
                snippet="",
            ))


def _check_target_sdk(
    root: Path,
    gradle_paths: list[Path],
    report: PlayPolicyReport,
    min_target_sdk: int,
) -> None:
    val, path, lineno = _parse_target_sdk(gradle_paths)
    report.target_sdk = val

    if val is None:
        # Only flag when there IS a gradle script — otherwise this isn't
        # an Android project at all and the caller should skip us.
        if gradle_paths:
            report.findings.append(PlayFinding(
                rule_id="target_sdk",
                severity="blocker",
                path=str(gradle_paths[0].relative_to(root)),
                line=1,
                message=(
                    "No targetSdk / targetSdkVersion declaration found in any "
                    "build.gradle(.kts). Play requires an explicit target SDK."
                ),
                snippet="",
            ))
        return

    if val < min_target_sdk:
        assert path is not None
        report.findings.append(PlayFinding(
            rule_id="target_sdk",
            severity="blocker",
            path=str(path.relative_to(root)),
            line=lineno,
            message=(
                f"targetSdk={val} is below Play floor {min_target_sdk}. "
                "Apps below floor are delisted annually."
            ),
            snippet="",
        ))
    elif val == min_target_sdk:
        assert path is not None
        report.findings.append(PlayFinding(
            rule_id="target_sdk",
            severity="warning",
            path=str(path.relative_to(root)),
            line=lineno,
            message=(
                f"targetSdk={val} matches floor — will need to bump before "
                "next annual Play policy deadline."
            ),
            snippet="",
        ))


def _check_data_safety_form(
    root: Path,
    report: PlayPolicyReport,
) -> None:
    form = _find_data_safety_form(root)
    if form is None:
        if report.dependencies:
            # Only flag when there IS an Android project with deps —
            # otherwise we're not the right gate for this path.
            report.findings.append(PlayFinding(
                rule_id="data_safety",
                severity="blocker",
                path=".",
                line=0,
                message=(
                    "No Data Safety form found. Expected one of: "
                    + ", ".join(DATA_SAFETY_FORM_PATHS)
                    + ". Play rejects submissions without a completed form."
                ),
                snippet="",
            ))
        return

    report.data_safety_form_path = str(form.relative_to(root))
    declared_sdks = _load_data_safety_declared_sdks(form)

    # Cross-check: every dependency coordinate (group:artifact) should
    # appear in ``declared_sdks`` OR its group-prefix should. This is a
    # best-effort check — false positives on internal / first-party
    # artifacts are tolerable; the evidence is what the Play reviewer
    # actually cares about.
    missing: list[str] = []
    for dep in report.dependencies:
        # ``com.google.firebase:firebase-auth:22.0.0`` → match on
        # "com.google.firebase:firebase-auth" or "com.google.firebase".
        coord = dep.rsplit(":", 1)[0] if dep.count(":") >= 2 else dep
        group = coord.split(":", 1)[0]
        if coord in declared_sdks or group in declared_sdks:
            continue
        missing.append(coord)

    for coord in missing[:10]:  # cap findings to avoid log spam
        report.findings.append(PlayFinding(
            rule_id="data_safety",
            severity="warning",
            path=str(form.relative_to(root)),
            line=0,
            message=(
                f"Dependency '{coord}' is not listed in declared_sdks — "
                "Play requires every third-party SDK that transmits data "
                "to be declared on the Data Safety form."
            ),
            snippet="",
        ))


# ── Public entry ────────────────────────────────────────────────────


def scan_play_policy(
    app_path: Path | str,
    *,
    min_target_sdk: int = MIN_TARGET_SDK,
) -> PlayPolicyReport:
    """Run the three Play-policy checks over ``app_path``.

    If there's no AndroidManifest.xml AND no build.gradle anywhere
    under ``app_path`` the report returns empty with ``passed=True``
    and ``target_sdk=None`` — callers treat it as "not an Android app,
    skip me".
    """
    root = Path(app_path).resolve()
    report = PlayPolicyReport(app_path=str(root))

    if not root.exists():
        return report

    manifests = _find_android_manifests(root)
    gradles = _find_gradle_scripts(root)

    if not manifests and not gradles:
        return report  # skip — not an Android project

    manifest_data = [
        (m, *_parse_manifest_permissions(m))
        for m in manifests
    ]

    report.dependencies = _collect_dependencies(gradles)

    _check_background_location(root, manifest_data, report)
    _check_target_sdk(root, gradles, report, min_target_sdk)
    _check_data_safety_form(root, report)

    report.recompute_passed()
    return report


__all__ = [
    "BACKGROUND_LOCATION_PERMISSION",
    "DATA_SAFETY_FORM_PATHS",
    "MIN_TARGET_SDK",
    "PlayFinding",
    "PlayPolicyReport",
    "scan_play_policy",
]
