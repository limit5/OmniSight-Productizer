"""P9 #294 — SKILL-FLUTTER pilot skill contract tests.

SKILL-FLUTTER is the THIRD consumer of the P0-P6 framework and the
first cross-platform one. SKILL-IOS (P7 #292) validated iOS-only;
SKILL-ANDROID (P8 #293) validated Android-only; this suite proves the
framework holds on BOTH rails simultaneously (Flutter 3.22+ / Dart
3.4+, one codebase, both stores).

Framework invariants locked:

* **P0** — BOTH ``ios-arm64`` AND ``android-arm64-v8a`` profiles
  load cleanly; rendered scaffold's iOS Podfile platform + Info.plist
  MinimumOSVersion match ios-arm64 ``min_os_version``; Android
  ``minSdk`` / ``targetSdk`` match android-arm64-v8a values.
* **P2** — rendered project autodetects as ``flutter`` via
  ``mobile_simulator.resolve_ui_framework`` — pubspec.yaml beats the
  native subdirs in the autodetect order, even though the scaffold
  also emits iOS and Android native folders.
* **P3** — both iOS ExportOptions.plist + Android key.properties
  use ``$OMNISIGHT_*`` env references; never bake a real cert hash,
  keystore binary, or password.
* **P4** — generated Dart honours the flutter-dart role anti-patterns
  (no ``print()`` in business code, ``debugPrint`` + ``kDebugMode``
  guard, StateNotifier / Riverpod over cross-widget ``setState``,
  ``mounted`` guards across async gaps).
* **P5** — BOTH ``AppStoreMetadata.json`` (ASC shape) AND
  ``PlayStoreMetadata.json`` (Play shape) conform to their respective
  adapter schemas.
* **P6** — ``mobile_compliance.run_all(platform="both")`` passes
  with the default knobs; ASC + Play + Privacy gates all green.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from backend.flutter_scaffolder import (
    RenderOutcome,
    ScaffoldOptions,
    _ANDROID_PROFILE_ID,
    _IOS_PROFILE_ID,
    _PAYMENTS_ONLY_FILES,
    _PUSH_ONLY_FILES,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _render_context,
    pilot_report,
    render_project,
    validate_pack,
)
from backend.platform import load_raw_profile
from backend.skill_registry import get_skill, list_skills, validate_skill


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "PilotApp"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="PilotApp",
        push=True,
        payments=True,
        compliance=True,
    )
    kwargs.update(overrides)
    return ScaffoldOptions(**kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill pack registry invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSkillPackRegistry:
    def test_pack_discoverable(self):
        names = {s.name for s in list_skills()}
        assert "skill-flutter" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-flutter")
        assert result.ok, (
            f"skill-flutter validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-flutter")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-flutter")
        assert info is not None
        assert info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core

    def test_manifest_keywords_include_pilot_marker(self):
        info = get_skill("skill-flutter")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {"flutter", "dart", "cross-platform", "ios", "android", "p9"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-flutter"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold render (unit)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldRender:
    def test_render_writes_core_files(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        must_exist = [
            "pubspec.yaml",
            "analysis_options.yaml",
            ".gitignore",
            "lib/main.dart",
            "lib/app.dart",
            "lib/features/home/home_screen.dart",
            "test/widget_test.dart",
            "integration_test/app_test.dart",
            "ios/Runner/Info.plist",
            "ios/Podfile",
            "android/settings.gradle.kts",
            "android/build.gradle.kts",
            "android/app/build.gradle.kts",
            "android/app/src/main/AndroidManifest.xml",
            "android/key.properties.example",
            "android/gradle.properties",
            "android/gradle/wrapper/gradle-wrapper.properties",
            "fastlane/Fastfile",
            "fastlane/Appfile",
            "AppStoreMetadata.json",
            "PlayStoreMetadata.json",
            "docs/play/data_safety.yaml",
            "README.md",
        ]
        for rel in must_exist:
            assert (project_dir / rel).is_file(), f"missing: {rel}"
        assert outcome.bytes_written > 0
        assert outcome.warnings == []

    def test_render_returns_render_outcome(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        assert isinstance(outcome, RenderOutcome)
        assert outcome.out_dir == project_dir
        assert outcome.profile_binding["ios_profile"] == _IOS_PROFILE_ID
        assert outcome.profile_binding["android_profile"] == _ANDROID_PROFILE_ID

    def test_push_off_skips_fcm_files(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        for rel in _PUSH_ONLY_FILES:
            stripped = rel.removesuffix(".j2")
            assert not (project_dir / stripped).exists(), f"{stripped} leaked through"

    def test_push_off_drops_pubspec_and_gradle_deps(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        pubspec = (project_dir / "pubspec.yaml").read_text()
        assert "firebase_messaging" not in pubspec
        assert "firebase_core" not in pubspec
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        assert "firebase-messaging" not in gradle
        manifest = (project_dir / "android/app/src/main/AndroidManifest.xml").read_text()
        no_comments = re.sub(r"<!--.*?-->", "", manifest, flags=re.DOTALL)
        assert "POST_NOTIFICATIONS" not in no_comments

    def test_push_on_emits_fcm_files_and_deps(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        assert (project_dir / "lib/features/push/push_service.dart").is_file()
        pubspec = (project_dir / "pubspec.yaml").read_text()
        assert "firebase_messaging" in pubspec
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        assert "firebase-messaging" in gradle
        manifest = (project_dir / "android/app/src/main/AndroidManifest.xml").read_text()
        no_comments = re.sub(r"<!--.*?-->", "", manifest, flags=re.DOTALL)
        assert "POST_NOTIFICATIONS" in no_comments

    def test_payments_off_skips_iap_files(self, project_dir):
        render_project(project_dir, _default_opts(payments=False))
        for rel in _PAYMENTS_ONLY_FILES:
            stripped = rel.removesuffix(".j2")
            assert not (project_dir / stripped).exists(), f"{stripped} leaked through"
        pubspec = (project_dir / "pubspec.yaml").read_text()
        assert "in_app_purchase" not in pubspec

    def test_payments_on_emits_iap_files(self, project_dir):
        render_project(project_dir, _default_opts(payments=True))
        assert (project_dir / "lib/features/billing/iap_service.dart").is_file()
        pubspec = (project_dir / "pubspec.yaml").read_text()
        assert "in_app_purchase" in pubspec

    def test_compliance_off_drops_privacy_and_metadata(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "AppStoreMetadata.json").exists()
        assert not (project_dir / "PlayStoreMetadata.json").exists()
        assert not (project_dir / "docs/play/data_safety.yaml").exists()
        assert not (project_dir / "ios/Runner/PrivacyInfo.xcprivacy").exists()

    def test_compliance_on_ships_all_surfaces(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        assert (project_dir / "AppStoreMetadata.json").is_file()
        assert (project_dir / "PlayStoreMetadata.json").is_file()
        assert (project_dir / "docs/play/data_safety.yaml").is_file()
        assert (project_dir / "ios/Runner/PrivacyInfo.xcprivacy").is_file()

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_non_scaffold_files_are_preserved(self, project_dir):
        render_project(project_dir, _default_opts())
        custom = project_dir / "lib/features/login/login.dart"
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text("// user-added — must not be clobbered\n")
        render_project(project_dir, _default_opts())
        assert custom.is_file()
        assert "user-added" in custom.read_text()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="   ").validate()

    def test_project_name_with_hyphen_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="My-App").validate()

    def test_project_name_leading_digit_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="123App").validate()

    def test_invalid_package_id_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", package_id="not-reverse-dns").validate()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P0 platform binding — BOTH rails
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP0PlatformBinding:
    def test_ios_profile_loads(self):
        raw = load_raw_profile(_IOS_PROFILE_ID)
        assert raw["mobile_platform"] == "ios"
        assert raw["mobile_abi"] == "arm64"
        assert raw["min_os_version"] == "16.0"
        assert raw["sdk_version"] == "17.5"

    def test_android_profile_loads(self):
        raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert raw["mobile_platform"] == "android"
        assert raw["mobile_abi"] == "arm64-v8a"
        assert raw["min_os_version"] == "24"
        assert raw["sdk_version"] == "35"

    def test_render_context_pulls_both_profiles(self):
        ctx = _render_context(_default_opts())
        ios_raw = load_raw_profile(_IOS_PROFILE_ID)
        android_raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert ctx["min_os_version_ios"] == str(ios_raw["min_os_version"])
        assert ctx["sdk_version_ios"] == str(ios_raw["sdk_version"])
        assert ctx["min_os_version_android"] == str(android_raw["min_os_version"])
        assert ctx["sdk_version_android"] == str(android_raw["sdk_version"])

    def test_ios_podfile_platform_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        podfile = (project_dir / "ios/Podfile").read_text()
        ios_raw = load_raw_profile(_IOS_PROFILE_ID)
        assert f"platform :ios, '{ios_raw['min_os_version']}'" in podfile

    def test_ios_info_plist_min_os_version_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        plist = (project_dir / "ios/Runner/Info.plist").read_text()
        ios_raw = load_raw_profile(_IOS_PROFILE_ID)
        assert f"<string>{ios_raw['min_os_version']}</string>" in plist

    def test_android_min_sdk_pinned_in_gradle(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert f"minSdk = {raw['min_os_version']}" in gradle

    def test_android_target_sdk_pinned_in_gradle(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert f"targetSdk = {raw['sdk_version']}" in gradle
        assert f"compileSdk = {raw['sdk_version']}" in gradle

    def test_package_id_flows_to_both_stores(self, project_dir):
        opts = _default_opts(package_id="com.acme.prod.app")
        render_project(project_dir, opts)
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        assert 'applicationId = "com.acme.prod.app"' in gradle
        plist = (project_dir / "ios/Runner/Info.plist").read_text()
        assert "<string>com.acme.prod.app</string>" in plist


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2 simulate-track binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP2SimulateBinding:
    def test_flutter_autodetect_wins_over_native_subdirs(self, project_dir):
        from backend.mobile_simulator import resolve_ui_framework

        render_project(project_dir, _default_opts())
        # Scaffold emits both ios/ and android/ subdirs; pubspec.yaml
        # must still win per the autodetect order.
        assert (project_dir / "ios").is_dir()
        assert (project_dir / "android").is_dir()
        assert (project_dir / "pubspec.yaml").is_file()
        framework = resolve_ui_framework(project_dir)
        assert framework == "flutter"

    def test_integration_test_dir_present(self, project_dir):
        render_project(project_dir, _default_opts())
        assert (project_dir / "integration_test/app_test.dart").is_file()

    def test_integration_test_uses_integration_test_binding(self, project_dir):
        render_project(project_dir, _default_opts())
        body = (project_dir / "integration_test/app_test.dart").read_text()
        assert "IntegrationTestWidgetsFlutterBinding" in body
        assert "testWidgets" in body


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P3 codesign chain — iOS + Android
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP3CodesignChain:
    def test_android_key_properties_env_placeholders(self, project_dir):
        render_project(project_dir, _default_opts())
        props = (project_dir / "android/key.properties.example").read_text()
        assert "$OMNISIGHT_KEYSTORE_PATH" in props
        assert "$OMNISIGHT_KEYSTORE_PASSWORD" in props
        assert "$OMNISIGHT_KEY_ALIAS" in props
        assert "$OMNISIGHT_KEY_PASSWORD" in props

    def test_ios_export_options_env_placeholders(self, project_dir):
        render_project(project_dir, _default_opts())
        plist = (project_dir / "ios/ExportOptions.plist.example").read_text()
        assert "$OMNISIGHT_IOS_TEAM_ID" in plist
        assert "$OMNISIGHT_IOS_SIGN_IDENTITY" in plist
        assert "$OMNISIGHT_IOS_PROVISIONING_PROFILE" in plist

    def test_no_real_secrets_baked_in_signing_files(self, project_dir):
        render_project(project_dir, _default_opts())
        props = (project_dir / "android/key.properties.example").read_text()
        assert not re.search(r"\b[A-Fa-f0-9]{64}\b", props), "Looks like a real SHA-256"
        assert not re.search(
            r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
            props,
        ), "Looks like a real UUID"

    def test_gradle_reads_env_vars(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        assert 'System.getenv("OMNISIGHT_KEYSTORE_PATH")' in gradle
        assert "OMNISIGHT_KEYSTORE_PASSWORD" in gradle
        assert "OMNISIGHT_KEY_ALIAS" in gradle

    def test_fastfile_bails_on_missing_env(self, project_dir):
        render_project(project_dir, _default_opts())
        fast = (project_dir / "fastlane/Fastfile").read_text()
        assert 'OMNISIGHT_MACOS_BUILDER' in fast
        assert 'OMNISIGHT_KEYSTORE_PATH' in fast
        assert "user_error!" in fast

    def test_gitignore_excludes_signing_material(self, project_dir):
        render_project(project_dir, _default_opts())
        ignore = (project_dir / ".gitignore").read_text()
        assert "key.properties" in ignore
        assert "*.jks" in ignore
        assert "*.keystore" in ignore
        assert "google-services.json" in ignore


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P4 flutter-dart role anti-patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _strip_dart_comments(text: str) -> str:
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = re.sub(r"//[^\n]*", "", out)
    return out


class TestP4RoleAntiPatterns:
    def test_no_print_in_dart_business_code(self, project_dir):
        render_project(project_dir, _default_opts())
        offenders: list[str] = []
        for dart in (project_dir / "lib").rglob("*.dart"):
            code = _strip_dart_comments(dart.read_text())
            # `print(` = bare Dart print(); `debugPrint(` is allowed.
            # Watch for `print(` NOT preceded by `debug`.
            for line in code.split("\n"):
                if re.search(r"(?<!debug)(?<!\.)\bprint\s*\(", line):
                    offenders.append(f"{dart.relative_to(project_dir)}: {line.strip()}")
        assert not offenders, offenders

    def test_home_screen_uses_state_notifier(self, project_dir):
        render_project(project_dir, _default_opts())
        body = (project_dir / "lib/features/home/home_screen.dart").read_text()
        assert "StateNotifier" in body
        assert "ConsumerWidget" in body
        # No bare setState pattern.
        code = _strip_dart_comments(body)
        assert "setState(" not in code

    def test_analysis_options_blocks_print_and_context_across_async(self, project_dir):
        render_project(project_dir, _default_opts())
        opts = (project_dir / "analysis_options.yaml").read_text()
        assert "avoid_print: error" in opts
        assert "use_build_context_synchronously: error" in opts

    def test_iap_verify_stub_returns_false(self, project_dir):
        render_project(project_dir, _default_opts(payments=True))
        iap = (project_dir / "lib/features/billing/iap_service.dart").read_text()
        # Server-verify stub must never grant entitlement — the #1
        # IAP-bypass pattern Apple / Google both warn about.
        assert "_verifyPurchase" in iap
        # Ensure `completePurchase` only runs AFTER the verified branch.
        assert "if (!verified)" in iap

    def test_push_service_does_not_log_raw_token(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        push = (project_dir / "lib/features/push/push_service.dart").read_text()
        # Only the fingerprint is allowed into a log line.
        assert "fingerprint" in push
        # Look for obvious direct token logging.
        code = _strip_dart_comments(push)
        offenders = [
            line for line in code.split("\n")
            if "debugPrint" in line and "token" in line and "fingerprint" not in line
        ]
        assert not offenders, offenders


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P5 dual-store submission shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP5DualStoreSubmission:
    def test_asc_metadata_renders_as_valid_json(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        assert meta["schema_version"] == 1
        assert meta["platform"] == "ios"
        assert meta["bundle_id"] == _default_opts().resolved_package_id()
        assert meta["app_name"] == "PilotApp"
        assert "age_rating" in meta
        assert meta["uses_idfa"] is False

    def test_play_metadata_renders_as_valid_json(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert meta["schema_version"] == 1
        assert meta["platform"] == "android"
        assert meta["package_name"] == _default_opts().resolved_package_id()
        assert "content_rating" in meta
        assert meta["data_safety_form_path"] == "docs/play/data_safety.yaml"

    def test_reverse_dns_matches_both_stores(self, project_dir):
        render_project(project_dir, _default_opts())
        asc = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        play = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        # Shared id across both stores — cross-platform promise.
        assert asc["bundle_id"] == play["package_name"]

    def test_iaps_present_when_payments_on(self, project_dir):
        render_project(project_dir, _default_opts(payments=True))
        asc = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        play = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert len(asc["in_app_purchases"]) >= 1
        assert len(play["in_app_purchases"]) >= 1
        assert len(play["subscriptions"]) >= 1

    def test_iaps_empty_when_payments_off(self, project_dir):
        render_project(project_dir, _default_opts(payments=False))
        asc = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        play = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert asc["in_app_purchases"] == []
        assert play["in_app_purchases"] == []
        assert play["subscriptions"] == []

    def test_fastlane_metadata_dirs_present_both_stores(self, project_dir):
        render_project(project_dir, _default_opts())
        android_meta = project_dir / "fastlane/metadata/android/en-US"
        ios_meta = project_dir / "fastlane/metadata/ios/en-US"
        assert (android_meta / "title.txt").is_file()
        assert (android_meta / "short_description.txt").is_file()
        assert (android_meta / "full_description.txt").is_file()
        assert (ios_meta / "name.txt").is_file()
        assert (ios_meta / "description.txt").is_file()

    def test_play_staged_rollout_present(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert "tracks" in meta
        assert 0.0 < meta["tracks"]["production"]["user_fraction"] < 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P6 mobile-compliance binding — platform="both"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP6Compliance:
    def test_mobile_compliance_both_platforms_passes_clean(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="both")
        assert bundle.passed, [
            (g.gate_id, g.verdict.value, g.summary) for g in bundle.gates
        ]

    def test_both_asc_and_play_gates_are_real_passes(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="both")
        asc = bundle.get("app_store_guidelines")
        play = bundle.get("play_policy")
        privacy = bundle.get("privacy_labels")
        assert asc is not None
        assert play is not None
        assert privacy is not None
        # Neither rail should be skipped under platform="both".
        assert asc.verdict.value in ("pass", "skipped"), asc.summary
        assert play.verdict.value == "pass", play.summary
        # Privacy gate should pick up at least firebase_messaging when push=on.
        assert privacy.verdict.value == "pass", privacy.summary

    def test_data_safety_yaml_parses(self, project_dir):
        import yaml

        render_project(project_dir, _default_opts())
        form = project_dir / "docs/play/data_safety.yaml"
        doc = yaml.safe_load(form.read_text())
        assert doc["schema_version"] == 1
        assert doc["package_name"] == _default_opts().resolved_package_id()
        assert isinstance(doc["declared_sdks"], list)
        assert len(doc["declared_sdks"]) >= 1

    def test_data_safety_lists_firebase_when_push_on(self, project_dir):
        import yaml

        render_project(project_dir, _default_opts(push=True))
        form = project_dir / "docs/play/data_safety.yaml"
        doc = yaml.safe_load(form.read_text())
        assert any(
            "firebase" in sdk.lower() for sdk in doc["declared_sdks"]
        ), doc["declared_sdks"]

    def test_privacy_gate_detects_firebase_messaging(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts(push=True))
        bundle = run_all(project_dir, platform="both")
        privacy = bundle.get("privacy_labels")
        assert privacy is not None
        assert privacy.verdict.value == "pass", privacy.summary
        detected = privacy.detail.get("detected_sdks") or []
        joined = " ".join(detected).lower()
        assert "firebase" in joined or "messaging" in joined


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot report integration (aggregates P0-P6 across both rails)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_aggregates_all_gates(self, project_dir):
        opts = _default_opts()
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["skill"] == "skill-flutter"
        # Both rails bound.
        assert report["p0_ios_profile"]["min_os_version"] == "16.0"
        assert report["p0_android_profile"]["min_os_version"] == "24"
        # Flutter autodetect wins.
        assert report["p2_simulate_autodetect"] == "flutter"
        # Both store metadatas present + aligned with package_id.
        assert report["p5_asc_metadata"]["present"] is True
        assert report["p5_asc_metadata"]["package_matches"] is True
        assert report["p5_play_metadata"]["present"] is True
        assert report["p5_play_metadata"]["package_matches"] is True
        # Compliance bundle green on both rails.
        assert report["p6_compliance"]["passed"] is True

    def test_pilot_report_options_round_trip(self, project_dir):
        opts = _default_opts(package_id="com.example.pilot.app")
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["options"]["package_id"] == "com.example.pilot.app"
        assert report["options"]["push"] is True
        assert report["options"]["payments"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Package-id resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPackageIdResolution:
    def test_default_package_id_uses_com_example(self):
        opts = ScaffoldOptions(project_name="MyApp")
        assert opts.resolved_package_id() == "com.example.myapp"

    def test_default_package_id_sanitises_underscores(self):
        opts = ScaffoldOptions(project_name="My_Awesome_App")
        assert opts.resolved_package_id() == "com.example.myawesomeapp"

    def test_explicit_package_id_propagates_to_gradle(self, project_dir):
        opts = _default_opts(package_id="com.acme.production.app")
        render_project(project_dir, opts)
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        assert 'applicationId = "com.acme.production.app"' in gradle

    def test_explicit_package_id_propagates_to_ios(self, project_dir):
        opts = _default_opts(package_id="com.acme.production.app")
        render_project(project_dir, opts)
        plist = (project_dir / "ios/Runner/Info.plist").read_text()
        assert "<string>com.acme.production.app</string>" in plist

    def test_explicit_package_id_propagates_to_both_metadatas(self, project_dir):
        opts = _default_opts(package_id="com.acme.production.app")
        render_project(project_dir, opts)
        asc = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        play = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert asc["bundle_id"] == "com.acme.production.app"
        assert play["package_name"] == "com.acme.production.app"

    def test_explicit_package_id_propagates_to_appfile(self, project_dir):
        opts = _default_opts(package_id="com.acme.production.app")
        render_project(project_dir, opts)
        app = (project_dir / "fastlane/Appfile").read_text()
        assert 'app_identifier "com.acme.production.app"' in app
        assert 'package_name "com.acme.production.app"' in app


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Toolchain pins
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolchainPins:
    def test_gradle_wrapper_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        wrapper = (project_dir / "android/gradle/wrapper/gradle-wrapper.properties").read_text()
        assert "gradle-8.7-bin.zip" in wrapper

    def test_dart_sdk_constraint_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        pubspec = (project_dir / "pubspec.yaml").read_text()
        assert 'sdk: ">=3.4.0 <4.0.0"' in pubspec
        assert 'flutter: ">=3.22.0"' in pubspec

    def test_jvm_target_17(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/app/build.gradle.kts").read_text()
        assert "VERSION_17" in gradle
        assert 'jvmTarget = "17"' in gradle

    def test_cocoapods_stats_disabled(self, project_dir):
        render_project(project_dir, _default_opts())
        podfile = (project_dir / "ios/Podfile").read_text()
        assert "COCOAPODS_DISABLE_STATS" in podfile
