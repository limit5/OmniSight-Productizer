# SKILL-RN — P9 #294 (cross-platform, contrast)

Fourth mobile-vertical skill pack. React Native 0.76+ / TypeScript 5 /
Hermes with the New Architecture (Fabric + TurboModules) on by default.

## Why this skill exists

Contrast pack to SKILL-FLUTTER. Both cover the same cross-platform
slot; SKILL-FLUTTER is the organisation's default recommendation,
SKILL-RN is the React-ecosystem alternative for teams whose existing
stack is already React + TypeScript (typically same-company
Next.js / shared component library).

Running two cross-platform packs side-by-side lets the P0-P6 mobile
framework be validated against two distinctly different JS / Dart
toolchains before we declare the framework "general" — same
n=multiple-consumers strategy the rest of the framework tree uses.

## Outputs

A rendered React Native project that:

- builds with `npx react-native build-ios --mode=Release` on macOS +
  `npx react-native build-android --mode=Release` on Linux (P1 dual-host)
- pins iOS platform version to `configs/platforms/ios-arm64.yaml`
  `min_os_version` via the Podfile and Android `minSdkVersion` to
  `android-arm64-v8a.yaml`
- has Hermes + New Architecture + Fabric enabled via
  `android/gradle.properties` + `ios/Podfile` flags
- passes `mobile_simulator.resolve_ui_framework(...)` as
  `"react-native"` — package.json with `react-native` dep is the
  second-priority marker after pubspec.yaml in the autodetect order
- passes the P6 bundle with `platform="both"`
- can be submitted through either `backend/app_store_connect.py` or
  `backend/google_play_developer.py`

## Choice knobs

| Knob         | Values        | Default                  |
|--------------|---------------|--------------------------|
| `package_id` | reverse-DNS   | `com.example.<project>`  |
| `push`       | `on` \| `off` | `on`                     |
| `payments`   | `on` \| `off` | `on`                     |
| `compliance` | `on` \| `off` | `on`                     |

Same knob surface as SKILL-FLUTTER — the two packs intentionally share
their public-API shape so operators can swap `flutter_scaffolder` ↔
`rn_scaffolder` without rewriting orchestration glue.

## How to render

```python
from backend.rn_scaffolder import render_project, ScaffoldOptions

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

- **P0** mobile platform profiles — scaffold reads BOTH `ios-arm64`
  and `android-arm64-v8a` profiles, pins `Podfile` iOS platform and
  Android `build.gradle` `minSdk` straight from them.
- **P1** mobile toolchain — Fastfile emits iOS lane via
  `OMNISIGHT_MACOS_BUILDER` and Android lane via the Linux Docker
  image.
- **P2** simulate-track — Jest + Detox wired; `run_rn_tests` runs
  `detox test` on both rails.
- **P3** codesign chain — iOS ExportOptions.plist + Android
  `key.properties` both env-ref only. `.gitignore` excludes
  keystores, provisioning profiles, and service-account JSON.
- **P4** role skills — code honours
  `configs/roles/mobile/react-native.skill.md` anti-patterns (no
  `console.log` in production, New Architecture on, Hermes on,
  `StyleSheet.create` not inline styles, `useCallback` / `useMemo`
  in FlatList renderItem).
- **P5** ASC + Play submission — emits both `AppStoreMetadata.json`
  and `PlayStoreMetadata.json`.
- **P6** compliance — `PrivacyInfo.xcprivacy` (iOS) + `data_safety.yaml`
  (Android) + `Info.plist` + `AndroidManifest.xml` all clean against
  `mobile_compliance.run_all(platform="both")`.

## Pilot-validation checklist

```python
from backend.rn_scaffolder import pilot_report, ScaffoldOptions

report = pilot_report(out_dir, ScaffoldOptions(project_name="MyApp"))
assert report["p0_ios_profile"]["min_os_version"] == "16.0"
assert report["p0_android_profile"]["min_os_version"] == "24"
assert report["p2_simulate_autodetect"] == "react-native"
assert report["p6_compliance"]["passed"], report
```

## Primary vs contrast — pick your rail

- Stack you already use React + Next.js + TypeScript? → SKILL-RN.
- New greenfield product, no existing React code to share? → SKILL-FLUTTER.
- Need deep native iOS only? → `skill-ios`.
- Need deep native Android only? → `skill-android`.
