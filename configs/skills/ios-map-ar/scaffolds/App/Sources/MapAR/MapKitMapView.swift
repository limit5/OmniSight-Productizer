// ios-map-ar (OP-1820) - SwiftUI MapKit surface.

import Combine
import MapARCore
import MapKit
import SwiftUI

struct MapKitMapView: View {
    @ObservedObject var store: MapLocationStore
    @State private var region: MKCoordinateRegion

    init(store: MapLocationStore) {
        self.store = store
        let center = store.selectedPoint?.coordinate.clLocationCoordinate
            ?? store.pointsOfInterest.first?.coordinate.clLocationCoordinate
            ?? CLLocationCoordinate2D(latitude: 37.334_900, longitude: -122.009_020)
        _region = State(initialValue: MKCoordinateRegion(
            center: center,
            span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)
        ))
    }

    var body: some View {
        Map(
            coordinateRegion: $region,
            showsUserLocation: true,
            annotationItems: store.pointsOfInterest
        ) { point in
            MapAnnotation(coordinate: point.coordinate.clLocationCoordinate) {
                Button {
                    store.select(point)
                    focus(on: point)
                } label: {
                    VStack(spacing: 4) {
                        Image(systemName: point == store.selectedPoint ? "mappin.circle.fill" : "mappin.circle")
                            .font(.title2)
                            .foregroundStyle(point == store.selectedPoint ? .yellow : .teal)
                        Text(point.name)
                            .font(.caption2)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 3)
                            .background(.thinMaterial, in: Capsule())
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Select \(point.name)")
            }
        }
        .accessibilityIdentifier("MapKitMapView.map")
        .onReceive(store.$selectedPoint) { point in
            guard let point else { return }
            focus(on: point)
        }
    }

    private func focus(on point: PointOfInterest) {
        withAnimation {
            region = MKCoordinateRegion(
                center: point.coordinate.clLocationCoordinate,
                span: MKCoordinateSpan(latitudeDelta: 0.006, longitudeDelta: 0.006)
            )
        }
    }
}

#Preview {
    MapKitMapView(store: MapLocationStore())
}
