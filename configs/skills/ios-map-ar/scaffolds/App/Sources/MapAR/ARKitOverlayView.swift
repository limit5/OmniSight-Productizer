// ios-map-ar (OP-1820) - RealityKit AR overlay anchored to map points.

import ARKit
import RealityKit
import SwiftUI

struct ARKitOverlayView: UIViewRepresentable {
    let selectedPoint: PointOfInterest?
    let allPoints: [PointOfInterest]

    /// AR availability guard (design finding 4): ARWorldTrackingConfiguration is
    /// unsupported on the simulator — running it hangs the app at launch. Fall
    /// back to a virtual-camera RealityKit scene so the same anchors render.
    static var isARSupported: Bool {
        #if targetEnvironment(simulator)
        return false
        #else
        return ARWorldTrackingConfiguration.isSupported
        #endif
    }

    func makeUIView(context: Context) -> ARView {
        let view: ARView
        if Self.isARSupported {
            view = ARView(frame: .zero)
            view.automaticallyConfigureSession = false

            let configuration = ARWorldTrackingConfiguration()
            configuration.worldAlignment = .gravityAndHeading
            configuration.planeDetection = [.horizontal]
            view.session.run(configuration)
        } else {
            view = ARView(
                frame: .zero,
                cameraMode: .nonAR,
                automaticallyConfigureSession: false
            )
        }

        context.coordinator.installAnchors(
            in: view,
            allPoints: allPoints,
            selectedPoint: selectedPoint
        )
        return view
    }

    func updateUIView(_ uiView: ARView, context: Context) {
        context.coordinator.installAnchors(
            in: uiView,
            allPoints: allPoints,
            selectedPoint: selectedPoint
        )
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    static func dismantleUIView(_ uiView: ARView, coordinator: Coordinator) {
        uiView.session.pause()
    }

    // RealityKit scene APIs are MainActor-isolated; UIViewRepresentable drives the
    // coordinator on the main thread — make that explicit for Swift 6.
    @MainActor
    final class Coordinator {
        private var renderedSelection: PointOfInterest.ID?

        func installAnchors(
            in view: ARView,
            allPoints: [PointOfInterest],
            selectedPoint: PointOfInterest?
        ) {
            guard renderedSelection != selectedPoint?.id else { return }
            renderedSelection = selectedPoint?.id
            view.scene.anchors.removeAll()

            let points = selectedPoint.map { [$0] } ?? Array(allPoints.prefix(3))
            for (index, point) in points.enumerated() {
                let anchor = AnchorEntity(world: SIMD3<Float>(
                    Float(index) * 0.55 - 0.55,
                    Float(max(0.25, point.altitudeMeters / 10.0)),
                    -1.4 - Float(index) * 0.35
                ))
                anchor.addChild(makeMarker(for: point, selected: point.id == selectedPoint?.id))
                view.scene.addAnchor(anchor)
            }
        }

        private func makeMarker(for point: PointOfInterest, selected: Bool) -> ModelEntity {
            let radius: Float = selected ? 0.13 : 0.09
            let mesh = MeshResource.generateSphere(radius: radius)
            let color: UIColor = selected ? .systemYellow : .systemTeal
            let material = SimpleMaterial(color: color, roughness: 0.35, isMetallic: false)
            let entity = ModelEntity(mesh: mesh, materials: [material])
            entity.name = point.name
            return entity
        }
    }
}
