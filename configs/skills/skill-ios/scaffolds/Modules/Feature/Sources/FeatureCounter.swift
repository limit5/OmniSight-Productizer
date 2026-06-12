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
#if canImport(Observation)
import Observation
#endif
#if canImport(os)
import os
#endif

#if canImport(Observation)
@Observable
#endif
public final class FeatureCounter {
    public private(set) var value: Int

#if canImport(os)
    private static let logger = Logger(subsystem: "com.omnisight.skill-ios", category: "FeatureCounter")
#endif

    public init(initial: Int = 0) {
        self.value = initial
    }

    public func increment() {
        value &+= 1
#if canImport(os)
        Self.logger.debug("FeatureCounter incremented to \(self.value, privacy: .public)")
#endif
    }

    public func reset() {
        value = 0
    }
}
