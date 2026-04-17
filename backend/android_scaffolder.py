"""P8 #293 — SKILL-ANDROID project scaffolder.

Renders a Jetpack Compose + Kotlin 2.0 Android project from the
templates shipped in ``configs/skills/skill-android/scaffolds/``.
Second mobile-vertical skill pack — n=2 consumer of the P0-P6 framework
(P7 SKILL-IOS was the pilot). The Python entry points mirror
``backend.ios_scaffolder`` so operators moving between iOS and Android
deal with one mental model.

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. This keeps Kotlin bodies,
  plist-style XML and the gradle wrapper properties out of the
  templating path, while knob-dependent files (``AndroidManifest.xml``,
  ``app/build.gradle.kts``, ``PlayStoreMetadata.json``) can branch on
  ``push`` / ``billing`` / ``compliance``.
* **Idempotent** — on re-render we overwrite scaffold files. Files
  outside the scaffold surface are never touched.
* **Framework binding** — each render resolves ``android-arm64-v8a``
  from ``backend.platform.load_raw_profile`` so ``minSdk`` / ``targetSdk``
  read straight from the P0 profile, not a copy.
* **Pilot report** — ``pilot_report()`` runs the P6 mobile_compliance
  bundle against the rendered project + checks the P5 Play metadata
  shape, providing one-shot proof the P0-P6 framework holds for the
  n=2 consumer.

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
    / "configs" / "skills" / "skill-android"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_TEMPLATE_SUFFIX = ".j2"

_PLATFORM_PROFILE_ID = "android-arm64-v8a"

# Default floor / compile SDK when the P0 profile can't be read (e.g.
# minimal install in a sandbox).
_DEFAULT_MIN_SDK = "24"
_DEFAULT_TARGET_SDK = "35"

# Reverse-DNS shape for Android applicationId. Same pattern the Play
# Developer credentials validator enforces.
_PACKAGE_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
)

# Files only relevant when push=on. Skipped (template + path) when off.
_PUSH_ONLY_FILES: frozenset[str] = frozenset({
    "app/src/main/java/com/omnisight/pilot/push/FcmMessagingService.kt.j2",
    "app/src/main/java/com/omnisight/pilot/push/PushRegistrar.kt.j2",
})

# Files only relevant when billing=on.
_BILLING_ONLY_FILES: frozenset[str] = frozenset({
    "app/src/main/java/com/omnisight/pilot/billing/BillingClientManager.kt",
    "app/src/main/java/com/omnisight/pilot/billing/BillingScreen.kt",
    "app/src/test/java/com/omnisight/pilot/BillingClientManagerTest.kt",
})

# Files only shipped when compliance=on.
_COMPLIANCE_GATED: frozenset[str] = frozenset({
    "PlayStoreMetadata.json.j2",
    "docs/play/data_safety.yaml.j2",
    "fastlane/metadata/android/en-US/full_description.txt.j2",
    "fastlane/metadata/android/en-US/short_description.txt.j2",
    "fastlane/metadata/android/en-US/title.txt.j2",
    "fastlane/metadata/android/en-US/video.txt",
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    package_id: str = ""
    push: bool = True
    billing: bool = True
    compliance: bool = True

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        # Android activity / Kotlin class names allow letters / digits
        # / underscores; hyphens are not legal identifiers.
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
        # Default to com.example.<lowercased-no-underscores>
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
    if rel_path in _BILLING_ONLY_FILES and not opts.billing:
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
        "billing": opts.billing,
        "compliance": opts.compliance,
    }

    # Resolve P0 platform profile so minSdk / targetSdk / sdk_version
    # come from configs/platforms/android-arm64-v8a.yaml — not a
    # duplicate pinned in the scaffold templates.
    min_os_version = _DEFAULT_MIN_SDK
    sdk_version = _DEFAULT_TARGET_SDK
    target_os_version = _DEFAULT_TARGET_SDK
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
    """Render the SKILL-ANDROID scaffold into ``out_dir``.

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
        "SKILL-ANDROID rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot P0-P6 gate report for the rendered project.

    Runs the P6 mobile_compliance bundle (Play + Privacy) against the
    rendered directory and layers the P0 profile binding + P2 framework
    autodetect on top so the caller has a single view of pilot health
    for the n=2 mobile consumer.

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

    # P2 simulate-track autodetect — pass the android platform hint so
    # the detection works when the scaffold has only root-level gradle
    # (no nested android/ folder a Flutter project would have).
    p2_framework = resolve_ui_framework(out_dir, mobile_platform="android")

    # P5 Play metadata sanity (only if compliance gates are on;
    # off-mode explicitly suppresses PlayStoreMetadata.json).
    p5_metadata: dict[str, Any] = {"present": False}
    metadata_path = out_dir / "PlayStoreMetadata.json"
    if metadata_path.is_file():
        try:
            doc = json.loads(metadata_path.read_text(encoding="utf-8"))
            p5_metadata = {
                "present": True,
                "package_matches": doc.get("package_name") == options.resolved_package_id(),
                "schema_version": doc.get("schema_version"),
                "has_content_rating": "content_rating" in doc,
                "has_data_safety_path": "data_safety_form_path" in doc,
            }
        except (OSError, json.JSONDecodeError) as exc:
            p5_metadata = {"present": True, "parse_error": str(exc)}

    # P6 compliance bundle — android-only scan keeps ASC gate in skipped
    # state (correct: this is an Android-only pilot pack).
    bundle = run_mobile_compliance(out_dir, platform="android")

    return {
        "skill": "skill-android",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "package_id": options.resolved_package_id(),
            "push": options.push,
            "billing": options.billing,
            "compliance": options.compliance,
        },
        "p0_profile": profile,
        "p2_simulate_autodetect": p2_framework,
        "p5_play_metadata": p5_metadata,
        "p6_compliance": bundle.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-android pack is complete.

    Returns a dict with the skill registry validation result. Used by
    ``test_skill_android.py`` as a living spec — a missing artifact or
    broken manifest trips the test immediately.
    """
    info = get_skill("skill-android")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-android dir missing"]}

    result = validate_skill("skill-android")
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
    "_BILLING_ONLY_FILES",
    "_COMPLIANCE_GATED",
    "_PLATFORM_PROFILE_ID",
    "_PUSH_ONLY_FILES",
    "_SCAFFOLDS_DIR",
    "_SKILL_DIR",
    "_render_context",
    "pilot_report",
    "render_project",
    "validate_pack",
]
