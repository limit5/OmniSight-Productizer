// ios-map-ar (OP-2124) - unit tests for Apple-framework-free core values.

import XCTest
@testable import MapARCore

final class GeoCoordinateTests: XCTestCase {
    func testGeoCoordinateStoresLatitudeAndLongitude() {
        let coordinate = GeoCoordinate(latitudeDegrees: 37.334_900, longitudeDegrees: -122.009_020)

        XCTAssertEqual(coordinate.latitudeDegrees, 37.334_900)
        XCTAssertEqual(coordinate.longitudeDegrees, -122.009_020)
    }

    func testPointOfInterestUsesGeoCoordinate() {
        let point = PointOfInterest(
            name: "North Gate",
            summary: "Primary entrance marker for AR wayfinding.",
            coordinate: GeoCoordinate(latitudeDegrees: 37.334_900, longitudeDegrees: -122.009_020),
            altitudeMeters: 1.4
        )

        XCTAssertEqual(point.coordinate.latitudeDegrees, 37.334_900)
        XCTAssertEqual(point.coordinate.longitudeDegrees, -122.009_020)
        XCTAssertEqual(point.altitudeMeters, 1.4)
    }
}
