# SKILL-RN — Integration guide

## When to use this pack

Pick `skill-rn` when:
- Your existing stack is React + TypeScript (Next.js web app, shared
  component library) and shipping RN reuses that muscle.
- You're OK with a heavier native-bridge surface than Flutter's Dart
  embedding in exchange for React-ecosystem ergonomics.
- You want a single render that targets both App Store + Play with the
  New Architecture (Fabric + TurboModules) on.

Skip when:
- Greenfield with no React code to share — SKILL-FLUTTER is simpler,
  single-toolchain.
- Native iOS-only → `skill-ios`; native Android-only → `skill-android`.

## Required environment

Same envelope as SKILL-FLUTTER — see
`configs/skills/skill-flutter/docs/integration_guide.md`; P3 / P5 / P6
prerequisites are identical because they all consume the same mobile
framework gates.

## The dual-host build requirement

- `npx react-native build-ios --mode=Release` → **macOS + Xcode** only.
  The Fastfile's `ios_beta` lane goes through `OMNISIGHT_MACOS_BUILDER`
  and fails fast on Linux if the env var is missing.
- `npx react-native build-android --mode=Release` → Linux via the P1
  Docker image; `OMNISIGHT_KEYSTORE_*` env must be populated.

## New Architecture toggle

The scaffold sets `newArchEnabled=true` in `android/gradle.properties`
and `RCT_NEW_ARCH_ENABLED=1` in the iOS Podfile. RN 0.76+ requires
TurboModules for new plugins; legacy NativeModules still work via the
interop layer but shouldn't be added net-new — the `react-native` role
skill pins this as an anti-pattern.

## Pilot-validation

```python
from backend.rn_scaffolder import pilot_report, ScaffoldOptions

report = pilot_report(out_dir, ScaffoldOptions(project_name="MyApp"))
assert report["p0_ios_profile"]["min_os_version"] == "16.0"
assert report["p0_android_profile"]["min_os_version"] == "24"
assert report["p2_simulate_autodetect"] == "react-native"
assert report["p5_asc_metadata"]["package_matches"]
assert report["p5_play_metadata"]["package_matches"]
assert report["p6_compliance"]["passed"], report
```

## Knob recipes

| Recipe                                | Knobs                |
|---------------------------------------|----------------------|
| Greenfield RN cross-platform app      | defaults             |
| Free app (no IAP)                     | `payments=False`     |
| OSS (no compliance)                   | `compliance=False`   |
| Messaging disabled (caveat — see note below) | `push=False`  |

Note on `push=off` + `compliance=on`: same trade-off as SKILL-FLUTTER.
With push disabled and no other catalogued SDK in deps, the Privacy
gate trips on "no detected SDKs". Operators add another catalogued SDK
(`sentry`, `amplitude`, `revenuecat`…) or extend
`configs/privacy_label_sdks.yaml`.

## Cross-pack swap

`backend.rn_scaffolder.ScaffoldOptions` has the same public fields as
`backend.flutter_scaffolder.ScaffoldOptions` (project_name, package_id,
push, payments, compliance). Same-shape `RenderOutcome`. Same-shape
`pilot_report` dict. Swap the import and re-render — the rest of your
orchestration pipeline is stable.
