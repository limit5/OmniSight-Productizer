// ios-map-ar (OP-1820) - shared point-of-interest model for MapKit + ARKit.

import CoreLocation
import Foundation

struct PointOfInterest: Identifiable, Equatable, Hashable {
    let id: UUID
    let name: String
    let summary: String
    let coordinate: CLLocationCoordinate2D
    let altitudeMeters: CLLocationDistance

    init(
        id: UUID = UUID(),
        name: String,
        summary: String,
        coordinate: CLLocationCoordinate2D,
        altitudeMeters: CLLocationDistance = 0
    ) {
        self.id = id
        self.name = name
        self.summary = summary
        self.coordinate = coordinate
        self.altitudeMeters = altitudeMeters
    }

    static func == (lhs: PointOfInterest, rhs: PointOfInterest) -> Bool {
        lhs.id == rhs.id
            && lhs.name == rhs.name
            && lhs.summary == rhs.summary
            && lhs.coordinate.latitude == rhs.coordinate.latitude
            && lhs.coordinate.longitude == rhs.coordinate.longitude
            && lhs.altitudeMeters == rhs.altitudeMeters
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
        hasher.combine(name)
        hasher.combine(summary)
        hasher.combine(coordinate.latitude)
        hasher.combine(coordinate.longitude)
        hasher.combine(altitudeMeters)
    }
}
