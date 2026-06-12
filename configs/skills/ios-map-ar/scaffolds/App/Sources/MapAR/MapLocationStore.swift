// ios-map-ar (OP-1820) - CoreLocation feed and map selection state.

@preconcurrency import CoreLocation
import Foundation
import MapARCore

@MainActor
final class MapLocationStore: NSObject, ObservableObject, CLLocationManagerDelegate {
    private let manager: CLLocationManager

    @Published private(set) var userLocation: CLLocation?
    @Published private(set) var authorizationStatus: CLAuthorizationStatus
    @Published var selectedPoint: PointOfInterest?

    let pointsOfInterest: [PointOfInterest]

    init(
        manager: CLLocationManager = CLLocationManager(),
        pointsOfInterest: [PointOfInterest] = MapLocationStore.defaultPoints
    ) {
        self.manager = manager
        self.pointsOfInterest = pointsOfInterest
        self.authorizationStatus = manager.authorizationStatus
        self.selectedPoint = pointsOfInterest.first
        super.init()
        self.manager.delegate = self
        self.manager.desiredAccuracy = kCLLocationAccuracyBest
    }

    func requestLocationUpdates() {
        switch authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedAlways, .authorizedWhenInUse:
            manager.startUpdatingLocation()
            manager.startUpdatingHeading()
        case .denied, .restricted:
            break
        @unknown default:
            break
        }
    }

    func stopLocationUpdates() {
        manager.stopUpdatingLocation()
        manager.stopUpdatingHeading()
    }

    func select(_ point: PointOfInterest) {
        selectedPoint = point
    }

    // CLLocationManagerDelegate requirements are nonisolated under Swift 6 strict
    // concurrency — declare them nonisolated and hop to the MainActor for writes.
    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            self.authorizationStatus = status
            self.requestLocationUpdates()
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        let latest = locations.last
        Task { @MainActor in
            self.userLocation = latest
        }
    }

    static let defaultPoints: [PointOfInterest] = [
        PointOfInterest(
            name: "North Gate",
            summary: "Primary entrance marker for AR wayfinding.",
            coordinate: GeoCoordinate(latitudeDegrees: 37.334_900, longitudeDegrees: -122.009_020),
            altitudeMeters: 1.4
        ),
        PointOfInterest(
            name: "Transit Stop",
            summary: "Map anchor for pickup guidance and live AR labels.",
            coordinate: GeoCoordinate(latitudeDegrees: 37.333_520, longitudeDegrees: -122.011_470),
            altitudeMeters: 0.8
        ),
        PointOfInterest(
            name: "Observation Deck",
            summary: "Elevated point rendered above the AR horizon guide.",
            coordinate: GeoCoordinate(latitudeDegrees: 37.332_620, longitudeDegrees: -122.006_260),
            altitudeMeters: 4.0
        ),
    ]
}
