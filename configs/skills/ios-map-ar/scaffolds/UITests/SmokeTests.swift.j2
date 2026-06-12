// ios-map-ar (OP-2122) - XCUITest smoke flow for the map/AR overlay.

import XCTest

// XCUIApplication APIs are MainActor-isolated under Swift 6.
@MainActor
final class SmokeTests: XCTestCase {
    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testLaunchShowsMapARSurface() throws {
        let app = XCUIApplication()
        app.launch()

        let root = app.descendants(matching: .any)["ContentView.mapARRoot"]
        let rootFound = root.waitForExistence(timeout: 10)
        if !rootFound {
            // Ground truth for CI logs: what the element tree actually holds.
            print("XCUI ELEMENT TREE DUMP:\n\(app.debugDescription)")
        }
        XCTAssertTrue(rootFound, "ContentView.mapARRoot not in element tree")

        let selectedPoint = app.descendants(matching: .any)["MapARHomeView.selectedPoint"]
        let panelFound = selectedPoint.waitForExistence(timeout: 5)
        if !panelFound {
            print("XCUI ELEMENT TREE DUMP:\n\(app.debugDescription)")
        }
        XCTAssertTrue(panelFound, "MapARHomeView.selectedPoint not in element tree")
    }
}
