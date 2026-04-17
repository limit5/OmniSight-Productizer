# SKILL-FLUTTER — Integration guide

## When to use this pack

Pick `skill-flutter` when:
- You want **one codebase** on both iOS and Android and you're OK
  staying inside the Flutter widget tree (Material 3 / Cupertino).
- The app ships through **both** App Store Connect (TestFlight +
  production) AND Google Play Console (internal → closed → production).
- You want the P6 compliance bundle green on `platform="both"` from
  day one.

Skip this pack when:
- You need deep-native iOS platform APIs with no plugin cover (e.g.
  CoreML on-device inference, StoreKit advanced subscription offers);
  prefer `skill-ios` + `skill-android` duplex with a shared business
  layer.
- You need React ecosystem integration (same company's web app is
  React + Next.js, shared component library) — `skill-rn` is the
  React-ecosystem cross-platform option.

## Required environment

| Layer  | Needed before render                                                  |
|--------|-----------------------------------------------------------------------|
| P0     | `configs/platforms/ios-arm64.yaml` + `android-arm64-v8a.yaml`         |
| P1     | `OMNISIGHT_MACOS_BUILDER` set if rendering on Linux (iOS rail only)   |
| P3     | iOS + Android signing material registered in `backend/codesign_store.py` |
| P5     | App Store Connect API key + Play Developer service-account both in `backend/secret_store` |
| P6     | `configs/privacy_label_sdks.yaml` populated (default catalogue OK)    |

If any P3 / P5 surface is absent the scaffold still renders — the
placeholders are clearly marked `# TODO(P3)` / `# TODO(P5)` so a
developer can render locally and wire secrets later.

## The dual-host build requirement

Flutter's build surface splits by target:

- `flutter build ipa` requires **macOS + Xcode** (Apple licensing +
  proprietary linker). P1 supplies `OMNISIGHT_MACOS_BUILDER`
  (self-hosted / macstadium / cirrus-ci / github-macos-runner); the
  Fastfile's `ios_*` lanes fail fast if this is missing on a Linux host.
- `flutter build appbundle` runs happily on **Linux via the P1
  Docker image** (ghcr.io/omnisight/mobile-build ships Dart SDK +
  Android SDK + NDK).

P1 dispatches each lane to the right host automatically — this pack
just emits the Fastfile that binds to that dispatch.

## Pilot-validation checklist

Run after `render_project()`:

```python
from backend.flutter_scaffolder import pilot_report, ScaffoldOptions

report = pilot_report(out_dir, ScaffoldOptions(project_name="MyApp"))
assert report["p0_ios_profile"]["min_os_version"] == "16.0"
assert report["p0_android_profile"]["min_os_version"] == "24"
assert report["p2_simulate_autodetect"] == "flutter"
assert report["p5_asc_metadata"]["package_matches"]
assert report["p5_play_metadata"]["package_matches"]
assert report["p6_compliance"]["passed"], report
```

## Common knob recipes

| Recipe                                 | Knobs                                                  |
|----------------------------------------|--------------------------------------------------------|
| Greenfield cross-platform Flutter app  | defaults                                               |
| Free app (no IAP)                      | `payments=False`                                       |
| Open-source OSS (no compliance)        | `compliance=False`                                     |
| Messaging disabled                     | `push=False` — see caveat below                        |

### Caveat — `push=off` + `compliance=on`

The default push SDK (`firebase_messaging`) is catalogued in
`configs/privacy_label_sdks.yaml`, so when `push=on` the rendered
project has at least one SDK the privacy gate can fingerprint.
`push=off` strips that — an app with zero catalogued SDKs fails the
privacy gate ("no detected SDKs → cannot generate label"). Operators
who disable push must EITHER:

1. Add another catalogued SDK (`sentry`, `amplitude`, `revenuecat`
   are already in the baseline catalogue), OR
2. Extend `configs/privacy_label_sdks.yaml` with whatever SDK they
   use, OR
3. Set `compliance=off` and self-service the Play Data Safety form.

The scaffold picks the first path (adds the catalogued
firebase-messaging dependency) so the default knob matrix stays
green — same trade-off P8 SKILL-ANDROID documents.

## Renderer + non-scaffold file preservation

`render_project(out_dir, overwrite=True)` overwrites the scaffold
surface only. User files under `lib/features/<custom>/` are never
touched — the same idempotency contract P7 + P8 pin. Tests enforce
this explicitly (`test_non_scaffold_files_are_preserved`).

## Cross-platform invariants to watch for

- `package_id` is shared. iOS calls it `CFBundleIdentifier`, Android
  calls it `applicationId`. The scaffold populates both from the
  single knob. Reject an id that doesn't satisfy both platform's
  rules (reverse-DNS + alphanumeric-only segments; no hyphens across
  the whole id or Android gradle will reject; iOS also rejects).
- Fastlane `ios_beta` lane requires `OMNISIGHT_MACOS_BUILDER`.
  Fastlane `android_internal` lane requires `OMNISIGHT_KEYSTORE_*`.
  Each lane fails fast and prints which env var is missing.
