// ios-map-ar (OP-1820) - integrated MapKit + ARKit app surface.

import SwiftUI

struct MapARHomeView: View {
    @StateObject private var store = MapLocationStore()

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                MapKitMapView(store: store)
                    .frame(maxWidth: .infinity)
                    .frame(height: 280)
                    .accessibilityLabel("Map points of interest")

                ARKitOverlayView(
                    selectedPoint: store.selectedPoint,
                    allPoints: store.pointsOfInterest
                )
                .overlay(alignment: .bottomLeading) {
                    selectedPointPanel
                }
                .accessibilityIdentifier("MapARHomeView.arOverlay")
            }
            .navigationTitle("Map AR")
            .task {
                store.requestLocationUpdates()
            }
            .onDisappear {
                store.stopLocationUpdates()
            }
        }
    }

    private var selectedPointPanel: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(store.selectedPoint?.name ?? "Nearby points")
                .font(.headline)
            Text(store.selectedPoint?.summary ?? "Select a map marker to focus the AR anchor.")
                .font(.subheadline)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
        .padding()
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("MapARHomeView.selectedPoint")
    }
}

#Preview {
    MapARHomeView()
}
