# SKILL-FLUTTER — P9 #294 (cross-platform, primary)

Third mobile-vertical skill pack and the first that emits one codebase
covering both iOS (`ios-arm64`) and Android (`android-arm64-v8a`) from a
single render. Flutter 3.22+ / Dart 3.4+.

## Why this skill exists

Priority P built six mobile scaffolding layers (P0-P6). P7 SKILL-IOS
validated them for native iOS; P8 SKILL-ANDROID re-validated the same
surface against a disjoint Android toolchain. P9's contribution is the
first cross-platform pack: does the framework still hold when one pack
consumes *both* `ios-arm64.yaml` and `android-arm64-v8a.yaml`, generates
dual signing surfaces (APNs .p8 + Play keystore), and has to pass
`mobile_compliance.run_all(platform="both")` rather than an
iOS-only or Android-only bundle? That's the n=3 consumer signal.

Flutter is P9's *primary* pick (SKILL-RN is the contrast). Flutter
wins primary because (a) the Dart SDK is a single toolchain surface
for both platforms, (b) `flutter` CLI already autodetects in
`backend.mobile_simulator.resolve_ui_framework` ahead of native
`android/` + `ios/` subdirs, and (c) the P4 `flutter-dart` role skill
already codifies anti-patterns we can gate against in the scaffold.

## Outputs

A rendered Flutter project that:

- builds with `flutter build ipa --release` on macOS + `flutter build
  appbundle --release` on Linux/Docker (the P1 dual-host matrix)
- pins iOS `IPHONEOS_DEPLOYMENT_TARGET` to `configs/platforms/ios-arm64.yaml`
  `min_os_version` and Android `minSdkVersion` to `android-arm64-v8a.yaml`
  `min_os_version`
- passes `scripts/simulate.sh --type=mobile --module=ios-arm64
  --mobile-app-path=<rendered-project>` AND `--module=android-arm64-v8a
  --mobile-app-path=<rendered-project>` (Flutter autodetect wins in
  both, runs `flutter test integration_test/` as the UI framework)
- passes the P6 bundle with `platform="both"` (both ASC + Play + Privacy)
- can be `submit_to_store()`'d through either `backend/app_store_connect.py`
  or `backend/google_play_developer.py` — one codebase, both stores

## Choice knobs

| Knob          | Values          | Default                  |
|---------------|-----------------|--------------------------|
| `package_id`  | reverse-DNS     | `com.example.<project>`  |
| `push`        | `on` \| `off`   | `on`                     |
| `payments`    | `on` \| `off`   | `on`                     |
| `compliance`  | `on` \| `off`   | `on`                     |

`package_id` is shared across both iOS `CFBundleIdentifier` and Android
`applicationId` — the whole point of cross-platform is one id per
product. Renderer rejects an id that isn't valid on either platform.

See `configs/skills/skill-flutter/tasks.yaml` for the DAG each knob
routes through.

## How to render

```python
from backend.flutter_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/MyApp"),
    options=ScaffoldOptions(
        project_name="MyApp",
        package_id="com.example.myapp",
        push=True,
        payments=True,
        compliance=True,
    ),
)
```

## Framework gates covered

- **P0** mobile platform profiles — scaffold reads *both*
  `ios-arm64` and `android-arm64-v8a` profiles, honours
  `min_os_version` on each surface (iOS Podfile + Info.plist + ios
  project settings; Android `minSdk` in `app/build.gradle.kts`).
- **P1** mobile toolchain — Fastfile emits iOS lane through
  `OMNISIGHT_MACOS_BUILDER` and Android lane through the Linux Docker
  image dispatch path. Same `gym` / `supply` pattern as P7 + P8.
- **P2** simulate-track — `integration_test/app_test.dart` wired;
  `mobile_simulator.resolve_ui_framework(...)` returns `"flutter"` for
  the rendered project (pubspec.yaml beats native subdirs — that's the
  autodetect order P2 explicitly pins).
- **P3** codesign chain — iOS `ExportOptions.plist` and Android
  `key.properties` both reference `OMNISIGHT_*` env vars. Scaffold
  MUST NOT bake cert hashes or keystore binaries; `.gitignore` excludes
  `*.jks` / `*.keystore` / `*.p12` / `key.properties` / `google-services.json`.
- **P4** role skills — code honours `configs/roles/mobile/flutter-dart.skill.md`
  anti-patterns (no `print()`, use `debugPrint`; Riverpod preferred over
  `setState` for state shared beyond a single widget; `mounted` guard
  across `async` gaps).
- **P5** ASC + Play submission — emits BOTH `AppStoreMetadata.json`
  (ASC `create_version` shape) AND `PlayStoreMetadata.json` (Play
  `upload_bundle` shape), consumable by their respective adapters.
- **P6** compliance bundle — `Info.plist` + `PrivacyInfo.xcprivacy` on
  the iOS side and `AndroidManifest.xml` + `data_safety.yaml` on the
  Android side are clean against `mobile_compliance.run_all(platform="both")`.

## Pilot-validation checklist

```python
from backend.flutter_scaffolder import pilot_report, ScaffoldOptions

report = pilot_report(out_dir, ScaffoldOptions(project_name="MyApp"))
assert report["p0_ios_profile"]["min_os_version"] == "16.0"
assert report["p0_android_profile"]["min_os_version"] == "24"
assert report["p2_simulate_autodetect"] == "flutter"
assert report["p5_asc_metadata"]["present"], report["p5_asc_metadata"]
assert report["p5_play_metadata"]["present"], report["p5_play_metadata"]
assert report["p6_compliance"]["passed"], report
```

## Common knob recipes

| Recipe                                 | Knobs                              |
|----------------------------------------|------------------------------------|
| Greenfield cross-platform app          | defaults                           |
| Free app (no IAP)                      | `payments=False`                   |
| Open-source OSS pack (no compliance)   | `compliance=False`                 |
| Messaging disabled (enterprise LAN)    | `push=False` — requires operator to add another catalogued SDK so the Privacy gate has something to fingerprint; otherwise it trips "no detected SDKs" |
