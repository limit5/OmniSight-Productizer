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
                // Identifier must precede .overlay(): applied after it, SwiftUI
                // propagates it onto the overlay's combined accessibility
                // element and stomps the panel's own 'selectedPoint' identifier.
                .accessibilityIdentifier("MapARHomeView.arOverlay")
                .overlay(alignment: .bottomLeading) {
                    selectedPointPanel
                }
            }
            // Root smoke-test marker. Lives INSIDE the NavigationStack on
            // purpose: accessibility modifiers applied outside the stack are
            // dropped (its content is hosted in a separate UIKit hierarchy),
            // which is why marking ContentView itself never surfaced.
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("ContentView.mapARRoot")
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
