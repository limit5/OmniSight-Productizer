"""P7 #292 — SKILL-IOS project scaffolder.

Renders a SwiftUI 6 + Swift 5.9 iOS project from the templates shipped
in ``configs/skills/skill-ios/scaffolds/``. First mobile-vertical skill
pack and the pilot that exercises the P0-P6 framework end-to-end (same
pattern D1 SKILL-UVC applied to C5, D29 SKILL-HMI-WEBUI to C26, and W6
SKILL-NEXTJS to the web vertical).

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. This keeps Swift literals
  (``Configuration.storekit`` JSON, license text, asset catalogs) out
  of the templating path, while ``App.swift.j2`` /
  ``project.yml.j2`` can branch on knobs like ``push`` / ``storekit``
  / ``package_manager``.
* **Idempotent** — on re-render we overwrite scaffold files. Files
  outside the scaffold surface (e.g. ``App/Sources/Features/Login.swift``)
  are never touched.
* **Framework binding** — each render resolves ``ios-arm64`` from
  ``backend.platform.load_raw_profile`` so ``IPHONEOS_DEPLOYMENT_TARGET``
  reads straight from the P0 profile, not a copy.
* **Pilot report** — ``pilot_report()`` runs the P6 mobile_compliance
  bundle against the rendered project + checks the P5 ASC metadata
  shape, providing one-shot proof the P0-P6 framework holds.

Public API
----------
``ScaffoldOptions``   — knobs that parameterise the render.
``RenderOutcome``     — files written, size totals, warnings.
``render_project()``  — main entry point.
``pilot_report()``    — one-shot P0-P6 validation report.
``validate_pack()``   — registry self-check helper.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import jinja2

from backend import platform as _platform
from backend.skill_registry import get_skill, validate_skill

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-ios"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_PACKAGE_MANAGER_CHOICES = ("spm", "cocoapods", "both")

_TEMPLATE_SUFFIX = ".j2"

_PLATFORM_PROFILE_ID = "ios-arm64"

# Files that only make sense for one knob value. The scaffolder skips
# the irrelevant ones to keep the rendered tree clean.
_PUSH_ONLY_FILES: frozenset[str] = frozenset({
    "App/Sources/Push/AppDelegate.swift",
    "App/Sources/Push/PushNotificationManager.swift",
})

_STOREKIT_ONLY_FILES: frozenset[str] = frozenset({
    "App/Sources/StoreKit/StoreKitManager.swift",
    "App/Sources/StoreKit/StoreView.swift",
    "App/Sources/StoreKit/Configuration.storekit",
    "Tests/StoreKitManagerTests.swift.j2",
})

_PACKAGE_MANAGER_GATED: dict[str, str] = {
    # path -> required package_manager value (or "both")
    "Package.swift.j2": "spm",
    "Modules/Feature/Sources/FeatureCounter.swift": "spm",
    "Modules/Feature/Tests/FeatureCounterTests.swift": "spm",
    "Podfile.j2": "cocoapods",
}

_COMPLIANCE_GATED: frozenset[str] = frozenset({
    "App/Resources/PrivacyInfo.xcprivacy",
    "fastlane/metadata/en-US/privacy_url.txt",
    "AppStoreMetadata.json.j2",
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    bundle_id: str = ""
    package_manager: str = "spm"   # spm | cocoapods | both
    push: bool = True
    storekit: bool = True
    compliance: bool = True

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        # Apple bundle IDs / Xcode product names allow letters / digits /
        # hyphens / underscores only.
        clean = self.project_name.strip()
        if not all(c.isalnum() or c in ("-", "_") for c in clean):
            raise ValueError(
                "project_name must contain only letters, digits, '-' or '_' "
                f"(got {self.project_name!r})"
            )
        if self.package_manager not in _PACKAGE_MANAGER_CHOICES:
            raise ValueError(
                f"package_manager must be one of {_PACKAGE_MANAGER_CHOICES}, "
                f"got {self.package_manager!r}"
            )
        if self.bundle_id and not _looks_like_bundle_id(self.bundle_id):
            raise ValueError(
                f"bundle_id must be reverse-DNS (e.g. com.example.app), got {self.bundle_id!r}"
            )

    def resolved_bundle_id(self) -> str:
        if self.bundle_id:
            return self.bundle_id
        # Default to com.example.<project-lowercased-no-dashes>
        sanitised = self.project_name.lower().replace("-", "").replace("_", "")
        return f"com.example.{sanitised}"

    def bundle_prefix(self) -> str:
        return ".".join(self.resolved_bundle_id().split(".")[:-1]) or "com.example"


@dataclass
class RenderOutcome:
    out_dir: Path
    files_written: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    profile_binding: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "profile_binding": dict(self.profile_binding),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _looks_like_bundle_id(bundle_id: str) -> bool:
    parts = bundle_id.split(".")
    if len(parts) < 2:
        return False
    for part in parts:
        if not part:
            return False
        if not all(c.isalnum() or c in ("-", "_") for c in part):
            return False
    return True


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _should_skip(rel_path: str, opts: ScaffoldOptions) -> bool:
    # Push-gated
    if rel_path in _PUSH_ONLY_FILES and not opts.push:
        return True
    # StoreKit-gated
    if rel_path in _STOREKIT_ONLY_FILES and not opts.storekit:
        return True
    # Package manager-gated
    for marker, required in _PACKAGE_MANAGER_GATED.items():
        if rel_path == marker:
            if opts.package_manager not in (required, "both"):
                return True
    # Compliance-gated
    if not opts.compliance and rel_path in _COMPLIANCE_GATED:
        return True
    return False


def _build_jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_SCAFFOLDS_DIR)),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )


def _render_context(opts: ScaffoldOptions) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "project_name": opts.project_name,
        "bundle_id": opts.resolved_bundle_id(),
        "bundle_prefix": opts.bundle_prefix(),
        "package_manager": opts.package_manager,
        "push": opts.push,
        "storekit": opts.storekit,
        "compliance": opts.compliance,
    }

    # Resolve P0 platform profile so deployment target / SDK version
    # come from configs/platforms/ios-arm64.yaml — not duplicated in
    # the scaffold templates.
    min_os_version = "16.0"
    sdk_version = "17.5"
    target_os_version = "17.5"
    try:
        raw = _platform.load_raw_profile(_PLATFORM_PROFILE_ID)
        min_os_version = str(raw.get("min_os_version") or min_os_version)
        sdk_version = str(raw.get("sdk_version") or sdk_version)
        target_os_version = str(raw.get("target_os_version") or target_os_version)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to load %s profile, falling back to defaults: %s",
            _PLATFORM_PROFILE_ID,
            exc,
        )

    ctx["min_os_version"] = min_os_version
    ctx["sdk_version"] = sdk_version
    ctx["target_os_version"] = target_os_version
    return ctx


def _write_file(dest: Path, content: bytes | str) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        dest.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))
    dest.write_bytes(content)
    return len(content)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-IOS scaffold into ``out_dir``.

    Parameters
    ----------
    out_dir : Path
        Destination project root. Created if missing.
    options : ScaffoldOptions
        Knob values — ``project_name`` is required; everything else
        has a safe default.
    overwrite : bool
        When ``True`` (default), existing files inside the scaffold
        surface are overwritten. Files OUTSIDE the scaffold surface
        are never touched.
    """
    options.validate()
    out_dir = Path(out_dir)
    if not _SCAFFOLDS_DIR.is_dir():
        raise FileNotFoundError(f"scaffolds directory missing: {_SCAFFOLDS_DIR}")

    out_dir.mkdir(parents=True, exist_ok=True)

    env = _build_jinja_env()
    ctx = _render_context(options)

    outcome = RenderOutcome(out_dir=out_dir)
    outcome.profile_binding = {
        "platform_profile": _PLATFORM_PROFILE_ID,
        "min_os_version": ctx["min_os_version"],
        "sdk_version": ctx["sdk_version"],
        "target_os_version": ctx["target_os_version"],
    }

    for src in _iter_scaffold_files(_SCAFFOLDS_DIR):
        rel = src.relative_to(_SCAFFOLDS_DIR).as_posix()
        if _should_skip(rel, options):
            continue

        if rel.endswith(_TEMPLATE_SUFFIX):
            out_rel = rel[: -len(_TEMPLATE_SUFFIX)]
            dest = out_dir / out_rel
            if dest.exists() and not overwrite:
                outcome.warnings.append(f"skipped existing: {out_rel}")
                continue
            template = env.get_template(rel)
            rendered = template.render(**ctx)
            outcome.bytes_written += _write_file(dest, rendered)
        else:
            dest = out_dir / rel
            if dest.exists() and not overwrite:
                outcome.warnings.append(f"skipped existing: {rel}")
                continue
            outcome.bytes_written += _write_file(dest, src.read_bytes())
        outcome.files_written.append(dest)

    logger.info(
        "SKILL-IOS rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot P0-P6 gate report for the rendered project.

    Runs the P6 mobile_compliance bundle (ASC + Privacy) against the
    rendered directory and layers the P0 profile binding + P2 framework
    autodetect on top so the caller has a single view of pilot health.

    Lazy-imports ``mobile_compliance`` and ``mobile_simulator`` so a
    minimal install without the full mobile vertical can still import
    this module.
    """
    from backend.mobile_compliance import run_all as run_mobile_compliance
    from backend.mobile_simulator import resolve_ui_framework

    out_dir = Path(out_dir)

    # P0 profile binding (read straight from the YAML so the report is
    # ground truth, not the rendered scaffold's interpretation).
    profile: dict[str, Any] = {}
    try:
        raw = _platform.load_raw_profile(_PLATFORM_PROFILE_ID)
        profile = {
            "platform": _PLATFORM_PROFILE_ID,
            "min_os_version": str(raw.get("min_os_version", "")),
            "sdk_version": str(raw.get("sdk_version", "")),
            "target_os_version": str(raw.get("target_os_version", "")),
        }
    except Exception as exc:  # noqa: BLE001
        profile = {"platform": _PLATFORM_PROFILE_ID, "error": str(exc)}

    # P2 simulate-track autodetect — pass the iOS platform hint so the
    # detection works even before XcodeGen has materialised the
    # .xcodeproj at build time.
    p2_framework = resolve_ui_framework(out_dir, mobile_platform="ios")

    # P5 ASC metadata sanity (only if compliance gates are on; off-mode
    # explicitly suppresses AppStoreMetadata.json).
    p5_metadata: dict[str, Any] = {"present": False}
    metadata_path = out_dir / "AppStoreMetadata.json"
    if metadata_path.is_file():
        try:
            doc = json.loads(metadata_path.read_text(encoding="utf-8"))
            p5_metadata = {
                "present": True,
                "bundle_id_matches": doc.get("bundle_id") == options.resolved_bundle_id(),
                "schema_version": doc.get("schema_version"),
                "has_age_rating": "age_rating" in doc,
                "uses_idfa": doc.get("uses_idfa"),
            }
        except (OSError, json.JSONDecodeError) as exc:
            p5_metadata = {"present": True, "parse_error": str(exc)}

    # P6 compliance bundle — iOS-only scan keeps Play gate in skipped
    # state (correct: this is an iOS-only pilot pack).
    bundle = run_mobile_compliance(out_dir, platform="ios")

    return {
        "skill": "skill-ios",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "bundle_id": options.resolved_bundle_id(),
            "package_manager": options.package_manager,
            "push": options.push,
            "storekit": options.storekit,
            "compliance": options.compliance,
        },
        "p0_profile": profile,
        "p2_simulate_autodetect": p2_framework,
        "p5_asc_metadata": p5_metadata,
        "p6_compliance": bundle.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-ios pack is complete.

    Returns a dict with the skill registry validation result. Used by
    ``test_skill_ios.py`` as a living spec — a missing artifact or
    broken manifest trips the test immediately.
    """
    info = get_skill("skill-ios")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-ios dir missing"]}

    result = validate_skill("skill-ios")
    return {
        "installed": True,
        "ok": result.ok,
        "skill_name": result.skill_name,
        "issues": [{"level": i.level, "message": i.message} for i in result.issues],
        "artifact_kinds": sorted(info.artifact_kinds),
        "has_manifest": info.has_manifest,
        "has_tasks_yaml": info.has_tasks_yaml,
    }


__all__ = [
    "RenderOutcome",
    "ScaffoldOptions",
    "_PLATFORM_PROFILE_ID",
    "_PUSH_ONLY_FILES",
    "_SCAFFOLDS_DIR",
    "_SKILL_DIR",
    "_STOREKIT_ONLY_FILES",
    "_render_context",
    "pilot_report",
    "render_project",
    "validate_pack",
]
