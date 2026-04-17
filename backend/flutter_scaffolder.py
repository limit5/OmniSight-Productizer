"""P9 #294 — SKILL-FLUTTER project scaffolder.

Renders a Flutter 3.22+ / Dart 3.4+ project from the templates shipped
in ``configs/skills/skill-flutter/scaffolds/``. Third mobile-vertical
skill pack and the FIRST cross-platform consumer of the P0-P6 framework
(P7 SKILL-IOS was the iOS-only pilot; P8 SKILL-ANDROID was the
Android-only n=2). The Python entry points mirror
``backend.ios_scaffolder`` and ``backend.android_scaffolder`` so
operators moving between native + cross-platform live under one mental
model.

Design
------
* **Dual-profile binding** — ``_render_context`` resolves BOTH
  ``ios-arm64`` and ``android-arm64-v8a`` profiles from
  ``backend.platform.load_raw_profile`` so ``min_os_version`` /
  ``sdk_version`` come from the P0 YAMLs for each rail.
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte.
* **Idempotent** — on re-render we overwrite scaffold files; files
  outside the scaffold surface are never touched.
* **Pilot report** — ``pilot_report()`` returns a merged bundle
  covering both iOS + Android P0 profile binding, P2 framework
  autodetect, P5 ASC + Play metadata sanity, and P6 mobile_compliance
  with ``platform="both"``.

Public API (identical shape to skill-rn and skill-ios/android)
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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import jinja2

from backend import platform as _platform
from backend.skill_registry import get_skill, validate_skill

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-flutter"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_TEMPLATE_SUFFIX = ".j2"

_IOS_PROFILE_ID = "ios-arm64"
_ANDROID_PROFILE_ID = "android-arm64-v8a"

# Cross-platform fallback defaults — used only when a P0 profile is
# absent (sandbox / minimal install). The real values come straight
# from the YAML under configs/platforms/.
_DEFAULT_IOS_MIN = "16.0"
_DEFAULT_IOS_SDK = "17.5"
_DEFAULT_ANDROID_MIN = "24"
_DEFAULT_ANDROID_SDK = "35"

# Same reverse-DNS shape as Android's Play Developer validator + Apple
# CFBundleIdentifier rules — shared across both stores.
_PACKAGE_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
)

# Files only relevant when push=on. Skipped (template + output) when off.
_PUSH_ONLY_FILES: frozenset[str] = frozenset({
    "lib/features/push/push_service.dart.j2",
})

# Files only relevant when payments=on.
_PAYMENTS_ONLY_FILES: frozenset[str] = frozenset({
    "lib/features/billing/iap_service.dart.j2",
})

# Files only shipped when compliance=on. Mirrors the skill-android /
# skill-ios pattern so the knob flips one consistent switch across both
# store-submission surfaces.
_COMPLIANCE_GATED: frozenset[str] = frozenset({
    "AppStoreMetadata.json.j2",
    "PlayStoreMetadata.json.j2",
    "docs/play/data_safety.yaml.j2",
    "ios/Runner/PrivacyInfo.xcprivacy",
    "ios/Podfile.lock.j2",
    "fastlane/metadata/android/en-US/title.txt.j2",
    "fastlane/metadata/android/en-US/short_description.txt.j2",
    "fastlane/metadata/android/en-US/full_description.txt.j2",
    "fastlane/metadata/ios/en-US/name.txt.j2",
    "fastlane/metadata/ios/en-US/description.txt.j2",
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    package_id: str = ""
    push: bool = True
    payments: bool = True
    compliance: bool = True

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        clean = self.project_name.strip()
        # Flutter + Dart + Kotlin + Swift all accept letters / digits /
        # underscores as class/package name parts; hyphens are not legal
        # on Android gradle `namespace` and iOS Xcode target names.
        if not all(c.isalnum() or c == "_" for c in clean):
            raise ValueError(
                "project_name must contain only letters, digits or '_' "
                f"(got {self.project_name!r})"
            )
        if not clean[0].isalpha():
            raise ValueError(
                f"project_name must start with a letter (got {self.project_name!r})"
            )
        if self.package_id and not _PACKAGE_ID_RE.match(self.package_id):
            raise ValueError(
                "package_id must be reverse-DNS (e.g. com.example.app); "
                f"got {self.package_id!r}"
            )

    def resolved_package_id(self) -> str:
        if self.package_id:
            return self.package_id
        sanitised = self.project_name.lower().replace("_", "").replace("-", "")
        return f"com.example.{sanitised}"

    def package_prefix(self) -> str:
        return ".".join(self.resolved_package_id().split(".")[:-1]) or "com.example"


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


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _should_skip(rel_path: str, opts: ScaffoldOptions) -> bool:
    if rel_path in _PUSH_ONLY_FILES and not opts.push:
        return True
    if rel_path in _PAYMENTS_ONLY_FILES and not opts.payments:
        return True
    if rel_path in _COMPLIANCE_GATED and not opts.compliance:
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
        "package_id": opts.resolved_package_id(),
        "package_prefix": opts.package_prefix(),
        "push": opts.push,
        "payments": opts.payments,
        "compliance": opts.compliance,
    }

    # Resolve BOTH P0 profiles — this is the cross-platform twist. Both
    # rails must bind to their own YAML so the scaffold's surfaces
    # (Podfile platform :ios + gradle minSdk) stay in lockstep with the
    # profiles the rest of the framework reads.
    min_os_ios = _DEFAULT_IOS_MIN
    sdk_ios = _DEFAULT_IOS_SDK
    target_ios = _DEFAULT_IOS_SDK
    try:
        raw_ios = _platform.load_raw_profile(_IOS_PROFILE_ID)
        min_os_ios = str(raw_ios.get("min_os_version") or min_os_ios)
        sdk_ios = str(raw_ios.get("sdk_version") or sdk_ios)
        target_ios = str(raw_ios.get("target_os_version") or target_ios)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to load %s profile, falling back to defaults: %s",
            _IOS_PROFILE_ID, exc,
        )

    min_os_android = _DEFAULT_ANDROID_MIN
    sdk_android = _DEFAULT_ANDROID_SDK
    target_android = _DEFAULT_ANDROID_SDK
    try:
        raw_android = _platform.load_raw_profile(_ANDROID_PROFILE_ID)
        min_os_android = str(raw_android.get("min_os_version") or min_os_android)
        sdk_android = str(raw_android.get("sdk_version") or sdk_android)
        target_android = str(raw_android.get("target_os_version") or target_android)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to load %s profile, falling back to defaults: %s",
            _ANDROID_PROFILE_ID, exc,
        )

    ctx["min_os_version_ios"] = min_os_ios
    ctx["sdk_version_ios"] = sdk_ios
    ctx["target_os_version_ios"] = target_ios
    ctx["min_os_version_android"] = min_os_android
    ctx["sdk_version_android"] = sdk_android
    ctx["target_os_version_android"] = target_android
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
    """Render the SKILL-FLUTTER scaffold into ``out_dir``.

    Parameters
    ----------
    out_dir : Path
        Destination project root. Created if missing.
    options : ScaffoldOptions
        Knob values — ``project_name`` is required; everything else has
        a safe default.
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
        "ios_profile": _IOS_PROFILE_ID,
        "android_profile": _ANDROID_PROFILE_ID,
        "min_os_version_ios": ctx["min_os_version_ios"],
        "sdk_version_ios": ctx["sdk_version_ios"],
        "min_os_version_android": ctx["min_os_version_android"],
        "sdk_version_android": ctx["sdk_version_android"],
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
        "SKILL-FLUTTER rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot P0-P6 gate report for the rendered project.

    Unlike the iOS- or Android-only pilot_report from P7 / P8 this one
    covers BOTH rails — the cross-platform pack has to pass on both
    profiles and against ``mobile_compliance.run_all(platform="both")``.

    Lazy-imports ``mobile_compliance`` and ``mobile_simulator`` so a
    minimal install without the full mobile vertical can still import
    this module.
    """
    from backend.mobile_compliance import run_all as run_mobile_compliance
    from backend.mobile_simulator import resolve_ui_framework

    out_dir = Path(out_dir)

    # P0 profile binding (read straight from the YAMLs so the report is
    # ground truth, not the rendered scaffold's interpretation).
    ios_profile: dict[str, Any] = {}
    try:
        raw = _platform.load_raw_profile(_IOS_PROFILE_ID)
        ios_profile = {
            "platform": _IOS_PROFILE_ID,
            "min_os_version": str(raw.get("min_os_version", "")),
            "sdk_version": str(raw.get("sdk_version", "")),
            "target_os_version": str(raw.get("target_os_version", "")),
        }
    except Exception as exc:  # noqa: BLE001
        ios_profile = {"platform": _IOS_PROFILE_ID, "error": str(exc)}

    android_profile: dict[str, Any] = {}
    try:
        raw = _platform.load_raw_profile(_ANDROID_PROFILE_ID)
        android_profile = {
            "platform": _ANDROID_PROFILE_ID,
            "min_os_version": str(raw.get("min_os_version", "")),
            "sdk_version": str(raw.get("sdk_version", "")),
            "target_os_version": str(raw.get("target_os_version", "")),
        }
    except Exception as exc:  # noqa: BLE001
        android_profile = {"platform": _ANDROID_PROFILE_ID, "error": str(exc)}

    # P2 simulate-track autodetect — no platform hint because Flutter's
    # pubspec.yaml marker wins over both native platform markers;
    # passing a hint here would silently mask a scaffold that forgot
    # pubspec.yaml.
    p2_framework = resolve_ui_framework(out_dir)

    # P5 ASC + Play metadata sanity — both files ride on compliance=on.
    p5_asc: dict[str, Any] = {"present": False}
    asc_path = out_dir / "AppStoreMetadata.json"
    if asc_path.is_file():
        try:
            doc = json.loads(asc_path.read_text(encoding="utf-8"))
            p5_asc = {
                "present": True,
                "package_matches": doc.get("bundle_id") == options.resolved_package_id(),
                "schema_version": doc.get("schema_version"),
                "has_age_rating": "age_rating" in doc,
                "uses_idfa": doc.get("uses_idfa"),
            }
        except (OSError, json.JSONDecodeError) as exc:
            p5_asc = {"present": True, "parse_error": str(exc)}

    p5_play: dict[str, Any] = {"present": False}
    play_path = out_dir / "PlayStoreMetadata.json"
    if play_path.is_file():
        try:
            doc = json.loads(play_path.read_text(encoding="utf-8"))
            p5_play = {
                "present": True,
                "package_matches": doc.get("package_name") == options.resolved_package_id(),
                "schema_version": doc.get("schema_version"),
                "has_content_rating": "content_rating" in doc,
                "has_data_safety_path": "data_safety_form_path" in doc,
            }
        except (OSError, json.JSONDecodeError) as exc:
            p5_play = {"present": True, "parse_error": str(exc)}

    # P6 compliance — platform="both" runs ASC + Play + Privacy gates,
    # so a cross-platform scaffold that breaks either rail shows up here.
    bundle = run_mobile_compliance(out_dir, platform="both")

    return {
        "skill": "skill-flutter",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "package_id": options.resolved_package_id(),
            "push": options.push,
            "payments": options.payments,
            "compliance": options.compliance,
        },
        "p0_ios_profile": ios_profile,
        "p0_android_profile": android_profile,
        "p2_simulate_autodetect": p2_framework,
        "p5_asc_metadata": p5_asc,
        "p5_play_metadata": p5_play,
        "p6_compliance": bundle.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-flutter pack is complete.

    Returns a dict with the skill registry validation result. Used by
    ``test_skill_flutter.py`` as a living spec — a missing artifact or
    broken manifest trips the test immediately.
    """
    info = get_skill("skill-flutter")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-flutter dir missing"]}

    result = validate_skill("skill-flutter")
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
    "_ANDROID_PROFILE_ID",
    "_COMPLIANCE_GATED",
    "_IOS_PROFILE_ID",
    "_PAYMENTS_ONLY_FILES",
    "_PUSH_ONLY_FILES",
    "_SCAFFOLDS_DIR",
    "_SKILL_DIR",
    "_render_context",
    "pilot_report",
    "render_project",
    "validate_pack",
]
