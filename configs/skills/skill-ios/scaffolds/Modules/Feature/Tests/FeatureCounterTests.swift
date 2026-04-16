// SKILL-IOS (P7 #292) — feature module unit tests.

import XCTest
@testable import FeatureModule

final class FeatureCounterTests: XCTestCase {
    func testIncrementsFromZero() {
        let counter = FeatureCounter()
        XCTAssertEqual(counter.value, 0)
        counter.increment()
        XCTAssertEqual(counter.value, 1)
    }

    func testResetReturnsToZero() {
        let counter = FeatureCounter(initial: 5)
        counter.reset()
        XCTAssertEqual(counter.value, 0)
    }

    func testIncrementOverflowWraps() {
        let counter = FeatureCounter(initial: Int.max)
        counter.increment()
        XCTAssertEqual(counter.value, Int.min, "Overflow protected by &+= operator")
    }
}
