// ios-map-ar (OP-2124) - shared point-of-interest model for MapKit + ARKit.

import Foundation

public struct PointOfInterest: Codable, Identifiable, Equatable, Hashable, Sendable {
    public let id: UUID
    public let name: String
    public let summary: String
    public let coordinate: GeoCoordinate
    public let altitudeMeters: Double

    public init(
        id: UUID = UUID(),
        name: String,
        summary: String,
        coordinate: GeoCoordinate,
        altitudeMeters: Double = 0
    ) {
        self.id = id
        self.name = name
        self.summary = summary
        self.coordinate = coordinate
        self.altitudeMeters = altitudeMeters
    }
}
