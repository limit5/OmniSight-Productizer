// SKILL-IOS (P7 #292) — example feature module.
//
// Lives outside the app target so the SPM dependency surface is
// exercised from day one. Demonstrates `@Observable` over the
// deprecated `ObservableObject`+`@Published` pair (P4 ios-swift role
// anti-pattern).
//
// Side-effect-free: counter mutation happens through `increment()`,
// not by calling fetchData() inside a SwiftUI body. SwiftUI views
// should call `.task { ... }` if they need to drive this.

import Foundation
import os

@Observable
public final class FeatureCounter {
    public private(set) var value: Int

    private static let logger = Logger(subsystem: "com.omnisight.skill-ios", category: "FeatureCounter")

    public init(initial: Int = 0) {
        self.value = initial
    }

    public func increment() {
        value &+= 1
        Self.logger.debug("FeatureCounter incremented to \(self.value, privacy: .public)")
    }

    public func reset() {
        value = 0
    }
}
