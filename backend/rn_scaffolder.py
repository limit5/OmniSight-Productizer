"""P9 #294 — SKILL-RN project scaffolder.

Renders a React Native 0.76+ / TypeScript 5 / Hermes project from the
templates shipped in ``configs/skills/skill-rn/scaffolds/``. Contrast
pick to SKILL-FLUTTER (same cross-platform slot, different JS runtime +
native bridge). Fourth consumer of the P0-P6 mobile framework.

The public API is deliberately identical in shape to
``backend.flutter_scaffolder`` so operators can swap
``flutter_scaffolder`` ↔ ``rn_scaffolder`` without changing
orchestration glue — the same swap the W6 SKILL-NEXTJS /
``backend.nextjs_scaffolder`` line set up for the web vertical when
SKILL-NUXT + SKILL-ASTRO came in.
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
    / "configs" / "skills" / "skill-rn"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_TEMPLATE_SUFFIX = ".j2"

_IOS_PROFILE_ID = "ios-arm64"
_ANDROID_PROFILE_ID = "android-arm64-v8a"

_DEFAULT_IOS_MIN = "16.0"
_DEFAULT_IOS_SDK = "17.5"
_DEFAULT_ANDROID_MIN = "24"
_DEFAULT_ANDROID_SDK = "35"

_PACKAGE_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
)

# Files only relevant when push=on.
_PUSH_ONLY_FILES: frozenset[str] = frozenset({
    "src/features/push/push.ts.j2",
})

# Files only relevant when payments=on.
_PAYMENTS_ONLY_FILES: frozenset[str] = frozenset({
    "src/features/payments/iap.ts.j2",
})

# Files only shipped when compliance=on.
_COMPLIANCE_GATED: frozenset[str] = frozenset({
    "AppStoreMetadata.json.j2",
    "PlayStoreMetadata.json.j2",
    "docs/play/data_safety.yaml.j2",
    "ios/RNApp/PrivacyInfo.xcprivacy",
    "ios/Podfile.lock.j2",
    "fastlane/metadata/android/en-US/title.txt.j2",
    "fastlane/metadata/android/en-US/short_description.txt.j2",
    "fastlane/metadata/android/en-US/full_description.txt.j2",
    "fastlane/metadata/ios/en-US/name.txt.j2",
    "fastlane/metadata/ios/en-US/description.txt.j2",
})


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


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-RN scaffold into ``out_dir``.

    Same idempotency / overwrite semantics as the other mobile
    scaffolders.
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
        "SKILL-RN rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot P0-P6 gate report for the rendered project.

    Covers BOTH rails + Privacy / ASC / Play gates under
    ``platform="both"``.
    """
    from backend.mobile_compliance import run_all as run_mobile_compliance
    from backend.mobile_simulator import resolve_ui_framework

    out_dir = Path(out_dir)

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

    # P2 autodetect — package.json with react-native dep is the RN
    # marker per backend.mobile_simulator.resolve_ui_framework.
    p2_framework = resolve_ui_framework(out_dir)

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

    bundle = run_mobile_compliance(out_dir, platform="both")

    return {
        "skill": "skill-rn",
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
    info = get_skill("skill-rn")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-rn dir missing"]}

    result = validate_skill("skill-rn")
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
