# SKILL-ANDROID Integration Guide (P8 #293)

## When to use this pack

Reach for SKILL-ANDROID when you're bootstrapping a **new Android SKU**
that needs to ship to the Google Play Store. The pack is designed to
take the P0-P6 framework from "rendered files on disk" to "pilot report
green" in a single `render_project()` call.

If the app will also ship on iOS, render SKILL-IOS (P7 #292) in parallel
— the two packs share the P0 platform profile contract, the P1 toolchain
delegation, and the P6 compliance harness. Cross-platform apps that
want a shared codebase should reach for SKILL-FLUTTER or SKILL-RN
(P9 #294) instead.

## Prerequisites per layer

| Layer | What must exist before `render_project()` works                          |
| ----- | ------------------------------------------------------------------------ |
| P0    | `configs/platforms/android-arm64-v8a.yaml` present. It ships in the repo.|
| P1    | Docker daemon accessible on the CI host OR a fallback to a host Gradle. |
| P3    | Optional for render — required before `fastlane build` at submission.    |
| P5    | Play Console app record created + a service-account JSON in secret_store.|
| P6    | `configs/privacy_label_sdks.yaml` catalogue present. Ships in repo.      |

None of P1 / P3 / P5 are strictly required for **render**. They are
required before the rendered project can build / sign / submit.

## Linux-host requirement (toolchain)

Unlike SKILL-IOS which requires a macOS builder for `fastlane gym`,
SKILL-ANDROID's toolchain runs cleanly on any Linux CI runner. The P1
Docker image (`ghcr.io/omnisight/mobile-build`) bakes in JDK 17 + Android
SDK 35 + NDK r27 + Gradle 8.7 + Fastlane 2.221 — a single image satisfies
both Android ABIs (arm64-v8a + armeabi-v7a).

No `OMNISIGHT_MACOS_BUILDER` env is required; the `Fastfile` does not
enforce a host-OS gate.

## Pilot-validation checklist

After `render_project()`, run:

```bash
python3 - <<EOF
from pathlib import Path
from backend.android_scaffolder import ScaffoldOptions, render_project, pilot_report

out = Path("/tmp/android-pilot")
opts = ScaffoldOptions(project_name="PilotApp", push=True, billing=True)
render_project(out, opts)
report = pilot_report(out, opts)

for key in ("p0_profile", "p2_simulate_autodetect", "p5_play_metadata", "p6_compliance"):
    print(key, report[key])
EOF
```

Expected output:

- `p0_profile`: `{"platform": "android-arm64-v8a", "min_os_version": "24", "sdk_version": "35", ...}`
- `p2_simulate_autodetect`: `"espresso"`
- `p5_play_metadata`: `{"present": True, "package_matches": True, ...}`
- `p6_compliance`: `{"passed": True, ...}`

If any of the four lines drifts from the expected shape, a framework
invariant has regressed — file a bug with the pilot report attached.

## Common knob recipes

### FCM push only

```python
ScaffoldOptions(project_name="News", push=True, billing=False)
```

Adds `com.google.firebase:firebase-messaging` to the gradle deps and
`FcmMessagingService` to the manifest. The Privacy gate detects the
SDK and the Data Safety form lists it under "Device or other IDs".

### Play Billing only

```python
ScaffoldOptions(project_name="Store", push=False, billing=True)
```

Adds `com.android.billingclient:billing-ktx` + a `BillingClientManager`
wrapper. The `PurchasesUpdatedListener` callback routes to
`verifyPurchase()` which is a stub that **must** be replaced with a
server-side Play Developer API verification call before shipping — the
comment at the top of `BillingClientManager.kt` says so.

### Full offline scaffolding

Every file the pack writes is deterministic. There is zero network
traffic during render — the Jinja templates only pull from the
repository-local `android-arm64-v8a.yaml` profile. Tests run in a
sandbox with no internet.

## What NOT to edit in the rendered output

- `keystore.properties.example` — this is a placeholder template. Add
  a real `keystore.properties` (gitignored) for local testing, and wire
  production values via `backend/codesign_store.materialize_env()`.
- `google-services.json` — the scaffold intentionally does NOT ship one.
  Drop the real file into `app/` at build time (it's a runtime config
  that varies by environment).
- `docs/play/data_safety.yaml` — this is the Play Data Safety form
  upload source. Edit to reflect your app's actual data collection.
- `PlayStoreMetadata.json` — edit the listing copy, but preserve the
  shape expected by `backend/google_play_developer.upload_bundle`.

## What the pack DOES NOT provide

- A Kotlin Multiplatform shared module (`shared/`). Out of scope —
  render a separate KMP skill if that's the target.
- Proguard / R8 optimisation rules beyond the default `proguard-rules.pro`.
  Add app-specific keep rules per feature.
- Jetpack Hilt wiring. The scaffold keeps DI surface area small; wire
  Hilt / Koin / Anvil yourself once the structure is clear.
