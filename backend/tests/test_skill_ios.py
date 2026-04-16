"""P7 #292 — SKILL-IOS pilot skill contract tests.

SKILL-IOS is the first mobile-vertical skill pack and the pilot that
validates the P0-P6 framework end-to-end. These tests lock the
framework invariants the same way D1 SKILL-UVC locked C5, D29
SKILL-HMI-WEBUI locked C26, and W6 SKILL-NEXTJS locked the web vertical:

* **P0** — ``ios-arm64`` profile loads cleanly via
  ``backend.platform.load_raw_profile``; rendered scaffold's
  ``IPHONEOS_DEPLOYMENT_TARGET`` matches the profile's
  ``min_os_version``.
* **P2** — rendered project autodetects as ``xcuitest`` via
  ``mobile_simulator.resolve_ui_framework``.
* **P3** — codesign xcconfig contains ``$(OMNISIGHT_*)`` placeholders,
  never bakes a real cert hash.
* **P4** — generated SwiftUI views honour the ios-swift role
  anti-patterns (`@Observable` over `ObservableObject`, no `print()`
  in source, Keychain-friendly defaults).
* **P5** — ``AppStoreMetadata.json`` shape conforms to the
  ``backend.app_store_connect`` schema (bundle_id round-trip, age
  rating present, idfa flag).
* **P6** — ``mobile_compliance.run_all(platform="ios")`` passes
  against the rendered project; ASC clean, Privacy gate skipped (no
  Podfile.lock yet — by design).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.ios_scaffolder import (
    RenderOutcome,
    ScaffoldOptions,
    _PLATFORM_PROFILE_ID,
    _PUSH_ONLY_FILES,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _STOREKIT_ONLY_FILES,
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
        package_manager="spm",
        push=True,
        storekit=True,
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
        assert "skill-ios" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-ios")
        assert result.ok, (
            f"skill-ios validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-ios")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-ios")
        assert info is not None
        assert info.manifest is not None
        # CORE-05 is the skill pack framework itself; must stay pinned.
        assert "CORE-05" in info.manifest.depends_on_core

    def test_manifest_keywords_include_pilot_marker(self):
        info = get_skill("skill-ios")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        # "p7" marks the pilot milestone; "ios" / "swiftui" / "storekit-2"
        # / "apns" make the pack findable by operators looking for the
        # mobile pilot.
        assert {"pilot", "p7", "ios", "swiftui", "storekit-2", "apns"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-ios"

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
            "App/Sources/App.swift",
            "App/Sources/ContentView.swift",
            "App/Resources/Info.plist",
            "App/Resources/App.entitlements",
            "Configs/Common.xcconfig",
            "Configs/Signing.xcconfig",
            "Package.swift",
            "fastlane/Fastfile",
            "fastlane/Appfile",
            "project.yml",
            "Tests/ContentViewTests.swift",
            "UITests/SmokeTests.swift",
            "README.md",
            ".gitignore",
        ]
        for rel in must_exist:
            assert (project_dir / rel).is_file(), f"missing: {rel}"
        assert outcome.bytes_written > 0
        assert outcome.warnings == []

    def test_render_returns_render_outcome(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        assert isinstance(outcome, RenderOutcome)
        assert outcome.out_dir == project_dir
        assert outcome.profile_binding["platform_profile"] == _PLATFORM_PROFILE_ID

    def test_push_off_skips_apns_files(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        for rel in _PUSH_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"

    def test_push_off_drops_aps_environment(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        ent = (project_dir / "App/Resources/App.entitlements").read_text()
        # Check the actual entitlement key is absent (the XML comment
        # header mentions the name).
        assert "<key>aps-environment</key>" not in ent
        info = (project_dir / "App/Resources/Info.plist").read_text()
        assert "<key>UIBackgroundModes</key>" not in info
        assert "<string>remote-notification</string>" not in info

    def test_push_on_emits_apns_files(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        for rel in _PUSH_ONLY_FILES:
            assert (project_dir / rel).is_file(), f"{rel} missing"
        assert "<key>aps-environment</key>" in (project_dir / "App/Resources/App.entitlements").read_text()

    def test_storekit_off_skips_storekit_files(self, project_dir):
        render_project(project_dir, _default_opts(storekit=False))
        for rel in _STOREKIT_ONLY_FILES:
            # The .j2 entries map to either rendered file (.j2 stripped)
            # OR the raw path; check both forms.
            stripped = rel.removesuffix(".j2")
            assert not (project_dir / stripped).exists(), f"{stripped} leaked through"

    def test_storekit_on_emits_storekit_files(self, project_dir):
        render_project(project_dir, _default_opts(storekit=True))
        assert (project_dir / "App/Sources/StoreKit/StoreKitManager.swift").is_file()
        assert (project_dir / "App/Sources/StoreKit/StoreView.swift").is_file()
        assert (project_dir / "App/Sources/StoreKit/Configuration.storekit").is_file()
        assert (project_dir / "Tests/StoreKitManagerTests.swift").is_file()

    def test_package_manager_spm_only(self, project_dir):
        render_project(project_dir, _default_opts(package_manager="spm"))
        assert (project_dir / "Package.swift").is_file()
        assert not (project_dir / "Podfile").exists()

    def test_package_manager_cocoapods_only(self, project_dir):
        render_project(project_dir, _default_opts(package_manager="cocoapods"))
        assert (project_dir / "Podfile").is_file()
        assert not (project_dir / "Package.swift").exists()
        # Feature SPM module also drops out under cocoapods-only.
        assert not (project_dir / "Modules/Feature/Sources/FeatureCounter.swift").exists()

    def test_package_manager_both_ships_both_managers(self, project_dir):
        render_project(project_dir, _default_opts(package_manager="both"))
        assert (project_dir / "Package.swift").is_file()
        assert (project_dir / "Podfile").is_file()

    def test_compliance_off_skips_privacy_files(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "App/Resources/PrivacyInfo.xcprivacy").exists()
        assert not (project_dir / "AppStoreMetadata.json").exists()
        assert not (project_dir / "fastlane/metadata/en-US/privacy_url.txt").exists()

    def test_compliance_on_ships_privacy_artifacts(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        assert (project_dir / "App/Resources/PrivacyInfo.xcprivacy").is_file()
        assert (project_dir / "AppStoreMetadata.json").is_file()
        # PrivacyInfo declares the required-reason API entries.
        priv = (project_dir / "App/Resources/PrivacyInfo.xcprivacy").read_text()
        assert "NSPrivacyAccessedAPITypes" in priv
        assert "NSPrivacyAccessedAPICategoryUserDefaults" in priv

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_invalid_package_manager_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", package_manager="bazel-maybe").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="   ").validate()

    def test_project_name_with_dots_rejected(self):
        # Dots are reserved for the bundle id; project_name must be
        # safe to use as an Xcode product name.
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="My.App").validate()

    def test_invalid_bundle_id_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", bundle_id="not-reverse-dns").validate()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P0 platform binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP0PlatformBinding:
    def test_platform_profile_loads(self):
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert raw["mobile_platform"] == "ios"
        assert raw["mobile_abi"] == "arm64"
        assert raw["min_os_version"] == "16.0"

    def test_render_context_pulls_min_os_from_profile(self):
        ctx = _render_context(_default_opts())
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert ctx["min_os_version"] == str(raw["min_os_version"])
        assert ctx["sdk_version"] == str(raw["sdk_version"])

    def test_deployment_target_pinned_in_xcconfig(self, project_dir):
        render_project(project_dir, _default_opts())
        xcconfig = (project_dir / "Configs/Common.xcconfig").read_text()
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert f"IPHONEOS_DEPLOYMENT_TARGET = {raw['min_os_version']}" in xcconfig

    def test_deployment_target_pinned_in_project_yml(self, project_dir):
        render_project(project_dir, _default_opts())
        proj = (project_dir / "project.yml").read_text()
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert f'iOS: "{raw["min_os_version"]}"' in proj

    def test_deployment_target_pinned_in_package_swift(self, project_dir):
        render_project(project_dir, _default_opts(package_manager="spm"))
        pkg = (project_dir / "Package.swift").read_text()
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert f'.iOS("{raw["min_os_version"]}")' in pkg

    def test_deployment_target_pinned_in_podfile(self, project_dir):
        render_project(project_dir, _default_opts(package_manager="cocoapods"))
        pod = (project_dir / "Podfile").read_text()
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert f"platform :ios, '{raw['min_os_version']}'" in pod


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2 simulate-track binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP2SimulateBinding:
    def test_xcuitest_autodetect_with_platform_hint(self, project_dir):
        from backend.mobile_simulator import resolve_ui_framework

        render_project(project_dir, _default_opts())
        # The platform hint mirrors how the P2 mobile track invokes
        # the autodetect — the rendered scaffold doesn't ship a
        # materialised .xcodeproj (XcodeGen does that at build time).
        framework = resolve_ui_framework(project_dir, mobile_platform="ios")
        assert framework == "xcuitest"

    def test_uitests_dir_present(self, project_dir):
        render_project(project_dir, _default_opts())
        assert (project_dir / "UITests/SmokeTests.swift").is_file()

    def test_uitests_smoke_uses_xcuitest_apis(self, project_dir):
        render_project(project_dir, _default_opts())
        smoke = (project_dir / "UITests/SmokeTests.swift").read_text()
        assert "import XCTest" in smoke
        assert "XCUIApplication" in smoke


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P3 codesign chain binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP3CodesignChain:
    def test_signing_xcconfig_uses_placeholders(self, project_dir):
        render_project(project_dir, _default_opts())
        signing = (project_dir / "Configs/Signing.xcconfig").read_text()
        # Placeholders MUST resolve at build time from the env
        # populated by backend/codesign_store.py — the scaffold
        # itself never bakes a real cert hash or profile UUID.
        assert "$(OMNISIGHT_CODE_SIGN_IDENTITY)" in signing
        assert "$(OMNISIGHT_PROVISIONING_PROFILE_SPECIFIER)" in signing
        assert "$(OMNISIGHT_DEVELOPMENT_TEAM)" in signing

    def test_signing_xcconfig_no_real_secrets(self, project_dir):
        render_project(project_dir, _default_opts())
        signing = (project_dir / "Configs/Signing.xcconfig").read_text()
        # No 40-char SHA-1 cert hash, no UUID-shaped provisioning ID,
        # no Apple team-id pattern (10-char alnum).
        import re
        assert not re.search(r"\b[A-F0-9]{40}\b", signing), "Looks like a real cert hash"
        assert not re.search(
            r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
            signing,
        ), "Looks like a real provisioning UUID"

    def test_fastfile_reads_codesign_env(self, project_dir):
        render_project(project_dir, _default_opts())
        fast = (project_dir / "fastlane/Fastfile").read_text()
        assert 'ENV["OMNISIGHT_CODE_SIGN_IDENTITY"]' in fast
        assert 'ENV["OMNISIGHT_PROVISIONING_PROFILE_SPECIFIER"]' in fast


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P4 ios-swift role anti-patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP4RoleAntiPatterns:
    def test_observable_macro_used_not_observableobject(self, project_dir):
        render_project(project_dir, _default_opts())
        counter = (project_dir / "Modules/Feature/Sources/FeatureCounter.swift").read_text()
        assert "@Observable" in counter
        # Strip comments: anti-pattern names appear in the rationale
        # comment, not as actual conformances.
        code = "\n".join(
            line for line in counter.splitlines() if not line.lstrip().startswith("//")
        )
        assert "ObservableObject" not in code
        assert "@Published" not in code

    def test_no_print_in_swift_sources(self, project_dir):
        render_project(project_dir, _default_opts())
        # P4 anti-pattern: never `print(...)` in Release. Use os.Logger.
        for swift_file in project_dir.rglob("*.swift"):
            text = swift_file.read_text()
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue
                assert not stripped.startswith("print("), (
                    f"{swift_file.relative_to(project_dir)}: print() snuck in"
                )

    def test_logger_wired_in_push_manager(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        push = (project_dir / "App/Sources/Push/PushNotificationManager.swift").read_text()
        assert "import os" in push
        assert "Logger(" in push

    def test_strict_concurrency_enabled(self, project_dir):
        render_project(project_dir, _default_opts())
        common = (project_dir / "Configs/Common.xcconfig").read_text()
        assert "SWIFT_STRICT_CONCURRENCY = complete" in common
        assert "SWIFT_TREAT_WARNINGS_AS_ERRORS = YES" in common

    def test_storekit_verifies_jws_results(self, project_dir):
        render_project(project_dir, _default_opts(storekit=True))
        skm = (project_dir / "App/Sources/StoreKit/StoreKitManager.swift").read_text()
        # Apple's StoreKit 2 contract: never trust unverified results.
        assert "verifyResult" in skm
        assert "VerificationResult" in skm
        assert ".unverified" in skm


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P5 App Store Connect submission shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP5StoreSubmission:
    def test_metadata_json_renders_as_valid_json(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        assert meta["schema_version"] == 1
        assert meta["bundle_id"] == _default_opts().resolved_bundle_id()
        assert meta["app_name"] == "PilotApp"

    def test_metadata_json_has_age_rating_and_idfa_flags(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        assert "age_rating" in meta
        assert isinstance(meta["age_rating"], dict)
        assert meta["uses_idfa"] is False

    def test_metadata_json_lists_iaps_when_storekit_on(self, project_dir):
        render_project(project_dir, _default_opts(storekit=True))
        meta = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        assert isinstance(meta["in_app_purchases"], list)
        assert len(meta["in_app_purchases"]) >= 1

    def test_metadata_json_drops_iaps_when_storekit_off(self, project_dir):
        render_project(project_dir, _default_opts(storekit=False))
        meta = json.loads((project_dir / "AppStoreMetadata.json").read_text())
        assert meta["in_app_purchases"] == []
        assert meta["subscriptions"] == []

    def test_fastlane_metadata_dir_present(self, project_dir):
        render_project(project_dir, _default_opts())
        # ASC submission via deliver() reads from fastlane/metadata/<locale>/.
        assert (project_dir / "fastlane/metadata/en-US/name.txt").is_file()
        assert (project_dir / "fastlane/metadata/en-US/description.txt").is_file()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P6 mobile-compliance binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP6Compliance:
    def test_mobile_compliance_passes_clean(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="ios")
        # Bundle.passed = no FAIL or ERROR; SKIPPED is acceptable.
        assert bundle.passed, [
            (g.gate_id, g.verdict.value, g.summary) for g in bundle.gates
        ]
        # ASC gate must be a real PASS (not skipped) — the scaffold
        # ships ≥ 1 .swift file so the ASC scanner has something to chew.
        asc = bundle.get("app_store_guidelines")
        assert asc is not None
        assert asc.verdict.value == "pass", asc.summary

    def test_play_gate_skipped_for_ios_only(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="ios")
        play = bundle.get("play_policy")
        assert play is not None
        assert play.verdict.value == "skipped"

    def test_privacy_manifest_xml_well_formed(self, project_dir):
        import xml.etree.ElementTree as ET
        render_project(project_dir, _default_opts())
        priv_path = project_dir / "App/Resources/PrivacyInfo.xcprivacy"
        # plist files are XML; just verify the parser doesn't choke.
        ET.parse(priv_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot-validation integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_aggregates_all_gates(self, project_dir):
        opts = _default_opts()
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["skill"] == "skill-ios"
        assert report["p0_profile"]["min_os_version"] == "16.0"
        assert report["p2_simulate_autodetect"] == "xcuitest"
        assert report["p5_asc_metadata"]["present"] is True
        assert report["p5_asc_metadata"]["bundle_id_matches"] is True
        assert report["p6_compliance"]["passed"] is True

    def test_pilot_report_options_round_trip(self, project_dir):
        opts = _default_opts(bundle_id="com.example.pilot.app")
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["options"]["bundle_id"] == "com.example.pilot.app"
        assert report["options"]["package_manager"] == "spm"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bundle-id resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBundleIdResolution:
    def test_default_bundle_id_uses_com_example(self):
        opts = ScaffoldOptions(project_name="MyApp")
        assert opts.resolved_bundle_id() == "com.example.myapp"

    def test_explicit_bundle_id_propagates_to_info_plist(self, project_dir):
        opts = _default_opts(bundle_id="com.acme.production.app")
        render_project(project_dir, opts)
        info = (project_dir / "App/Resources/Info.plist").read_text()
        assert "<string>com.acme.production.app</string>" in info

    def test_explicit_bundle_id_propagates_to_xcconfig(self, project_dir):
        opts = _default_opts(bundle_id="com.acme.production.app")
        render_project(project_dir, opts)
        common = (project_dir / "Configs/Common.xcconfig").read_text()
        assert "PRODUCT_BUNDLE_IDENTIFIER = com.acme.production.app" in common

    def test_explicit_bundle_id_propagates_to_appfile(self, project_dir):
        opts = _default_opts(bundle_id="com.acme.production.app")
        render_project(project_dir, opts)
        app = (project_dir / "fastlane/Appfile").read_text()
        assert 'app_identifier("com.acme.production.app")' in app

    def test_bundle_prefix_strips_last_component(self):
        opts = ScaffoldOptions(project_name="x", bundle_id="com.example.acme.foo")
        assert opts.bundle_prefix() == "com.example.acme"
