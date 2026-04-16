# SKILL-IOS — Integration guide

## When to use this pack

Pick `skill-ios` when:
- You need a Swift/SwiftUI iOS app, native (not Flutter / RN — those
  live in P9 #294).
- The target ships through App Store Connect (TestFlight + production)
  via the P5 `backend/app_store_connect.py` adapter.
- You want the P6 ASC + Privacy compliance gates green from day one.

Skip this pack when:
- The project is iOS-only **but** must hit the App Store via
  enterprise distribution only — the scaffold assumes ASC, not ad-hoc
  enterprise IPA mailout.
- You need Catalyst (macOS-from-iOS) — out of scope for the pilot, will
  be a separate skill.

## Required environment

| Layer  | Needed before render                                              |
|--------|-------------------------------------------------------------------|
| P0     | `configs/platforms/ios-arm64.yaml` checked in                     |
| P1     | `OMNISIGHT_MACOS_BUILDER` set if rendering on Linux               |
| P3     | Codesign material registered in `backend/codesign_store.py`       |
| P5     | App Store Connect API key in `backend/secret_store`               |
| P6     | `configs/privacy_label_sdks.yaml` populated (or use pack default) |

If P3 / P5 are absent the scaffold still renders — the placeholders are
clearly marked `# TODO(P3)` / `# TODO(P5)` so a developer can render
locally and wire secrets later.

## The macOS-host requirement

Pure iOS builds require a macOS host. The scaffold itself is render-only
(works on Linux), but:

- `xcodebuild` / `xcrun` are macOS-only.
- `gym` / Fastlane needs Xcode.
- StoreKit 2 sandbox testing needs a real device or Simulator.

P1 (#286) supplies the `OMNISIGHT_MACOS_BUILDER` delegation matrix
(`self-hosted` / `macstadium` / `cirrus-ci` / `github-macos-runner`).
The rendered Fastfile honours that env directly — no per-project
re-wiring.

## Pilot-validation checklist

Run after `render_project()`:

```python
from backend.ios_scaffolder import pilot_report, ScaffoldOptions

report = pilot_report(out_dir, ScaffoldOptions(project_name="MyApp"))
assert report["p6_compliance"]["passed"], report
assert report["p2_simulate_autodetect"] == "xcuitest"
assert report["p0_profile"]["min_os_version"] == "16.0"
```

## Common knob recipes

| Recipe                          | Knobs                                                |
|---------------------------------|------------------------------------------------------|
| Greenfield SwiftUI app          | defaults                                             |
| Migrate legacy CocoaPods app    | `package_manager="cocoapods"`                        |
| Free app (no IAP)               | `storekit=False`                                     |
| Background-app refresh only     | `push=True` (still required for silent push)         |
| Open-source pack (no compliance)| `compliance=False` — drops PrivacyInfo + ASC gates   |
