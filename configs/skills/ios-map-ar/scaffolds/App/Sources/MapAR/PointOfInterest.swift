// ios-map-ar (OP-2124) - CoreLocation conversion glue for MapARCore values.

import CoreLocation
import MapARCore

extension GeoCoordinate {
    init(_ coordinate: CLLocationCoordinate2D) {
        self.init(
            latitudeDegrees: coordinate.latitude,
            longitudeDegrees: coordinate.longitude
        )
    }

    var clLocationCoordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(
            latitude: latitudeDegrees,
            longitude: longitudeDegrees
        )
    }
}
