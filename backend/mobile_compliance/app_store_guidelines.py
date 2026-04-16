"""P6 #291 — App Store Review Guidelines static scan.

Scans an iOS / macOS project's source, Info.plist and store metadata for
the three "obvious-bust" App Store Review Guidelines violations the P6
ticket calls out:

    1. **Fake / misleading payment UI** (Guideline 3.1.1) — any in-app
       UI string that implies a real-money purchase while bypassing
       StoreKit / Apple IAP (e.g. credit-card input for a digital-good
       purchase, Stripe/PayPal SDK references alongside "buy X coins"
       strings).
    2. **Misleading marketing copy** (Guideline 2.3) — store listing
       text that references a competing platform ("Also on Android"),
       claims medical-grade accuracy without disclaimer, or uses words
       Apple explicitly bans in app titles ("lite", "free", "beta")
       except as sub-titles.
    3. **Undeclared private API usage** (Guideline 2.5.1) — Objective-C
       / Swift calls into known private SPI symbols without being
       gated behind ``#if DEBUG``. We look for a curated list of
       high-risk SPI selectors (``_setBackgroundStyle:``,
       ``_UIBackdropView``, ``launchApplicationWithIdentifier:``, …)
       and any DYLD_INSERT_LIBRARIES / `dlopen` of framework paths
       under ``/System/Library/PrivateFrameworks/``.

The scan is purely static — it never invokes ``xcodebuild`` or talks
to App Store Connect. False positives are resolvable via a
``.app-store-review-ignore`` file (one path per line, supports
``# comment`` lines). True negatives are fine; this gate's job is
to catch the *obvious* busts before a human reviewer wastes a
submission slot on a 24h rejection cycle.
"""

from __future__ import annotations

import logging
import plistlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ── Detection rules ─────────────────────────────────────────────────

# Guideline 3.1.1 — non-Apple payment SDKs shipped into an iOS app.
# Presence alone isn't fatal (a physical-goods app may legitimately use
# Stripe); we flag the combination of a non-Apple SDK *plus* an obvious
# "digital-goods purchase" string.
NON_APPLE_PAYMENT_SDK_MARKERS = (
    "StripePaymentSheet",
    "StripePayments",
    "StripeCore",
    "PayPalCheckout",
    "BraintreeCore",
    "SquareInAppPayments",
    "Adyen",
    "import Stripe",
    "import PayPalCheckout",
    "import Braintree",
)

DIGITAL_GOODS_PURCHASE_MARKERS = (
    r"buy\s+\d+\s+coins?",
    r"buy\s+\d+\s+gems?",
    r"purchase\s+(premium|pro|plus|gold)\s+(subscription|tier|plan)",
    r"unlock\s+full\s+version",
    r"remove\s+ads",
    r"(?:monthly|yearly|annual)\s+subscription",
    r"credit\s*card\s+required",
)

_DIGITAL_GOODS_RE = [
    re.compile(p, re.IGNORECASE) for p in DIGITAL_GOODS_PURCHASE_MARKERS
]

# Guideline 2.3.10 — misleading marketing / metadata.
# Apple has explicit rules around title length, competing-platform
# references, and claims that require a disclaimer.
MISLEADING_COPY_PATTERNS = (
    (re.compile(r"also\s+on\s+(android|google\s+play)", re.I),
     "references competing platform in listing copy"),
    (re.compile(r"medical[-\s]?grade\s+(accuracy|certified)", re.I),
     "claims medical-grade accuracy without FDA disclaimer"),
    (re.compile(r"#1\s+(app|best|top)\b", re.I),
     "uses unverifiable superlative ('#1 app')"),
    (re.compile(r"FDA[-\s]?approved", re.I),
     "claims FDA approval — Apple requires a verifiable reference"),
    (re.compile(r"guaranteed\s+\$\d+\s+per\s+(day|week|month)", re.I),
     "makes guaranteed-income earnings claim"),
    (re.compile(r"100\%\s+(free|risk[-\s]?free)", re.I),
     "absolute free claim conflicts with IAP tier"),
)

# Apple disallows these bare words as the entire app title, but they're
# OK as subtitle modifiers. We flag them when the **title** field of
# the store metadata file exactly matches or contains them as a bare
# word with no other text.
BARE_TITLE_WORDS = {"free", "lite", "beta", "test", "demo"}

# Guideline 2.5.1 — private API usage. This is a curated set of
# well-known private-SPI selectors and private-framework paths.
# Source: inferred from public Apple documentation + common rejection
# patterns. Not exhaustive; extended via project-level config.
PRIVATE_API_SYMBOLS = (
    "_setBackgroundStyle:",
    "_UIBackdropView",
    "_UIBackdropEffectView",
    "launchApplicationWithIdentifier:suspended:",
    "SBSLaunchApplicationWithIdentifier",
    "UIGetScreenImage",
    "_AXSSpeakThisEnabled",
    "_spi_viewControllerForAncestor",
    "CTServerConnectionCopyMobileIdentity",
    "_CFURLEnumeratorCreate",
    "MFMailComposeInternalViewController",
)

PRIVATE_FRAMEWORK_DLOPEN_RE = re.compile(
    r"(dlopen|NSBundle.*bundleWithPath)\s*\(\s*"
    r"['\"]\s*(/System/Library/PrivateFrameworks/[^'\"]+)['\"]",
    re.IGNORECASE,
)

# File suffixes we scan
_IOS_SOURCE_SUFFIXES = {".swift", ".m", ".mm", ".h", ".hpp", ".c", ".cpp"}
_METADATA_SUFFIXES = {".txt", ".md", ".json"}

# Metadata filenames that hold public-facing store copy.
_STORE_METADATA_CANDIDATES = (
    "fastlane/metadata/en-US/description.txt",
    "fastlane/metadata/en-US/name.txt",
    "fastlane/metadata/en-US/subtitle.txt",
    "fastlane/metadata/en-US/keywords.txt",
    "fastlane/metadata/en-US/marketing_url.txt",
    "AppStoreConnect/description.md",
    "AppStoreConnect/name.txt",
    "store/ios/description.txt",
    "store/ios/title.txt",
)

IGNORE_FILENAME = ".app-store-review-ignore"


# ── Result schema ───────────────────────────────────────────────────


@dataclass
class ASCFinding:
    """One concrete violation pin-pointed to a file + line."""

    rule_id: str   # "3.1.1" / "2.3.10" / "2.5.1"
    severity: str  # "blocker" / "warning"
    path: str
    line: int
    message: str
    snippet: str = ""

    @property
    def is_blocker(self) -> bool:
        return self.severity == "blocker"


@dataclass
class ASCGuidelinesReport:
    """Output of :func:`scan_app_store_guidelines`."""

    app_path: str
    findings: list[ASCFinding] = field(default_factory=list)
    files_scanned: int = 0
    ignored_paths: list[str] = field(default_factory=list)
    # True if no blocker findings; warnings don't fail the gate.
    passed: bool = True

    def recompute_passed(self) -> None:
        self.passed = not any(f.is_blocker for f in self.findings)

    @property
    def blockers(self) -> list[ASCFinding]:
        return [f for f in self.findings if f.is_blocker]

    @property
    def warnings(self) -> list[ASCFinding]:
        return [f for f in self.findings if not f.is_blocker]

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_path": self.app_path,
            "files_scanned": self.files_scanned,
            "passed": self.passed,
            "blocker_count": len(self.blockers),
            "warning_count": len(self.warnings),
            "ignored_paths": list(self.ignored_paths),
            "findings": [asdict(f) for f in self.findings],
        }


# ── Helpers ─────────────────────────────────────────────────────────


def _load_ignore(root: Path) -> set[str]:
    ignore = root / IGNORE_FILENAME
    if not ignore.exists():
        return set()
    try:
        lines = ignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def _is_ignored(rel_path: str, ignore: set[str]) -> bool:
    if rel_path in ignore:
        return True
    # Allow dir-prefix ignores
    for entry in ignore:
        if entry.endswith("/") and rel_path.startswith(entry):
            return True
    return False


def _iter_source_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return (
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix in _IOS_SOURCE_SUFFIXES
        and "/.git/" not in str(p)
        and "/Pods/" not in str(p)   # skip vendored pods — not our code
        and "/build/" not in str(p)
        and "/DerivedData/" not in str(p)
    )


def _iter_metadata_files(root: Path) -> Iterable[Path]:
    for rel in _STORE_METADATA_CANDIDATES:
        p = root / rel
        if p.exists() and p.is_file():
            yield p


def _scan_payment_violations(
    root: Path,
    ignore: set[str],
) -> tuple[list[ASCFinding], int]:
    """Guideline 3.1.1 — non-Apple IAP for digital goods."""
    findings: list[ASCFinding] = []
    scanned = 0

    # First pass: collect files that import a non-Apple payment SDK.
    files_with_sdk: list[tuple[Path, int, str]] = []
    files_with_digital: list[tuple[Path, int, str]] = []

    for path in _iter_source_files(root):
        rel = str(path.relative_to(root))
        if _is_ignored(rel, ignore):
            continue
        scanned += 1
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for marker in NON_APPLE_PAYMENT_SDK_MARKERS:
                if marker in line:
                    files_with_sdk.append((path, lineno, line.strip()))
                    break
            for rgx in _DIGITAL_GOODS_RE:
                m = rgx.search(line)
                if m:
                    files_with_digital.append((path, lineno, line.strip()))
                    break

    # Also check metadata for digital-goods marketing strings.
    for path in _iter_metadata_files(root):
        rel = str(path.relative_to(root))
        if _is_ignored(rel, ignore):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for rgx in _DIGITAL_GOODS_RE:
                if rgx.search(line):
                    files_with_digital.append((path, lineno, line.strip()))
                    break

    # Emit blockers only when BOTH conditions co-exist in the project.
    if files_with_sdk and files_with_digital:
        for path, lineno, snippet in files_with_sdk:
            findings.append(ASCFinding(
                rule_id="3.1.1",
                severity="blocker",
                path=str(path.relative_to(root)),
                line=lineno,
                message=(
                    "Non-Apple payment SDK detected in a project that also "
                    "markets digital goods — Apple requires StoreKit for "
                    "digital-content purchases (Guideline 3.1.1)"
                ),
                snippet=snippet[:200],
            ))
    # Warn when only one side is present (informational for reviewer).
    elif files_with_sdk and not files_with_digital:
        for path, lineno, snippet in files_with_sdk[:1]:
            findings.append(ASCFinding(
                rule_id="3.1.1",
                severity="warning",
                path=str(path.relative_to(root)),
                line=lineno,
                message=(
                    "Non-Apple payment SDK present. OK if all purchases are "
                    "physical goods or real-world services — otherwise must "
                    "use StoreKit (Guideline 3.1.1)."
                ),
                snippet=snippet[:200],
            ))

    return findings, scanned


def _scan_misleading_copy(
    root: Path,
    ignore: set[str],
) -> list[ASCFinding]:
    """Guideline 2.3.10 — misleading marketing copy."""
    findings: list[ASCFinding] = []
    for path in _iter_metadata_files(root):
        rel = str(path.relative_to(root))
        if _is_ignored(rel, ignore):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Bare-title word rule: only fires against *name.txt / title.txt*.
        if path.name in ("name.txt", "title.txt"):
            stripped = content.strip().lower()
            if stripped in BARE_TITLE_WORDS:
                findings.append(ASCFinding(
                    rule_id="2.3.10",
                    severity="blocker",
                    path=rel,
                    line=1,
                    message=(
                        f"App title is a bare word '{stripped}' — "
                        "Apple rejects titles that are just 'free' / "
                        "'lite' / 'beta' / 'demo' / 'test'."
                    ),
                    snippet=content.strip()[:120],
                ))

        for lineno, line in enumerate(content.splitlines(), start=1):
            for rgx, msg in MISLEADING_COPY_PATTERNS:
                if rgx.search(line):
                    findings.append(ASCFinding(
                        rule_id="2.3.10",
                        severity="blocker",
                        path=rel,
                        line=lineno,
                        message=f"Misleading copy: {msg}",
                        snippet=line.strip()[:200],
                    ))
                    break
    return findings


def _scan_private_api(
    root: Path,
    ignore: set[str],
) -> list[ASCFinding]:
    """Guideline 2.5.1 — undeclared private API usage."""
    findings: list[ASCFinding] = []
    for path in _iter_source_files(root):
        rel = str(path.relative_to(root))
        if _is_ignored(rel, ignore):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Walk line-by-line so we can track whether a match is inside
        # a ``#if DEBUG`` block.
        in_debug = 0  # nesting depth
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#if") and "DEBUG" in stripped:
                in_debug += 1
                continue
            if stripped.startswith("#endif") and in_debug > 0:
                in_debug -= 1
                continue
            if in_debug:
                continue

            for sym in PRIVATE_API_SYMBOLS:
                # Match either the full Obj-C selector ('foo:bar:') OR
                # the bare base name (Swift call site drops the colons:
                # '_setBackgroundStyle(0)' vs ObjC '_setBackgroundStyle:').
                base = sym.split(":", 1)[0]
                if sym in line or (base and base in line):
                    findings.append(ASCFinding(
                        rule_id="2.5.1",
                        severity="blocker",
                        path=rel,
                        line=lineno,
                        message=(
                            f"Uses private API symbol '{sym}' outside "
                            "#if DEBUG (Guideline 2.5.1)"
                        ),
                        snippet=line.strip()[:200],
                    ))
                    break  # one finding per line is plenty

            m = PRIVATE_FRAMEWORK_DLOPEN_RE.search(line)
            if m:
                findings.append(ASCFinding(
                    rule_id="2.5.1",
                    severity="blocker",
                    path=rel,
                    line=lineno,
                    message=(
                        "dlopen() into /System/Library/PrivateFrameworks/ "
                        "— unconditional private-framework load"
                    ),
                    snippet=line.strip()[:200],
                ))
    return findings


def _scan_info_plist_claims(root: Path) -> list[ASCFinding]:
    """Cross-check Info.plist for missing privacy strings when obvious
    APIs (camera / mic / location / contacts) are used in source.

    Apple rejects an app that requests ``NSCameraUsageDescription``-gated
    APIs without the matching key in Info.plist. We flag the inverse
    side: if the source uses the API and Info.plist lacks the key, that's
    a 5.1.1 blocker.
    """
    findings: list[ASCFinding] = []
    plist_candidates = [
        root / "Info.plist",
        root / "App" / "Info.plist",
        root / "iOS" / "Info.plist",
    ]
    plist_candidates += list(root.rglob("Info.plist"))
    seen = set()
    plist_dict: dict[str, Any] = {}
    plist_path: Path | None = None
    for cand in plist_candidates:
        if not cand.exists() or cand in seen:
            continue
        seen.add(cand)
        try:
            with open(cand, "rb") as fh:
                plist_dict = plistlib.load(fh)
            plist_path = cand
            break
        except (plistlib.InvalidFileException, OSError):
            continue

    if plist_path is None:
        # No Info.plist discovered — not an iOS project; no finding.
        return findings

    api_key_map = {
        "AVCaptureDevice": "NSCameraUsageDescription",
        "CLLocationManager": "NSLocationWhenInUseUsageDescription",
        "CNContactStore": "NSContactsUsageDescription",
        "EKEventStore": "NSCalendarsUsageDescription",
        "HKHealthStore": "NSHealthShareUsageDescription",
        "AVAudioRecorder": "NSMicrophoneUsageDescription",
        "PHPhotoLibrary": "NSPhotoLibraryUsageDescription",
    }

    used_apis: dict[str, tuple[str, int]] = {}
    for path in _iter_source_files(root):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for api, key in api_key_map.items():
                if api in line and api not in used_apis:
                    used_apis[api] = (str(path.relative_to(root)), lineno)

    for api, (rel, lineno) in used_apis.items():
        key = api_key_map[api]
        if key not in plist_dict:
            findings.append(ASCFinding(
                rule_id="5.1.1",
                severity="blocker",
                path=rel,
                line=lineno,
                message=(
                    f"Uses {api} but Info.plist lacks '{key}'. "
                    "Apple auto-rejects on missing usage-description strings."
                ),
                snippet="",
            ))

    return findings


# ── Public entry ────────────────────────────────────────────────────


def scan_app_store_guidelines(app_path: Path | str) -> ASCGuidelinesReport:
    """Run the three ASC-review checks over ``app_path``.

    Works on any directory: if it contains no iOS source at all the
    report is returned empty with ``passed=True`` and ``files_scanned=0``
    so the caller can treat it as "skipped" rather than a fail.
    """
    root = Path(app_path).resolve()
    report = ASCGuidelinesReport(app_path=str(root))

    if not root.exists():
        report.passed = True
        return report

    ignore = _load_ignore(root)
    report.ignored_paths = sorted(ignore)

    findings_payment, scanned = _scan_payment_violations(root, ignore)
    report.findings.extend(findings_payment)
    report.files_scanned = scanned

    report.findings.extend(_scan_misleading_copy(root, ignore))
    report.findings.extend(_scan_private_api(root, ignore))
    report.findings.extend(_scan_info_plist_claims(root))

    report.recompute_passed()
    return report


__all__ = [
    "ASCFinding",
    "ASCGuidelinesReport",
    "BARE_TITLE_WORDS",
    "MISLEADING_COPY_PATTERNS",
    "NON_APPLE_PAYMENT_SDK_MARKERS",
    "PRIVATE_API_SYMBOLS",
    "scan_app_store_guidelines",
]
