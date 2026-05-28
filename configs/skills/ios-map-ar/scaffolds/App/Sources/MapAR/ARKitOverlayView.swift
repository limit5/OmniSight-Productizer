// ios-map-ar (OP-1820) - RealityKit AR overlay anchored to map points.

import ARKit
import RealityKit
import SwiftUI

struct ARKitOverlayView: UIViewRepresentable {
    let selectedPoint: PointOfInterest?
    let allPoints: [PointOfInterest]

    func makeUIView(context: Context) -> ARView {
        let view = ARView(frame: .zero)
        view.automaticallyConfigureSession = false

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravityAndHeading
        configuration.planeDetection = [.horizontal]
        view.session.run(configuration)

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
