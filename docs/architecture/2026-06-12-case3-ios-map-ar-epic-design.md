# EPIC design — Case 3 iOS map-AR app (T1, software-only)

Date: 2026-06-12
Status: v2 — Plan-agent review folded (11 findings: 4 MUST + 2 SHOULD + 5 NOTE); ready for operator +2 → wave filing
Template: Case-1 camviewpro feature-push EPIC + Case-2 UVCCamera_Qt CI-matrix precedent
Related: configs/skills/ios-map-ar (OP-1820), configs/skills/skill-ios,
backend/ios_scaffolder.py, product_source/delivery_target/github_pr_target (B2 flywheel).

## 0. The 4-line summary

Case 3 = the last untouched T1 customer: an iOS app combining ARKit + MapKit
(AR wayfinding / POI anchored to map locations). Pack + scaffolder exist with
real SwiftUI sources (**render-verified only — never compiled**); this EPIC
creates the product repo, proves the macOS build loop without owning a Mac
(GitHub Actions macOS runner + XcodeGen), and makes the app real. On-device AR
verification (ARKit needs a physical iPhone) is the only deferred phase.

## 1. Premise-check summary (Stage 1 SOP; reviewer-verified)

- **Pack content is real for map/store/UI, placeholder for AR anchoring**:
  ios-map-ar ships MapARHomeView/MapKitMapView/MapLocationStore/PointOfInterest
  + ARKitOverlayView — but anchor placement is a hardcoded demo layout
  (ARKitOverlayView.swift:58-62); geo-anchoring math is C3.B/C3.C scope.
  The 2026-05-28 "dry-run" verified **rendering only; the Swift has never been
  compiled** (no macOS host) — first macOS CI run WILL be red (see D1 notes).
- **Scaffold shape = XcodeGen, not an .xcodeproj and not a SwiftPM app**:
  skill-ios ships `project.yml.j2` (XcodeGen spec; .xcodeproj materialized at
  build time per ios_scaffolder.py:308-310); scheme name = `{{project_name}}`;
  `Package.swift` defines only a `<Name>Feature` library with
  project-yml `dependencies: []`. Build mechanics in D1 reflect this.
- **Known latent CI-red defects to fix-forward at first compile** (reviewer
  located): ContentViewTests references `FeatureCounter` not linked into the
  test target; base `UITests/SmokeTests` asserts ContentView/counter/StoreKit
  UI the map-ar overlay removed; `SWIFT_TREAT_WARNINGS_AS_ERRORS` +
  `SWIFT_STRICT_CONCURRENCY: complete` on never-compiled code. Scaffold run
  uses `storekit=false, push=false`; overlay overrides/deletes stale tests.
- **macOS constraint solved by precedent**: Case-2 GH-Actions matrix
  (docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md).
  GitHub `macos-14` runners ship Xcode; simulator build+test needs **no
  signing**. XcodeGen is NOT preinstalled → CI installs it (brew).
- **Swift-on-Linux fast loop requires a hard import rule**: Linux Swift has
  corelibs Foundation only — **no CoreLocation** (current PointOfInterest/
  MapLocationStore import it). MapARCore therefore bans ALL Apple-framework
  imports (incl. CoreLocation/MapKit) and defines its own `GeoCoordinate`;
  conversion extensions + the CoreLocation wrapper live in the app target.
- **AR in simulator**: `ARWorldTrackingConfiguration.isSupported == false` on
  simulator and the current overlay starts the session unconditionally →
  guard + `.nonAR` RealityKit fallback is an explicit leaf (it also gives
  simulator screenshot tests real anchor-geometry coverage — the cheapest
  pre-HIL verification; ARKit session replay is device-only, not CI-reachable).
- **ARGeoAnchor rejected deliberately**: ARGeoTracking is region-gated (not
  available in Taiwan) — manual geo math (`gravityAndHeading` + GPS/bearing)
  is the portable approach; accuracy tuning = HIL.
- **No product repo exists yet** — creation is a Wave-1 operator leaf.

## 2. Target architecture

```
configs/skills/ios-map-ar ──scaffold──▶ limit5/mapar-ios (GitHub; final name =
                                        operator pick at repo creation)
        XcodeGen project (project.yml → .xcodeproj at CI time)
            ├─ MapARCore  (local SwiftPM pkg: NO Apple frameworks;
            │              GeoCoordinate, geo→ENU/anchor math, POI model/store;
            │              Linux-testable)
            └─ MapARApp   (SwiftUI + ARKit + MapKit + CoreLocation wrapper;
                           macOS-runner-only build)
   CI: GH Actions ── macos-14: brew install xcodegen && xcodegen generate &&
                     xcodebuild -project <Name>.xcodeproj -scheme <Name>
                       -destination 'platform=iOS Simulator,…' build test
                     (DEVELOPER_DIR pins Xcode; simctl privacy pre-grant)
                  └─ ubuntu: swift test (MapARCore only — fast loop)
   HIL (deferred): operator iPhone — AR session smoke + geo-accuracy
```

Architectural decisions:
- **D1 build loop**: macos-14 + pinned Xcode (`DEVELOPER_DIR`), `brew install
  xcodegen`, `xcodegen generate`, then `xcodebuild -project … -scheme <Name>`
  simulator build+test (no signing). First-compile is an explicit
  **fix-forward AC** on the scaffold-deliver leaf (known red list in §1).
  Ubuntu runs `swift test` on MapARCore every push; the macOS job is
  **gated** (label `ci:macos` or merge-queue) to control paid minutes.
- **D2 module split**: MapARCore = local SwiftPM package wired into the app
  via project.yml `packages:`/`dependencies:` (an explicit edit — currently
  `[]`). Hard rule: **no Apple-framework imports of any kind in MapARCore**
  (incl. CoreLocation); it owns `GeoCoordinate` etc.; conversions + the
  CLLocationManager wrapper live in MapARApp.
- **D3 signing/distribution: OUT of scope** (no Apple Developer account
  assumed; HIL install uses operator's free personal team). App-icon/asset-
  catalog **pipeline** is in scope (one leaf — empty catalog + AppIcon slot;
  HIL installs aren't blank, future ASC validation won't trip
  `CFBundleIconName`); branding/content stays out. The vestigial
  `NSUserTrackingUsageDescription` is deleted in the overlay (privacy strings
  for location+camera already present and stay).
- **D4 verification ladder**: (1) ubuntu: MapARCore unit tests (geo math vs
  fixtures); (2) macos CI: compile + unit tests + XCUITest with
  `simctl privacy … grant location-when-in-use` pre-grant; AR view renders
  the **`.nonAR` fallback** in simulator so screenshot tests cover anchor
  placement geometry; (3) deferred HIL: real-iPhone AR session smoke +
  geo-accuracy (explicitly NOT verifiable in CI).
- **D5 repo + routing**: product repo on GitHub under limit5 (name = operator
  pick; placeholder `limit5/mapar-ios`). **Visibility decision at creation:
  public → macOS minutes free; private → 10x multiplier means ~200 effective
  macOS-min/month on free tier (a 10-25-min job burns 100-250/PR) → the
  macOS-job gating in D1 is mandatory, or budget paid minutes.** Registered as
  ProductSource (pinned_ref) + delivery_target; contributions via
  github_pr_target (OP-1862 worktree-origin routing lesson already baked in);
  register the ProductSource row BEFORE the first autonomous PR.

## 3. Sub-EPIC breakdown (4, ~17 leaves)

- **C3.A repo + pipeline bring-up** (~5, strictly serial — see §4): product
  repo creation + visibility decision + git_accounts/ProductSource/
  delivery_target registration **[operator, tier:X]**; scaffold dry-run →
  deliver-to-repo (render with `storekit=false,push=false`; override/delete
  stale base tests; **first-compile fix-forward AC**); GH Actions CI
  (macos-14 XcodeGen flow + ubuntu SwiftPM, macOS job gated, Xcode pinned);
  MapARCore module split (project.yml packages wiring + CI rework budgeted
  into this leaf); asset-catalog pipeline + Info.plist privacy-string cleanup.
- **C3.B core domain make-real** (~4): `GeoCoordinate` + geo→ENU/anchor math
  module (bearing/distance/altitude transform, unit-tested vs fixtures,
  Linux-runnable); POI model + persistent store rewritten Apple-framework-free
  (JSON first pass) + tests; CoreLocation wrapper (protocol-mocked,
  **app-target**, macOS-CI-tested); offline POI seed dataset + loader.
- **C3.C map + AR UI make-real** (~5): MapKit screen real wiring (POI pins,
  region tracking, selection→detail); **AR availability guard +
  `.nonAR` simulator fallback**; ARKit overlay geo-anchored placement fed by
  MapARCore math (replaces the hardcoded demo layout); map↔AR mode transition
  + state preservation; diagnostics/settings screen (location status, anchor
  debug readout).
- **C3.D verification + delivery** (~3): simulator XCUITest suite (launch,
  map loads, POI list, mode switch, `.nonAR` AR screenshot; permission
  pre-grant in CI); HIL runbook + operator on-device AR smoke checklist
  **[operator, tier:X, parks until iPhone]**; Case-3 delivery runbook
  (ProductSource row, CI gates, contribution flow).

~17 leaves (15 codex + 2 operator). H10 ≤3/batch; H9 gates in labels/blockedBy.

## 4. Phasing + calendar

- **Wave 1**: C3.A — intentionally serial (repo[operator] → scaffold-deliver →
  CI → module split → assets); CI lands before the split on purpose (surfaces
  the §1 latent reds earliest); the split leaf budgets the CI rework.
- **Wave 2**: C3.B + C3.C (parallel, file-disjoint by module)
- **Wave 3**: C3.D (verification + runbooks; HIL leaf parks until iPhone)
- Codex code wall-clock ~2-4 h/wave; calendar at sibling-measured review
  cadence (0.5-2 days/leaf × ~17) ≈ **2-4 weeks** to sim-verified close.

## 5. Risks

| Risk | Mitigation |
|---|---|
| XcodeGen/Xcode/iOS-SDK drift on runner | pin macos-14 + `DEVELOPER_DIR`; xcodegen version pinned in workflow; CI fails loudly |
| first compile of never-built Swift | known-red list in §1 + fix-forward AC on the deliver leaf |
| AR session unsupported in simulator | availability guard + `.nonAR` fallback leaf; screenshot tests run against fallback |
| location permission dialog blocks XCUITest | `simctl privacy … grant` pre-grant step in CI |
| macOS minutes cost (private repo 10x) | visibility decision at creation; macOS job gated; ubuntu fast loop on every push |
| geo→AR accuracy (GPS/heading noise) | math unit-tested vs fixtures; accuracy = HIL, marked not-CI-verifiable; ARGeoAnchor rejected (region-gated, no Taiwan) |
| codex cannot run Xcode locally | D2 split keeps codex's loop on Linux SwiftPM; UI verified by CI (Case-2 remote-verify pattern) |
| repo-routing mistakes | OP-1862 lesson in github_pr_target; ProductSource registered before first autonomous PR |

## 6. What this EPIC does NOT do

App Store / TestFlight distribution + signing; real-device AR verification
(deferred HIL leaf, operator iPhone); customer branding/data/POI content;
backend services for shared POI (offline-seed first); push notifications,
StoreKit, accounts (scaffold knobs OFF); localization beyond en + zh-TW shell;
ARGeoAnchor/ARGeoTracking (region-gated — deliberately not used).

## 7. Next gates

1. ~~Plan-agent adversarial review~~ ✅ folded (this rev)
2. Operator +2 of this doc on Gerrit
3. File META + 4 Sub-EPIC parents (tier:X) + Wave-1 (repo leaf = operator
   tier:X; codex leaves ≤3/batch)

## 8. Review fold log (v1→v2)

MUST: (1) XcodeGen build mechanics (install+generate, -project, real scheme
name, project.yml packages wiring, diagram fixed); (2) MapARCore bans ALL
Apple frameworks incl CoreLocation; owns GeoCoordinate; CL wrapper →
app target; (3) premise reworded render-verified-never-compiled + known-red
list + fix-forward AC + storekit/push off + stale-test override; (4) AR
isSupported guard + `.nonAR` simulator fallback leaf (doubles as the cheapest
pre-HIL verification). SHOULD: (5) simctl privacy pre-grant in CI; (6) repo
visibility decision + macOS-minutes budget + gated macOS job + DEVELOPER_DIR
pin. NOTE: (7) ARGeoAnchor explicitly rejected (Taiwan unavailable); AR
placement honestly labeled placeholder; (8) asset-catalog pipeline leaf +
NSUserTrackingUsageDescription deletion; (9) C3.A serialization acknowledged,
CI-first justified, CI rework budgeted in split leaf; (10) garbled D5 repo
line fixed; (11) HIL deferral confirmed correct (no CI-side AR runtime exists).
