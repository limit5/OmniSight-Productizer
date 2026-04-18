"""P9 #294 — SKILL-RN pilot skill contract tests.

SKILL-RN is the FOURTH consumer of the P0-P6 framework (second
cross-platform, contrast pick to SKILL-FLUTTER). Same P0-P6 invariants
as SKILL-FLUTTER, different toolchain (React Native 0.76 + TypeScript 5
+ Hermes + Fabric/TurboModules + Metro).

Framework invariants locked — same surface as the flutter suite but
exercised against the RN scaffold so we pin "framework holds on
BOTH cross-platform rails", not just on Flutter.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from backend.rn_scaffolder import (
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
        assert "skill-rn" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-rn")
        assert result.ok, (
            f"skill-rn validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-rn")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-rn")
        assert info is not None
        assert info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core

    def test_manifest_keywords_include_contrast_marker(self):
        info = get_skill("skill-rn")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {
            "react-native", "rn", "typescript", "hermes", "new-architecture",
            "cross-platform", "ios", "android", "contrast", "p9",
        }.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-rn"

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
            "package.json",
            "tsconfig.json",
            ".eslintrc.js",
            ".prettierrc",
            "metro.config.js",
            "babel.config.js",
            ".gitignore",
            "index.js",
            "app.json",
            "App.tsx",
            "src/features/home/HomeScreen.tsx",
            "__tests__/App.test.tsx",
            "e2e/app.test.ts",
            "e2e/.detoxrc.js",
            "ios/RNApp/Info.plist",
            "ios/Podfile",
            "android/settings.gradle",
            "android/build.gradle",
            "android/app/build.gradle",
            "android/app/src/main/AndroidManifest.xml",
            "android/gradle.properties",
            "android/key.properties.example",
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

    def test_push_off_skips_push_files(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        for rel in _PUSH_ONLY_FILES:
            stripped = rel.removesuffix(".j2")
            assert not (project_dir / stripped).exists(), f"{stripped} leaked"

    def test_push_off_drops_package_and_gradle_deps(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        pkg = json.loads((project_dir / "package.json").read_text())
        all_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        assert "@react-native-firebase/messaging" not in all_deps
        assert "@react-native-firebase/app" not in all_deps
        gradle = (project_dir / "android/app/build.gradle").read_text()
        assert "firebase-messaging" not in gradle
        manifest = (project_dir / "android/app/src/main/AndroidManifest.xml").read_text()
        no_comments = re.sub(r"<!--.*?-->", "", manifest, flags=re.DOTALL)
        assert "POST_NOTIFICATIONS" not in no_comments

    def test_push_on_emits_push_files_and_deps(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        assert (project_dir / "src/features/push/push.ts").is_file()
        pkg = json.loads((project_dir / "package.json").read_text())
        all_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        assert "@react-native-firebase/messaging" in all_deps
        gradle = (project_dir / "android/app/build.gradle").read_text()
        assert "firebase-messaging" in gradle

    def test_payments_off_skips_iap_files(self, project_dir):
        render_project(project_dir, _default_opts(payments=False))
        for rel in _PAYMENTS_ONLY_FILES:
            stripped = rel.removesuffix(".j2")
            assert not (project_dir / stripped).exists(), f"{stripped} leaked"
        pkg = json.loads((project_dir / "package.json").read_text())
        all_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        assert "react-native-iap" not in all_deps

    def test_payments_on_emits_iap_files(self, project_dir):
        render_project(project_dir, _default_opts(payments=True))
        assert (project_dir / "src/features/payments/iap.ts").is_file()
        pkg = json.loads((project_dir / "package.json").read_text())
        all_deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        assert "react-native-iap" in all_deps

    def test_compliance_off_drops_privacy_and_metadata(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "AppStoreMetadata.json").exists()
        assert not (project_dir / "PlayStoreMetadata.json").exists()
        assert not (project_dir / "docs/play/data_safety.yaml").exists()
        assert not (project_dir / "ios/RNApp/PrivacyInfo.xcprivacy").exists()

    def test_compliance_on_ships_all_surfaces(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        assert (project_dir / "AppStoreMetadata.json").is_file()
        assert (project_dir / "PlayStoreMetadata.json").is_file()
        assert (project_dir / "docs/play/data_safety.yaml").is_file()
        assert (project_dir / "ios/RNApp/PrivacyInfo.xcprivacy").is_file()

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_non_scaffold_files_are_preserved(self, project_dir):
        render_project(project_dir, _default_opts())
        custom = project_dir / "src/features/login/login.ts"
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
        assert raw["min_os_version"] == "16.0"

    def test_android_profile_loads(self):
        raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert raw["mobile_platform"] == "android"
        assert raw["min_os_version"] == "24"

    def test_render_context_pulls_both_profiles(self):
        ctx = _render_context(_default_opts())
        ios_raw = load_raw_profile(_IOS_PROFILE_ID)
        android_raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert ctx["min_os_version_ios"] == str(ios_raw["min_os_version"])
        assert ctx["min_os_version_android"] == str(android_raw["min_os_version"])

    def test_ios_podfile_platform_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        podfile = (project_dir / "ios/Podfile").read_text()
        ios_raw = load_raw_profile(_IOS_PROFILE_ID)
        assert f"platform :ios, '{ios_raw['min_os_version']}'" in podfile

    def test_ios_info_plist_min_os_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        plist = (project_dir / "ios/RNApp/Info.plist").read_text()
        ios_raw = load_raw_profile(_IOS_PROFILE_ID)
        assert f"<string>{ios_raw['min_os_version']}</string>" in plist

    def test_android_sdk_versions_pinned_in_root_gradle(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/build.gradle").read_text()
        raw = load_raw_profile(_ANDROID_PROFILE_ID)
        assert f"minSdkVersion = {raw['min_os_version']}" in gradle
        assert f"targetSdkVersion = {raw['sdk_version']}" in gradle
        assert f"compileSdkVersion = {raw['sdk_version']}" in gradle

    def test_package_id_flows_to_both_stores(self, project_dir):
        opts = _default_opts(package_id="com.acme.prod.rnapp")
        render_project(project_dir, opts)
        gradle = (project_dir / "android/app/build.gradle").read_text()
        assert 'applicationId "com.acme.prod.rnapp"' in gradle
        plist = (project_dir / "ios/RNApp/Info.plist").read_text()
        assert "<string>com.acme.prod.rnapp</string>" in plist


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2 simulate-track binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP2SimulateBinding:
    def test_react_native_autodetect(self, project_dir):
        from backend.mobile_simulator import resolve_ui_framework

        render_project(project_dir, _default_opts())
        assert (project_dir / "ios").is_dir()
        assert (project_dir / "android").is_dir()
        assert (project_dir / "package.json").is_file()
        framework = resolve_ui_framework(project_dir)
        assert framework == "react-native"

    def test_detox_e2e_present(self, project_dir):
        render_project(project_dir, _default_opts())
        assert (project_dir / "e2e/app.test.ts").is_file()
        assert (project_dir / "e2e/.detoxrc.js").is_file()

    def test_jest_unit_present(self, project_dir):
        render_project(project_dir, _default_opts())
        body = (project_dir / "__tests__/App.test.tsx").read_text()
        assert "@testing-library/react-native" in body
        assert "fireEvent" in body


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

    def test_no_real_secrets_baked(self, project_dir):
        render_project(project_dir, _default_opts())
        props = (project_dir / "android/key.properties.example").read_text()
        assert not re.search(r"\b[A-Fa-f0-9]{64}\b", props)
        assert not re.search(
            r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
            props,
        )

    def test_gradle_reads_env_vars(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/app/build.gradle").read_text()
        assert 'System.getenv("OMNISIGHT_KEYSTORE_PATH")' in gradle
        assert "OMNISIGHT_KEYSTORE_PASSWORD" in gradle
        assert "OMNISIGHT_KEY_ALIAS" in gradle

    def test_fastfile_bails_on_missing_env(self, project_dir):
        render_project(project_dir, _default_opts())
        fast = (project_dir / "fastlane/Fastfile").read_text()
        assert "OMNISIGHT_MACOS_BUILDER" in fast
        assert "OMNISIGHT_KEYSTORE_PATH" in fast
        assert "user_error!" in fast

    def test_gitignore_excludes_signing_material(self, project_dir):
        render_project(project_dir, _default_opts())
        ignore = (project_dir / ".gitignore").read_text()
        assert "key.properties" in ignore
        assert "*.jks" in ignore
        assert "*.keystore" in ignore
        assert "google-services.json" in ignore


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P4 react-native role anti-patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _strip_ts_comments(text: str) -> str:
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = re.sub(r"//[^\n]*", "", out)
    return out


class TestP4RoleAntiPatterns:
    def test_eslint_blocks_console_log(self, project_dir):
        render_project(project_dir, _default_opts())
        eslint = (project_dir / ".eslintrc.js").read_text()
        assert "no-console" in eslint
        assert "error" in eslint

    def test_no_console_log_in_ts_sources(self, project_dir):
        render_project(project_dir, _default_opts())
        offenders: list[str] = []
        for ts in (project_dir / "src").rglob("*.ts"):
            code = _strip_ts_comments(ts.read_text())
            if re.search(r"\bconsole\.log\s*\(", code):
                offenders.append(f"{ts.relative_to(project_dir)}: console.log")
        for tsx in (project_dir / "src").rglob("*.tsx"):
            code = _strip_ts_comments(tsx.read_text())
            if re.search(r"\bconsole\.log\s*\(", code):
                offenders.append(f"{tsx.relative_to(project_dir)}: console.log")
        app_tsx = project_dir / "App.tsx"
        if app_tsx.is_file():
            code = _strip_ts_comments(app_tsx.read_text())
            if re.search(r"\bconsole\.log\s*\(", code):
                offenders.append("App.tsx: console.log")
        assert not offenders, offenders

    def test_home_screen_uses_stylesheet_create(self, project_dir):
        render_project(project_dir, _default_opts())
        body = (project_dir / "src/features/home/HomeScreen.tsx").read_text()
        assert "StyleSheet.create" in body

    def test_home_screen_uses_useCallback(self, project_dir):
        render_project(project_dir, _default_opts())
        body = (project_dir / "src/features/home/HomeScreen.tsx").read_text()
        assert "useCallback" in body

    def test_iap_verify_stub_returns_false(self, project_dir):
        render_project(project_dir, _default_opts(payments=True))
        iap = (project_dir / "src/features/payments/iap.ts").read_text()
        assert "verifyPurchase" in iap
        # finishTransaction must sit inside the `verified` guard.
        assert "if (!verified)" in iap

    def test_push_does_not_log_raw_token(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        push = (project_dir / "src/features/push/push.ts").read_text()
        assert "fingerprint" in push
        code = _strip_ts_comments(push)
        # `console.log(token)` style leakage should not appear.
        assert not re.search(r"console\.(log|info|warn)\s*\(\s*token", code)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P5 dual-store submission shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP5DualStoreSubmission:
    def test_asc_metadata_valid_json(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        assert meta["schema_version"] == 1
        assert meta["platform"] == "ios"
        assert meta["bundle_id"] == _default_opts().resolved_package_id()
        assert meta["app_name"] == "PilotApp"
        assert "age_rating" in meta
        assert meta["uses_idfa"] is False

    def test_play_metadata_valid_json(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert meta["schema_version"] == 1
        assert meta["platform"] == "android"
        assert meta["package_name"] == _default_opts().resolved_package_id()
        assert "content_rating" in meta
        assert meta["data_safety_form_path"] == "docs/play/data_safety.yaml"

    def test_same_id_both_stores(self, project_dir):
        render_project(project_dir, _default_opts())
        asc = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        play = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
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
        assert (project_dir / "fastlane/metadata/android/en-US/title.txt").is_file()
        assert (project_dir / "fastlane/metadata/ios/en-US/name.txt").is_file()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P6 mobile-compliance binding — platform="both"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP6Compliance:
    def test_both_platforms_pass_clean(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="both")
        assert bundle.passed, [
            (g.gate_id, g.verdict.value, g.summary) for g in bundle.gates
        ]

    def test_privacy_gate_detects_firebase_on_push_on(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts(push=True))
        bundle = run_all(project_dir, platform="both")
        privacy = bundle.get("privacy_labels")
        assert privacy is not None
        assert privacy.verdict.value == "pass", privacy.summary

    def test_data_safety_yaml_parses(self, project_dir):
        import yaml

        render_project(project_dir, _default_opts())
        form = project_dir / "docs/play/data_safety.yaml"
        doc = yaml.safe_load(form.read_text())
        assert doc["schema_version"] == 1
        assert doc["package_name"] == _default_opts().resolved_package_id()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RN-specific: New Architecture + Hermes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNewArchitecture:
    def test_android_gradle_properties_enables_new_arch_and_hermes(self, project_dir):
        render_project(project_dir, _default_opts())
        props = (project_dir / "android/gradle.properties").read_text()
        assert "newArchEnabled=true" in props
        assert "hermesEnabled=true" in props

    def test_ios_podfile_enables_new_architecture(self, project_dir):
        render_project(project_dir, _default_opts())
        podfile = (project_dir / "ios/Podfile").read_text()
        assert "RCT_NEW_ARCH_ENABLED" in podfile
        assert ":new_arch_enabled => true" in podfile

    def test_ios_podfile_enables_hermes(self, project_dir):
        render_project(project_dir, _default_opts())
        podfile = (project_dir / "ios/Podfile").read_text()
        assert ":hermes_enabled => true" in podfile

    def test_ios_podfile_enables_fabric(self, project_dir):
        render_project(project_dir, _default_opts())
        podfile = (project_dir / "ios/Podfile").read_text()
        assert ":fabric_enabled => true" in podfile

    def test_typescript_strict_mode(self, project_dir):
        render_project(project_dir, _default_opts())
        ts = json.loads((project_dir / "tsconfig.json").read_text())
        co = ts.get("compilerOptions", {})
        assert co.get("strict") is True
        assert co.get("noUncheckedIndexedAccess") is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot report
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_aggregates_all_gates(self, project_dir):
        opts = _default_opts()
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["skill"] == "skill-rn"
        assert report["p0_ios_profile"]["min_os_version"] == "16.0"
        assert report["p0_android_profile"]["min_os_version"] == "24"
        assert report["p2_simulate_autodetect"] == "react-native"
        assert report["p5_asc_metadata"]["present"] is True
        assert report["p5_asc_metadata"]["package_matches"] is True
        assert report["p5_play_metadata"]["present"] is True
        assert report["p5_play_metadata"]["package_matches"] is True
        assert report["p6_compliance"]["passed"] is True

    def test_pilot_report_options_round_trip(self, project_dir):
        opts = _default_opts(package_id="com.example.rn.app")
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["options"]["package_id"] == "com.example.rn.app"
        assert report["options"]["push"] is True
        assert report["options"]["payments"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Package id resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPackageIdResolution:
    def test_default_package_id(self):
        opts = ScaffoldOptions(project_name="RnApp")
        assert opts.resolved_package_id() == "com.example.rnapp"

    def test_default_package_id_sanitises_underscores(self):
        opts = ScaffoldOptions(project_name="My_Rn_App")
        assert opts.resolved_package_id() == "com.example.myrnapp"

    def test_explicit_id_propagates_to_gradle(self, project_dir):
        opts = _default_opts(package_id="com.acme.rn.app")
        render_project(project_dir, opts)
        gradle = (project_dir / "android/app/build.gradle").read_text()
        assert 'applicationId "com.acme.rn.app"' in gradle

    def test_explicit_id_propagates_to_ios(self, project_dir):
        opts = _default_opts(package_id="com.acme.rn.app")
        render_project(project_dir, opts)
        plist = (project_dir / "ios/RNApp/Info.plist").read_text()
        assert "<string>com.acme.rn.app</string>" in plist

    def test_explicit_id_propagates_to_both_metadatas(self, project_dir):
        opts = _default_opts(package_id="com.acme.rn.app")
        render_project(project_dir, opts)
        asc = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        play = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert asc["bundle_id"] == "com.acme.rn.app"
        assert play["package_name"] == "com.acme.rn.app"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Toolchain pins
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolchainPins:
    def test_react_native_version_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = json.loads((project_dir / "package.json").read_text())
        deps = pkg.get("dependencies") or {}
        assert deps.get("react-native") == "0.76.0"
        assert deps.get("react") == "18.3.1"

    def test_typescript_version_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        pkg = json.loads((project_dir / "package.json").read_text())
        dev_deps = pkg.get("devDependencies") or {}
        assert dev_deps.get("typescript") == "5.5.4"

    def test_gradle_wrapper_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        wrapper = (project_dir / "android/gradle/wrapper/gradle-wrapper.properties").read_text()
        assert "gradle-8.7-bin.zip" in wrapper

    def test_jvm_target_17(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "android/app/build.gradle").read_text()
        assert "VERSION_17" in gradle
        assert 'jvmTarget = "17"' in gradle
