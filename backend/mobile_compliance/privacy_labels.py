"""P6 #291 — Privacy nutrition label + Play Data-Safety Form generator.

Reads a mobile project's dependency manifests (Podfile.lock /
Package.resolved / build.gradle) and the curated SDK → data-category
map at ``configs/privacy_label_sdks.yaml``, then emits:

    * **iOS App Privacy nutrition label** (JSON) — matches the schema
      App Store Connect expects when uploaded via the
      ``/v1/appPrivacyDetails`` endpoint.
    * **Play Data Safety form** (YAML) — matches the canonical field
      layout the P5 ``play_policy`` gate reads back.

The generator is static and deterministic — given the same
dependency list it always produces the same label. That stability
matters because the label becomes part of the store submission's
audit hash-chain (P3 codesign + P5 store_submission).

Usage:
    report = generate_privacy_label(app_path, platform="ios")
    # report.nutrition_label_ios  → dict (JSON-ready)
    # report.data_safety_form     → dict (YAML-ready)

The function works cross-platform — if both iOS and Android project
files are present it fills in both outputs from the merged SDK set.

Detection of dependencies:

* iOS
  ``Podfile.lock``             → top-level pods
  ``*.xcodeproj/project.pbxproj`` → SPM package references (regex)
  ``Package.resolved``         → SPM packages (JSON v2/v3 schemas)

* Android
  ``build.gradle`` / ``build.gradle.kts`` — ``implementation 'g:a:v'``
  dependency lines (Groovy + Kotlin DSL).

Missing manifest files are not an error — we just produce a label
populated by whatever manifests exist.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# Canonical taxonomy (subset — the full Apple list has ~30 categories;
# we enumerate the ones our SDK table produces).
APPLE_CATEGORY_TAXONOMY = (
    "Contact Info",
    "Health & Fitness",
    "Financial Info",
    "Location",
    "Sensitive Info",
    "Contacts",
    "User Content",
    "Browsing History",
    "Search History",
    "Identifiers",
    "Purchases",
    "Usage Data",
    "Diagnostics",
    "Other Data",
)

PLAY_CATEGORY_TAXONOMY = (
    "Personal info",
    "Financial info",
    "Health and fitness",
    "Messages",
    "Photos and videos",
    "Audio files",
    "Files and docs",
    "Calendar",
    "Contacts",
    "App activity",
    "Web browsing",
    "App info and performance",
    "Device or other IDs",
    "Location",
)

APPLE_PURPOSE_TAXONOMY = (
    "Third-Party Advertising",
    "Developer's Advertising or Marketing",
    "Analytics",
    "Product Personalization",
    "App Functionality",
    "Other Purposes",
)


# ── Result schema ───────────────────────────────────────────────────


@dataclass
class PrivacyLabelReport:
    """Generator output. Same shape for both platforms."""

    app_path: str
    platform: str  # "ios" / "android" / "both"
    detected_sdks: list[str] = field(default_factory=list)
    unknown_dependencies: list[str] = field(default_factory=list)
    nutrition_label_ios: dict[str, Any] = field(default_factory=dict)
    data_safety_form: dict[str, Any] = field(default_factory=dict)
    # "ok" / "no_manifests" / "empty_catalogue" — self-describing.
    status: str = "ok"

    @property
    def passed(self) -> bool:
        """A label generation "passes" if we produced non-empty output.

        Empty output (no manifests + no deps) is treated as a *skip*,
        not a fail, by the bundle orchestrator — an app repo with no
        iOS / Android project in it isn't the right caller for this
        gate.
        """
        return bool(self.detected_sdks or self.nutrition_label_ios
                    or self.data_safety_form)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_path": self.app_path,
            "platform": self.platform,
            "status": self.status,
            "detected_sdks": list(self.detected_sdks),
            "unknown_dependencies": list(self.unknown_dependencies),
            "nutrition_label_ios": self.nutrition_label_ios,
            "data_safety_form": self.data_safety_form,
        }


# ── SDK catalogue loader ────────────────────────────────────────────


_DEFAULT_CATALOGUE_PATH = (
    Path(__file__).resolve().parents[2]
    / "configs" / "privacy_label_sdks.yaml"
)


def _load_catalogue(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the SDK catalogue YAML. Returns ``{id: entry}``.

    Falls back to an empty dict if PyYAML is missing or the file is
    absent — the gate still runs and emits a ``no_catalogue`` status
    for the caller to handle.
    """
    target = path or _DEFAULT_CATALOGUE_PATH
    if not target.exists():
        return {}
    try:
        import yaml  # local import — yaml is a runtime dep of backend
    except ImportError:
        logger.warning("PyYAML missing, privacy_labels catalogue disabled")
        return {}
    try:
        raw = target.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to load SDK catalogue: %s", exc)
        return {}
    sdks = data.get("sdks", {}) if isinstance(data, dict) else {}
    if not isinstance(sdks, dict):
        return {}
    return sdks


# ── Dependency discovery ────────────────────────────────────────────


_PODFILE_POD_RE = re.compile(r"^\s*-\s+([A-Za-z0-9_+./-]+)", re.MULTILINE)
_SPM_PROJECT_RE = re.compile(
    r'repositoryURL\s*=\s*"([^"]+)"',
)
_ANDROID_DEP_RE = re.compile(
    r"(?:implementation|api|compileOnly|runtimeOnly|debugImplementation|"
    r"testImplementation|androidTestImplementation|kapt|ksp)"
    r"\s*[(]?\s*[\"']([^\"']+)[\"']",
)


def _discover_ios_deps(root: Path) -> list[str]:
    deps: list[str] = []
    # Podfile.lock (CocoaPods)
    pod_lock = root / "Podfile.lock"
    if pod_lock.exists():
        try:
            text = pod_lock.read_text(encoding="utf-8", errors="replace")
            # Only scan the ``PODS:`` section (lines start with "  - ").
            in_pods = False
            for line in text.splitlines():
                if line.strip() == "PODS:":
                    in_pods = True
                    continue
                if in_pods and line.strip() and not line.startswith("  "):
                    # exited PODS section
                    break
                if in_pods:
                    m = re.match(r"^\s+-\s+([A-Za-z0-9_+./-]+)", line)
                    if m:
                        deps.append(m.group(1))
        except OSError:
            pass

    # Package.resolved (SPM v2/v3 JSON)
    for cand in (root / "Package.resolved",
                 root / ".swiftpm/Package.resolved"):
        if not cand.exists():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pins = data.get("pins") or data.get("object", {}).get("pins") or []
        for pin in pins:
            # SPM v3: identity. v2: package.
            name = pin.get("identity") or pin.get("package")
            if not name and "location" in pin:
                name = pin["location"].rsplit("/", 1)[-1].removesuffix(".git")
            if name:
                deps.append(str(name))

    # SPM references embedded in pbxproj — best-effort regex.
    for pbx in root.rglob("project.pbxproj"):
        if "/build/" in str(pbx) or "/DerivedData/" in str(pbx):
            continue
        try:
            text = pbx.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _SPM_PROJECT_RE.finditer(text):
            url = m.group(1)
            name = url.rsplit("/", 1)[-1].removesuffix(".git")
            deps.append(name)

    # De-dup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for d in deps:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _discover_android_deps(root: Path) -> list[str]:
    deps: list[str] = []
    gradle_files = list(root.rglob("build.gradle")) + \
        list(root.rglob("build.gradle.kts"))
    for p in gradle_files:
        if "/build/" in str(p) or "/.gradle/" in str(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _ANDROID_DEP_RE.finditer(text):
            dep = m.group(1)
            # Normalise: drop version suffix for matching.
            coord = dep.rsplit(":", 1)[0] if dep.count(":") >= 2 else dep
            deps.append(coord)

    # De-dup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for d in deps:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _match_sdk(
    dep: str,
    catalogue: dict[str, dict[str, Any]],
) -> Optional[str]:
    """Return the SDK id whose identifiers match ``dep``, else None.

    Matching is:
        1. exact match of ``dep`` against any identifier;
        2. prefix match of any identifier against ``dep`` (covers
           versioned Maven coords and Pod sub-specs);
        3. exact match of the Maven group-part against an identifier.
    """
    lc_dep = dep.lower()
    for sdk_id, entry in catalogue.items():
        idents = entry.get("identifiers") or []
        for ident in idents:
            ident_lc = str(ident).lower()
            if lc_dep == ident_lc:
                return sdk_id
            if lc_dep.startswith(ident_lc):
                return sdk_id
            # Sub-spec: 'FirebaseAnalytics/AdIdSupport' → match 'FirebaseAnalytics'
            if "/" in lc_dep and lc_dep.split("/", 1)[0] == ident_lc:
                return sdk_id
            # Maven coord: 'com.google.firebase:firebase-analytics-ktx' vs
            # 'com.google.firebase:firebase-analytics' — prefix above
            # handles this for most; the group-only identifier falls here.
            if ":" in lc_dep and ":" not in ident_lc:
                group = lc_dep.split(":", 1)[0]
                if group == ident_lc:
                    return sdk_id
    return None


# ── Label assembly ─────────────────────────────────────────────────


def _merge_category_collections(
    entries: Iterable[dict[str, Any]],
    field_name: str,
) -> dict[str, dict[str, Any]]:
    """Given a list of catalogue entries, roll up their ``field_name``
    categories into ``{category: {purposes: [...], linked_to_user: bool,
    tracking: bool}}``.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for cat in entry.get(field_name) or []:
            slot = out.setdefault(
                cat,
                {"purposes": set(), "linked_to_user": False, "tracking": False},
            )
            slot["linked_to_user"] = (
                slot["linked_to_user"] or bool(entry.get("linked_to_user"))
            )
            slot["tracking"] = (
                slot["tracking"] or bool(entry.get("tracking"))
            )
            for p in entry.get("purposes") or []:
                slot["purposes"].add(p)

    # Freeze sets → sorted lists for determinism.
    for slot in out.values():
        slot["purposes"] = sorted(slot["purposes"])
    return out


def _build_ios_label(
    matched: list[dict[str, Any]],
    detected_sdk_names: list[str],
) -> dict[str, Any]:
    rollup = _merge_category_collections(matched, "apple_categories")
    data_collected = []
    for cat, meta in sorted(rollup.items()):
        data_collected.append({
            "category": cat,
            "purposes": meta["purposes"],
            "linked_to_user": meta["linked_to_user"],
            "used_for_tracking": meta["tracking"],
        })

    any_tracking = any(e.get("tracking") for e in matched)

    return {
        "schema_version": "apple.app_privacy.v1",
        "generated_from": "backend.mobile_compliance.privacy_labels",
        "sdks_declared": detected_sdk_names,
        "requires_app_tracking_transparency": any_tracking,
        "data_collected": data_collected,
    }


def _build_play_form(
    matched: list[dict[str, Any]],
    detected_sdk_names: list[str],
) -> dict[str, Any]:
    rollup = _merge_category_collections(matched, "play_categories")
    data_types = []
    for cat, meta in sorted(rollup.items()):
        data_types.append({
            "category": cat,
            "purposes": meta["purposes"],
            "linked_to_user": meta["linked_to_user"],
            "shared_with_third_parties": meta["tracking"],
        })

    return {
        "schema_version": "play.data_safety.v1",
        "generated_from": "backend.mobile_compliance.privacy_labels",
        "declared_sdks": detected_sdk_names,
        "data_types_collected": data_types,
        "encryption_in_transit": True,
        "data_deletion_request_url": None,
    }


# ── Public entry ────────────────────────────────────────────────────


def generate_privacy_label(
    app_path: Path | str,
    *,
    platform: str = "both",
    catalogue_path: Path | None = None,
) -> PrivacyLabelReport:
    """Generate iOS and/or Play privacy labels from detected SDKs.

    ``platform`` ∈ {"ios", "android", "both"} — restricts which
    manifests we scan. The detected SDK list is returned in either case.
    """
    if platform not in ("ios", "android", "both"):
        raise ValueError(f"Invalid platform: {platform!r}")

    root = Path(app_path).resolve()
    report = PrivacyLabelReport(app_path=str(root), platform=platform)

    if not root.exists():
        report.status = "no_manifests"
        return report

    catalogue = _load_catalogue(catalogue_path)

    deps: list[str] = []
    if platform in ("ios", "both"):
        deps.extend(_discover_ios_deps(root))
    if platform in ("android", "both"):
        deps.extend(_discover_android_deps(root))

    if not deps:
        report.status = "no_manifests"
        return report

    matched_ids: list[str] = []
    matched_entries: list[dict[str, Any]] = []
    unknown: list[str] = []
    seen_sdk_ids: set[str] = set()
    for dep in deps:
        sdk_id = _match_sdk(dep, catalogue)
        if sdk_id is None:
            unknown.append(dep)
            continue
        if sdk_id in seen_sdk_ids:
            continue
        seen_sdk_ids.add(sdk_id)
        matched_ids.append(sdk_id)
        matched_entries.append(catalogue[sdk_id])

    detected_sdk_names = [
        catalogue[i].get("display_name") or i for i in matched_ids
    ]
    report.detected_sdks = detected_sdk_names
    report.unknown_dependencies = sorted(set(unknown))

    if platform in ("ios", "both"):
        report.nutrition_label_ios = _build_ios_label(
            matched_entries, detected_sdk_names
        )
    if platform in ("android", "both"):
        report.data_safety_form = _build_play_form(
            matched_entries, detected_sdk_names
        )

    if not matched_entries and not catalogue:
        report.status = "no_catalogue"
    elif not matched_entries:
        report.status = "empty_catalogue"
    else:
        report.status = "ok"

    return report


__all__ = [
    "APPLE_CATEGORY_TAXONOMY",
    "APPLE_PURPOSE_TAXONOMY",
    "PLAY_CATEGORY_TAXONOMY",
    "PrivacyLabelReport",
    "generate_privacy_label",
]
