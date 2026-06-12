// ios-map-ar (OP-2124) - Apple-framework-free geographic coordinate value.

public struct GeoCoordinate: Codable, Equatable, Hashable, Sendable {
    public let latitudeDegrees: Double
    public let longitudeDegrees: Double

    public init(latitudeDegrees: Double, longitudeDegrees: Double) {
        self.latitudeDegrees = latitudeDegrees
        self.longitudeDegrees = longitudeDegrees
    }
}
