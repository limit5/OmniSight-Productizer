"""P8 #293 — SKILL-ANDROID pilot skill contract tests.

SKILL-ANDROID is the second mobile-vertical skill pack — n=2 consumer
of the P0-P6 framework. SKILL-IOS (P7 #292) was the pilot; this suite
proves the framework works on a disjoint mobile toolchain (Gradle 8 +
Kotlin 2.0 + Jetpack Compose + FCM + Play Billing).

Framework invariants locked:

* **P0** — ``android-arm64-v8a`` profile loads cleanly; rendered
  scaffold's ``minSdk`` / ``targetSdk`` match the profile's
  ``min_os_version`` / ``sdk_version``.
* **P2** — rendered project autodetects as ``espresso`` via
  ``mobile_simulator.resolve_ui_framework``.
* **P3** — keystore placeholders use ``$OMNISIGHT_KEYSTORE_*`` env
  references; never bake a real keystore binary or password.
* **P4** — generated Compose code honours the android-kotlin role
  anti-patterns (no ``println`` / ``System.out.println`` /
  ``Log.d`` / ``Log.v`` in code, ViewModel + StateFlow, warnings-as-
  errors).
* **P5** — ``PlayStoreMetadata.json`` shape conforms to the
  ``backend.google_play_developer`` schema (package_name round-trip,
  content_rating present, data_safety_form_path wired).
* **P6** — ``mobile_compliance.run_all(platform="android")`` passes
  against the rendered project with the default knobs; Play gate
  clean, ASC gate skipped (android-only pack).
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from backend.android_scaffolder import (
    RenderOutcome,
    ScaffoldOptions,
    _BILLING_ONLY_FILES,
    _PLATFORM_PROFILE_ID,
    _PUSH_ONLY_FILES,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _render_context,
    pilot_report,
    render_project,
    validate_pack,
)
from backend.platform_profile import load_raw_profile
from backend.skill_registry import get_skill, list_skills, validate_skill


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "PilotApp"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="PilotApp",
        push=True,
        billing=True,
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
        assert "skill-android" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-android")
        assert result.ok, (
            f"skill-android validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-android")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-android")
        assert info is not None
        assert info.manifest is not None
        # CORE-05 is the skill pack framework itself; must stay pinned.
        assert "CORE-05" in info.manifest.depends_on_core

    def test_manifest_keywords_include_pilot_marker(self):
        info = get_skill("skill-android")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {
            "pilot", "p8", "android", "kotlin", "jetpack-compose",
            "fcm", "play-billing",
        }.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-android"

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
            "settings.gradle.kts",
            "build.gradle.kts",
            "gradle.properties",
            "gradle/wrapper/gradle-wrapper.properties",
            "app/build.gradle.kts",
            "app/src/main/AndroidManifest.xml",
            "app/src/main/java/com/omnisight/pilot/MainActivity.kt",
            "app/src/main/java/com/omnisight/pilot/Application.kt",
            "app/src/main/java/com/omnisight/pilot/ui/HomeScreen.kt",
            "app/src/main/java/com/omnisight/pilot/ui/theme/Theme.kt",
            "app/src/main/res/values/strings.xml",
            "app/src/main/res/values/themes.xml",
            "app/src/main/res/xml/backup_rules.xml",
            "app/src/main/res/xml/data_extraction_rules.xml",
            "app/src/test/java/com/omnisight/pilot/ExampleUnitTest.kt",
            "app/src/androidTest/java/com/omnisight/pilot/MainActivityTest.kt",
            "fastlane/Fastfile",
            "fastlane/Appfile",
            "keystore.properties.example",
            "PlayStoreMetadata.json",
            "README.md",
            ".gitignore",
            "docs/play/data_safety.yaml",
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

    def test_push_off_skips_fcm_files(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        for rel in _PUSH_ONLY_FILES:
            stripped = rel.removesuffix(".j2")
            assert not (project_dir / stripped).exists(), f"{stripped} leaked through"

    def test_push_off_drops_manifest_entries(self, project_dir):
        render_project(project_dir, _default_opts(push=False))
        manifest = (project_dir / "app/src/main/AndroidManifest.xml").read_text()
        # Strip XML comments: the header comment mentions POST_NOTIFICATIONS
        # in rationale text; only the actual <uses-permission> should go away.
        no_comments = re.sub(r"<!--.*?-->", "", manifest, flags=re.DOTALL)
        assert "android.permission.POST_NOTIFICATIONS" not in no_comments
        assert "FcmMessagingService" not in no_comments
        assert "firebase_analytics_collection_enabled" not in no_comments

    def test_push_on_emits_fcm_files(self, project_dir):
        render_project(project_dir, _default_opts(push=True))
        for rel in _PUSH_ONLY_FILES:
            stripped = rel.removesuffix(".j2")
            assert (project_dir / stripped).is_file(), f"{stripped} missing"
        manifest = (project_dir / "app/src/main/AndroidManifest.xml").read_text()
        no_comments = re.sub(r"<!--.*?-->", "", manifest, flags=re.DOTALL)
        assert "android.permission.POST_NOTIFICATIONS" in no_comments
        assert "FcmMessagingService" in no_comments

    def test_billing_off_skips_billing_files(self, project_dir):
        render_project(project_dir, _default_opts(billing=False))
        for rel in _BILLING_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert "billingclient" not in gradle

    def test_billing_on_emits_billing_files(self, project_dir):
        render_project(project_dir, _default_opts(billing=True))
        assert (project_dir / "app/src/main/java/com/omnisight/pilot/billing/BillingClientManager.kt").is_file()
        assert (project_dir / "app/src/main/java/com/omnisight/pilot/billing/BillingScreen.kt").is_file()
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert "com.android.billingclient:billing-ktx" in gradle

    def test_compliance_off_skips_data_safety(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "PlayStoreMetadata.json").exists()
        assert not (project_dir / "docs/play/data_safety.yaml").exists()
        assert not (
            project_dir / "fastlane/metadata/android/en-US/full_description.txt"
        ).exists()

    def test_compliance_on_ships_data_safety(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        assert (project_dir / "PlayStoreMetadata.json").is_file()
        form = project_dir / "docs/play/data_safety.yaml"
        assert form.is_file()
        content = form.read_text()
        assert "declared_sdks" in content
        assert "schema_version" in content

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_non_scaffold_files_are_preserved(self, project_dir):
        render_project(project_dir, _default_opts())
        custom = project_dir / "app/src/main/java/com/omnisight/pilot/CustomFeature.kt"
        custom.write_text("// user-added file — must not be clobbered\n")
        render_project(project_dir, _default_opts())
        assert custom.is_file()
        assert "user-added" in custom.read_text()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="   ").validate()

    def test_project_name_with_hyphen_rejected(self):
        # Hyphens aren't legal Kotlin identifier chars, so we reject
        # them at validate-time rather than letting gradle / kotlinc
        # surface a confusing error.
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="My-App").validate()

    def test_project_name_with_dots_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="My.App").validate()

    def test_project_name_leading_digit_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="123App").validate()

    def test_invalid_package_id_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", package_id="not-reverse-dns").validate()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P0 platform binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP0PlatformBinding:
    def test_platform_profile_loads(self):
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert raw["mobile_platform"] == "android"
        assert raw["mobile_abi"] == "arm64-v8a"
        assert raw["min_os_version"] == "24"
        assert raw["sdk_version"] == "35"

    def test_render_context_pulls_sdk_versions_from_profile(self):
        ctx = _render_context(_default_opts())
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert ctx["min_os_version"] == str(raw["min_os_version"])
        assert ctx["sdk_version"] == str(raw["sdk_version"])

    def test_min_sdk_pinned_in_gradle(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert f"minSdk = {raw['min_os_version']}" in gradle

    def test_target_sdk_pinned_in_gradle(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        raw = load_raw_profile(_PLATFORM_PROFILE_ID)
        assert f"targetSdk = {raw['sdk_version']}" in gradle
        assert f"compileSdk = {raw['sdk_version']}" in gradle

    def test_application_id_matches_package_id(self, project_dir):
        opts = _default_opts(package_id="com.acme.prod.app")
        render_project(project_dir, opts)
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert 'applicationId = "com.acme.prod.app"' in gradle

    def test_namespace_is_fixed_across_render(self, project_dir):
        # The kotlin source tree layout is fixed (com/omnisight/pilot).
        # AGP 8's `namespace` is what makes R class generation work and
        # is decoupled from the Play-Store-visible applicationId.
        render_project(project_dir, _default_opts(package_id="com.arbitrary.x"))
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert 'namespace = "com.omnisight.pilot"' in gradle


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P2 simulate-track binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP2SimulateBinding:
    def test_espresso_autodetect(self, project_dir):
        from backend.mobile_simulator import resolve_ui_framework

        render_project(project_dir, _default_opts())
        framework = resolve_ui_framework(project_dir, mobile_platform="android")
        assert framework == "espresso"

    def test_android_test_dir_present(self, project_dir):
        render_project(project_dir, _default_opts())
        assert (
            project_dir / "app/src/androidTest/java/com/omnisight/pilot/MainActivityTest.kt"
        ).is_file()

    def test_android_test_uses_compose_apis(self, project_dir):
        render_project(project_dir, _default_opts())
        t = (
            project_dir / "app/src/androidTest/java/com/omnisight/pilot/MainActivityTest.kt"
        ).read_text()
        assert "AndroidJUnit4" in t
        assert "createAndroidComposeRule" in t
        assert "espresso" in t.lower()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P3 codesign chain binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP3CodesignChain:
    def test_keystore_example_uses_env_placeholders(self, project_dir):
        render_project(project_dir, _default_opts())
        props = (project_dir / "keystore.properties.example").read_text()
        assert "$OMNISIGHT_KEYSTORE_PATH" in props
        assert "$OMNISIGHT_KEYSTORE_PASSWORD" in props
        assert "$OMNISIGHT_KEY_ALIAS" in props
        assert "$OMNISIGHT_KEY_PASSWORD" in props

    def test_keystore_example_no_real_secrets(self, project_dir):
        render_project(project_dir, _default_opts())
        props = (project_dir / "keystore.properties.example").read_text()
        # Reject anything that LOOKS like a real secret.
        assert not re.search(r"\b[A-Fa-f0-9]{64}\b", props), "Looks like a real SHA-256"
        assert not re.search(
            r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
            props,
        ), "Looks like a real UUID"
        # No bare plaintext ascii password strings > 8 chars following = sign
        # (our placeholders are all `$VAR` shaped).
        assert re.findall(r"=\s*[A-Za-z0-9]{8,}$", props, flags=re.MULTILINE) == []

    def test_gradle_reads_keystore_env(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert 'System.getenv("OMNISIGHT_KEYSTORE_PATH")' in gradle or \
               'keystoreProp("OMNISIGHT_KEYSTORE_PATH")' in gradle
        assert "OMNISIGHT_KEYSTORE_PASSWORD" in gradle
        assert "OMNISIGHT_KEY_ALIAS" in gradle

    def test_fastfile_enforces_keystore_env(self, project_dir):
        render_project(project_dir, _default_opts())
        fast = (project_dir / "fastlane/Fastfile").read_text()
        assert 'ENV["OMNISIGHT_KEYSTORE_PATH"]' in fast
        # Fail-closed: the lane bails if the keystore env isn't wired.
        assert "user_error!" in fast

    def test_gitignore_excludes_keystore_material(self, project_dir):
        render_project(project_dir, _default_opts())
        ignore = (project_dir / ".gitignore").read_text()
        assert "keystore.properties" in ignore
        assert "*.jks" in ignore
        assert "*.keystore" in ignore


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P4 android-kotlin role anti-patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _strip_kotlin_comments(text: str) -> str:
    # Strip `// …` line comments and `/* … */` block comments.
    out = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = re.sub(r"//[^\n]*", "", out)
    return out


class TestP4RoleAntiPatterns:
    def test_no_println_in_kotlin_sources(self, project_dir):
        render_project(project_dir, _default_opts())
        offenders: list[str] = []
        for kt in project_dir.rglob("*.kt"):
            code = _strip_kotlin_comments(kt.read_text())
            # Anti-pattern: stdlib `println(...)` / `print(...)` in prod.
            # (Allow the helper name fragment in identifiers.)
            if re.search(r"\bprintln\s*\(", code):
                offenders.append(f"{kt.relative_to(project_dir)}: println()")
            if re.search(r"\bprint\s*\(", code):
                offenders.append(f"{kt.relative_to(project_dir)}: print()")
        assert not offenders, offenders

    def test_no_system_out_in_kotlin_sources(self, project_dir):
        render_project(project_dir, _default_opts())
        offenders: list[str] = []
        for kt in project_dir.rglob("*.kt"):
            code = _strip_kotlin_comments(kt.read_text())
            if "System.out" in code or "System.err" in code:
                offenders.append(str(kt.relative_to(project_dir)))
        assert not offenders, offenders

    def test_no_log_d_or_log_v_in_kotlin_sources(self, project_dir):
        render_project(project_dir, _default_opts())
        offenders: list[str] = []
        for kt in project_dir.rglob("*.kt"):
            code = _strip_kotlin_comments(kt.read_text())
            # Debug / verbose logs should never ship.
            if re.search(r"\bLog\.d\s*\(", code):
                offenders.append(f"{kt.relative_to(project_dir)}: Log.d")
            if re.search(r"\bLog\.v\s*\(", code):
                offenders.append(f"{kt.relative_to(project_dir)}: Log.v")
        assert not offenders, offenders

    def test_home_screen_uses_viewmodel_and_stateflow(self, project_dir):
        render_project(project_dir, _default_opts())
        home = (
            project_dir / "app/src/main/java/com/omnisight/pilot/ui/HomeScreen.kt"
        ).read_text()
        assert ": ViewModel()" in home
        assert "StateFlow" in home
        assert "collectAsStateWithLifecycle" in home
        # Must NOT use deprecated observer patterns. Strip comments first
        # so the rationale text in the header doesn't trip the check.
        code = _strip_kotlin_comments(home)
        assert "observeAsState" not in code

    def test_warnings_as_errors_set(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert "allWarningsAsErrors = true" in gradle

    def test_billing_verifies_purchases_server_side(self, project_dir):
        render_project(project_dir, _default_opts(billing=True))
        billing = (
            project_dir / "app/src/main/java/com/omnisight/pilot/billing/BillingClientManager.kt"
        ).read_text()
        assert "verifyPurchase" in billing
        # verifyPurchase must be invoked from the purchase-update path.
        assert "if (!verifyPurchase(purchase))" in billing


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P5 Play Developer submission shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP5PlaySubmission:
    def test_metadata_json_renders_as_valid_json(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert meta["schema_version"] == 1
        assert meta["package_name"] == _default_opts().resolved_package_id()
        assert meta["app_name"] == "PilotApp"

    def test_metadata_json_package_name_reverse_dns(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        # Matches the same pattern backend.google_play_developer enforces.
        assert re.match(
            r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$",
            meta["package_name"],
        )

    def test_metadata_json_has_content_rating_and_data_safety(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert "content_rating" in meta
        assert isinstance(meta["content_rating"], dict)
        assert "data_safety_form_path" in meta
        assert meta["data_safety_form_path"] == "docs/play/data_safety.yaml"

    def test_metadata_json_lists_iaps_when_billing_on(self, project_dir):
        render_project(project_dir, _default_opts(billing=True))
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert isinstance(meta["in_app_purchases"], list)
        assert len(meta["in_app_purchases"]) >= 1
        assert len(meta["subscriptions"]) >= 1

    def test_metadata_json_drops_iaps_when_billing_off(self, project_dir):
        render_project(project_dir, _default_opts(billing=False))
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert meta["in_app_purchases"] == []
        assert meta["subscriptions"] == []

    def test_fastlane_metadata_dir_present(self, project_dir):
        render_project(project_dir, _default_opts())
        # `supply` reads from fastlane/metadata/android/<locale>/.
        base = project_dir / "fastlane/metadata/android/en-US"
        assert (base / "title.txt").is_file()
        assert (base / "short_description.txt").is_file()
        assert (base / "full_description.txt").is_file()

    def test_metadata_json_tracks_include_staged_rollout(self, project_dir):
        render_project(project_dir, _default_opts())
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert "tracks" in meta
        assert 0.0 < meta["tracks"]["production"]["user_fraction"] < 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  P6 mobile-compliance binding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestP6Compliance:
    def test_mobile_compliance_passes_clean(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="android")
        # Bundle.passed = no FAIL or ERROR; SKIPPED is acceptable.
        assert bundle.passed, [
            (g.gate_id, g.verdict.value, g.summary) for g in bundle.gates
        ]
        # Play gate must be a real PASS.
        play = bundle.get("play_policy")
        assert play is not None
        assert play.verdict.value == "pass", play.summary

    def test_asc_gate_skipped_for_android_only(self, project_dir):
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts())
        bundle = run_all(project_dir, platform="android")
        asc = bundle.get("app_store_guidelines")
        assert asc is not None
        assert asc.verdict.value == "skipped"

    def test_data_safety_yaml_parses(self, project_dir):
        import yaml

        render_project(project_dir, _default_opts())
        form = project_dir / "docs/play/data_safety.yaml"
        doc = yaml.safe_load(form.read_text())
        assert doc["schema_version"] == 1
        assert doc["package_name"] == _default_opts().resolved_package_id()
        assert isinstance(doc["declared_sdks"], list)
        assert len(doc["declared_sdks"]) >= 1

    def test_data_safety_form_lists_firebase_when_push_on(self, project_dir):
        import yaml

        render_project(project_dir, _default_opts(push=True))
        form = project_dir / "docs/play/data_safety.yaml"
        doc = yaml.safe_load(form.read_text())
        assert any(
            "firebase" in sdk.lower() for sdk in doc["declared_sdks"]
        ), doc["declared_sdks"]

    def test_privacy_gate_detects_firebase_messaging(self, project_dir):
        # Push=on pulls firebase-messaging into gradle deps, which IS in
        # the SDK catalogue — so the privacy gate has at least one match.
        from backend.mobile_compliance import run_all

        render_project(project_dir, _default_opts(push=True))
        bundle = run_all(project_dir, platform="android")
        privacy = bundle.get("privacy_labels")
        assert privacy is not None
        assert privacy.verdict.value == "pass", privacy.summary
        # Check that the detected SDK matches firebase messaging.
        detected = privacy.detail.get("detected_sdks") or []
        joined = " ".join(detected).lower()
        assert "firebase" in joined or "messaging" in joined


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot-validation integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotReport:
    def test_pilot_report_aggregates_all_gates(self, project_dir):
        opts = _default_opts()
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["skill"] == "skill-android"
        assert report["p0_profile"]["min_os_version"] == "24"
        assert report["p0_profile"]["sdk_version"] == "35"
        assert report["p2_simulate_autodetect"] == "espresso"
        assert report["p5_play_metadata"]["present"] is True
        assert report["p5_play_metadata"]["package_matches"] is True
        assert report["p6_compliance"]["passed"] is True

    def test_pilot_report_options_round_trip(self, project_dir):
        opts = _default_opts(package_id="com.example.pilot.app")
        render_project(project_dir, opts)
        report = pilot_report(project_dir, opts)
        assert report["options"]["package_id"] == "com.example.pilot.app"
        assert report["options"]["push"] is True
        assert report["options"]["billing"] is True


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
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert 'applicationId = "com.acme.production.app"' in gradle

    def test_explicit_package_id_propagates_to_metadata(self, project_dir):
        opts = _default_opts(package_id="com.acme.production.app")
        render_project(project_dir, opts)
        meta = json.loads((project_dir / "PlayStoreMetadata.json").read_text())
        assert meta["package_name"] == "com.acme.production.app"

    def test_explicit_package_id_propagates_to_appfile(self, project_dir):
        opts = _default_opts(package_id="com.acme.production.app")
        render_project(project_dir, opts)
        app = (project_dir / "fastlane/Appfile").read_text()
        assert 'package_name("com.acme.production.app")' in app

    def test_package_prefix_strips_last_component(self):
        opts = ScaffoldOptions(project_name="x", package_id="com.example.acme.foo")
        assert opts.package_prefix() == "com.example.acme"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Gradle toolchain invariants (matches P1 Docker image)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolchainPins:
    def test_gradle_wrapper_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        wrapper = (project_dir / "gradle/wrapper/gradle-wrapper.properties").read_text()
        assert "gradle-8.7-bin.zip" in wrapper

    def test_kotlin_version_pinned(self, project_dir):
        render_project(project_dir, _default_opts())
        root = (project_dir / "build.gradle.kts").read_text()
        assert 'version("2.0.0")' in root or 'version "2.0.0"' in root

    def test_jvm_target_17(self, project_dir):
        render_project(project_dir, _default_opts())
        gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert "VERSION_17" in gradle
        assert 'jvmTarget = "17"' in gradle

    def test_compose_plugin_declared(self, project_dir):
        render_project(project_dir, _default_opts())
        root = (project_dir / "build.gradle.kts").read_text()
        app_gradle = (project_dir / "app/build.gradle.kts").read_text()
        assert "org.jetbrains.kotlin.plugin.compose" in root
        assert "org.jetbrains.kotlin.plugin.compose" in app_gradle
