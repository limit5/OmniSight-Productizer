# SKILL-ANDROID — Jetpack Compose + Gradle 8 pilot pack (P8 #293)

**Pack status**: n=2 consumer of the P0-P6 mobile framework. Pairs with
SKILL-IOS (P7 #292 — the pilot) to demonstrate the framework works for
two disjoint mobile toolchains, not just the iOS pilot path.

## When to use

- You're starting a new Android app that will ship to the Google Play
  Store and want the P0-P6 framework wired (platform profile binding,
  toolchain delegation, simulate-track autodetect, keystore chain, store
  submission adapter, Play policy + Data Safety gate).
- You need a template that is known-green under `mobile_compliance.run_all
  (platform="android")` out of the box.
- You want a scaffold that honours the role anti-patterns captured in
  `configs/roles/mobile/android-kotlin.skill.md`.

## Knobs

| Knob              | Values                      | Default          | Notes                                                                                                    |
| ----------------- | --------------------------- | ---------------- | -------------------------------------------------------------------------------------------------------- |
| `project_name`    | any XML-safe identifier     | (required)       | Becomes the Compose Activity class, the Gradle module name, and the Play Store listing title.           |
| `package_id`      | reverse-DNS (e.g. com.x.y)  | `com.example.<n>`| Maps to `applicationId` + AndroidManifest `package`. Play accepts only this shape.                      |
| `push`            | `true` / `false`            | `true`           | `true` wires `FirebaseMessagingService` + the FCM manifest entries.                                     |
| `billing`         | `true` / `false`            | `true`           | `true` wires `BillingClient` + the `com.android.billingclient:billing-ktx` dependency.                  |
| `compliance`      | `true` / `false`            | `true`           | `true` ships the `docs/play/data_safety.yaml` and Play metadata artifacts.                              |
| `min_sdk`         | integer                     | `24` (from P0)   | You rarely override; P0 pins the floor via `android-arm64-v8a.yaml::min_os_version`.                    |

## Render API

```python
from pathlib import Path
from backend.android_scaffolder import ScaffoldOptions, render_project, pilot_report

opts = ScaffoldOptions(
    project_name="PilotApp",
    package_id="com.example.pilot",
    push=True,
    billing=True,
    compliance=True,
)
outcome = render_project(Path("/tmp/android-pilot"), opts)
print(outcome.files_written)

# Pilot framework smoke — runs P0 / P2 / P5 / P6 gates in one shot.
report = pilot_report(Path("/tmp/android-pilot"), opts)
assert report["p0_profile"]["min_os_version"] == "24"
assert report["p2_simulate_autodetect"] == "espresso"
assert report["p6_compliance"]["passed"]
```

## Framework gate cover

| Gate                      | How this pack exercises it                                                                         |
| ------------------------- | -------------------------------------------------------------------------------------------------- |
| `p0-platform-profile`     | `_render_context()` reads `android-arm64-v8a.yaml` and pins `minSdk` / `targetSdk` into Gradle.    |
| `p1-mobile-toolchain`     | `fastlane/Fastfile` runs inside the `ghcr.io/omnisight/mobile-build` Docker image.                 |
| `p2-simulate-unit`        | `app/src/test/java/…/ExampleUnitTest.kt` — JUnit4 unit surface.                                    |
| `p2-simulate-ui`          | `app/src/androidTest/java/…/MainActivityTest.kt` — Espresso; detected by `resolve_ui_framework`.   |
| `p3-codesign-chain`       | `keystore.properties.example` + `app/build.gradle.kts` use `$OMNISIGHT_KEYSTORE_*` placeholders.   |
| `p4-role-android-kotlin`  | Compose idioms only; no `System.out.println` / `Log.d` in code (only comments).                    |
| `p5-store-submission`     | `PlayStoreMetadata.json` has the shape consumed by `google_play_developer.upload_bundle`.          |
| `p6-compliance`           | `docs/play/data_safety.yaml` ships pre-filled; Play + Privacy gates pass on fresh render.          |
| `p8-pilot`                | `pilot_report()` aggregates all of the above.                                                      |

## Common recipes

### Minimal app (no FCM, no Billing)

```python
ScaffoldOptions(project_name="Minimal", push=False, billing=False)
```

No Firebase or Play Billing deps in gradle; P6 Play gate still passes
(Data Safety form lists only first-party androidx libs).

### FCM-only (no Billing)

```python
ScaffoldOptions(project_name="Pushy", push=True, billing=False)
```

Used by content apps that push notifications but don't sell IAPs.

### Billing-only (no FCM)

```python
ScaffoldOptions(project_name="Storefront", push=False, billing=True)
```

Used by storefront apps that sell but don't push.

### Compliance-off (pre-submission sandbox)

```python
ScaffoldOptions(project_name="Sandbox", compliance=False)
```

Skips `docs/play/data_safety.yaml` + `PlayStoreMetadata.json`. Useful
for internal demos; **must not** be shipped to Play — Play rejects
submissions without a completed Data Safety form.

### Custom package id

```python
ScaffoldOptions(project_name="Prod", package_id="com.acme.prod.app")
```

`package_id` propagates to `app/build.gradle.kts` `applicationId`, the
`AndroidManifest.xml` package attribute, the Fastlane Appfile, and the
Play metadata summary.
