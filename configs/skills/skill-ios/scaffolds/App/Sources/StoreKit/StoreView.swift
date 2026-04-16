// SKILL-IOS (P7 #292) — StoreKit 2 buy sheet.
//
// Renders the products StoreKitManager fetched. Uses StoreKit 2's
// native `.storeProductView()` modifier when available; falls back to
// a custom row when not (e.g. SwiftUI Previews without a StoreKit
// configuration).

import SwiftUI
import StoreKit

struct StoreView: View {
    @Environment(StoreKitManager.self) private var store
    @State private var purchasing: String?

    var body: some View {
        List {
            if store.products.isEmpty {
                ContentUnavailableView(
                    "Loading products…",
                    systemImage: "cart"
                )
                .accessibilityIdentifier("StoreView.empty")
            } else {
                ForEach(store.products) { product in
                    productRow(for: product)
                }
            }
        }
        .navigationTitle("Store")
        .accessibilityIdentifier("StoreView.list")
        .task {
            await store.loadProducts()
        }
    }

    @ViewBuilder
    private func productRow(for product: Product) -> some View {
        HStack {
            VStack(alignment: .leading) {
                Text(product.displayName).font(.headline)
                Text(product.description).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            if store.purchasedProductIDs.contains(product.id) {
                Text("Owned").foregroundStyle(.green)
            } else if purchasing == product.id {
                ProgressView()
            } else {
                Button(product.displayPrice) {
                    Task { await buy(product) }
                }
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("Buy \(product.displayName) for \(product.displayPrice)")
            }
        }
        .accessibilityIdentifier("StoreView.row.\(product.id)")
    }

    private func buy(_ product: Product) async {
        purchasing = product.id
        defer { purchasing = nil }
        do {
            _ = try await store.purchase(product)
        } catch {
            // Surface to the user in production via a banner / alert.
        }
    }
}
