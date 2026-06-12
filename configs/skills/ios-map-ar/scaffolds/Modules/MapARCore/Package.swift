// swift-tools-version: 5.9
//
// ios-map-ar (OP-2124) - Linux-testable core package for map/AR domain code.

import PackageDescription

let package = Package(
    name: "MapARCore",
    defaultLocalization: "en",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(name: "MapARCore", targets: ["MapARCore"]),
    ],
    targets: [
        .target(
            name: "MapARCore",
            swiftSettings: [
                .enableExperimentalFeature("StrictConcurrency=complete"),
                .unsafeFlags(["-warnings-as-errors"], .when(configuration: .release)),
            ]
        ),
        .testTarget(
            name: "MapARCoreTests",
            dependencies: ["MapARCore"]
        ),
    ]
)
