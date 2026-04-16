# SKILL-IOS — P7 #292 (pilot)

First mobile-vertical skill pack. Generates a SwiftUI 6 + Swift 5.9
project that exercises every P0-P6 capability the framework ships.

## Why this skill exists

Priority P built six mobile scaffolding layers — platform profile schema
(P0), mobile toolchains (P1), simulate-track (P2), signing chain (P3),
mobile role skills (P4), store submission (P5), and store compliance
gates (P6). On their own those layers are a framework; they become
load-bearing only after a real skill pack consumes every one of them.
SKILL-IOS is that pack — same pilot pattern D1 set for C5, D29 set for
C26, and W6 set for the web vertical.

## Outputs

A rendered Xcode project tree that:

- builds with `xcodebuild -sdk iphoneos -configuration Release` 0 warning
  (when invoked from a macOS host with Xcode 16; Linux hosts delegate
  through `OMNISIGHT_MACOS_BUILDER` per P1 #286)
- pins `IPHONEOS_DEPLOYMENT_TARGET` to `configs/platforms/ios-arm64.yaml`
  `min_os_version` (16.0) — the StoreKit 2 floor
- passes `scripts/simulate.sh --type=mobile --module=ios-arm64
  --mobile-app-path=<rendered-project>` (P2 simulate-track)
- passes the P6 ASC + Privacy gates on a fresh render
- can be `submit_to_store()`'d through `backend/app_store_connect.py`
  (P5 store-submission, gated by P3 codesign chain)

## Choice knobs

| Knob               | Values                          | Default      |
|--------------------|---------------------------------|--------------|
| `package_manager`  | `spm` \| `cocoapods` \| `both`  | `spm`        |
| `push`             | `on` \| `off`                   | `on`         |
| `storekit`         | `on` \| `off`                   | `on`         |
| `compliance`       | `on` \| `off`                   | `on`         |
| `bundle_id`        | reverse-DNS                     | `com.example.<project>` |

See `configs/skills/skill-ios/tasks.yaml` for the DAG that each knob
routes through.

## How to render

```python
from backend.ios_scaffolder import render_project, ScaffoldOptions

outcome = render_project(
    out_dir=Path("/tmp/MyApp"),
    options=ScaffoldOptions(
        project_name="MyApp",
        bundle_id="com.example.myapp",
        package_manager="spm",
        push=True,
        storekit=True,
        compliance=True,
    ),
)
```

## Framework gates covered

- **P0** mobile platform profile — scaffold reads `ios-arm64` profile,
  honours `min_os_version` and `target_os_version`.
- **P1** mobile toolchain — `Fastfile` template emits `gym` invocation
  matching the P1 builder dispatch matrix; `xcconfig` references
  `DEVELOPER_DIR` / `OMNISIGHT_MACOS_BUILDER`.
- **P2** simulate-track — XCUITest + XCTest wired; project layout
  matches `mobile_simulator.resolve_ui_framework('xcuitest')` markers.
- **P3** codesign chain — scaffold ships `xcconfig` placeholders for
  `CODE_SIGN_IDENTITY` / `PROVISIONING_PROFILE_SPECIFIER` populated at
  build time by `backend/codesign_store.py`; never bakes secrets in.
- **P4** role skills — project style honours the
  `configs/roles/mobile/ios-swift.skill.md` anti-patterns
  (`@Observable` over `ObservableObject`, no `print()` in Release,
  Keychain over UserDefaults for tokens).
- **P5** App Store Connect — `App Store metadata` (`AppStoreMetadata.json`)
  emitted matches the schema `backend/app_store_connect.py::create_version`
  expects.
- **P6** ASC + Privacy gates — `Info.plist`, `PrivacyInfo.xcprivacy`,
  and source files emitted are clean against
  `backend/mobile_compliance.run_all(platform="ios")`.
