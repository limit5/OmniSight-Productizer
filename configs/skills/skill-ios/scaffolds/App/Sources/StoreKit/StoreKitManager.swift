// SKILL-IOS (P7 #292) — StoreKit 2 in-app purchase template.
//
// Locked to iOS 16+ via the platform profile floor. The classic
// SKPaymentQueue flow is intentionally NOT shipped — StoreKit 2 is
// strictly better (typed, async, JWS-verified) and supported on every
// device the App Store will accept post-2026.
//
// Public surface:
//   - `loadProducts()`     — fetch the catalogue from Apple.
//   - `purchase(_:)`       — drive the modal sheet, return verified.
//   - `currentEntitlements` — non-consumable + subscription state.
//
// All verification goes through `verifyResult(_:)` which rejects
// `unverified` JWS (Apple's docs are explicit: never trust an
// unverified Transaction).
//
// Sandbox testing is wired through `Configuration.storekit` test plan —
// see fastlane/test_plans/StoreKit.storekit (rendered by the scaffolder
// when storekit=on).

import Foundation
import StoreKit
import os

@Observable
@MainActor
public final class StoreKitManager {
    public enum StoreError: Error, Equatable {
        case productsNotLoaded
        case purchasePending
        case userCancelled
        case verificationFailed(String)
        case unknown(String)
    }

    /// Apple Product IDs the app cares about. In production, source
    /// this from a remote config so you can A/B price points without
    /// shipping a new build.
    public static let productIDs: Set<String> = [
        "com.example.test.consumable.coins_100",
        "com.example.test.nonconsumable.removeads",
        "com.example.test.subscription.monthly_pro",
    ]

    public private(set) var products: [Product] = []
    public private(set) var purchasedProductIDs: Set<String> = []
    public private(set) var lastError: StoreError?

    private static let logger = Logger(subsystem: "com.omnisight.skill-ios", category: "StoreKit")

    private var transactionListenerTask: Task<Void, Never>?

    public init() {
        transactionListenerTask = Task { [weak self] in
            await self?.observeTransactionUpdates()
        }
    }

    deinit {
        transactionListenerTask?.cancel()
    }

    public func loadProducts() async {
        do {
            let fetched = try await Product.products(for: Self.productIDs)
            self.products = fetched.sorted { $0.id < $1.id }
            await refreshEntitlements()
        } catch {
            self.lastError = .unknown(error.localizedDescription)
            Self.logger.error("Product fetch failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    public func purchase(_ product: Product) async throws -> Transaction {
        let result = try await product.purchase()
        switch result {
        case .success(let verification):
            let txn = try verifyResult(verification)
            await txn.finish()
            await refreshEntitlements()
            return txn
        case .pending:
            throw StoreError.purchasePending
        case .userCancelled:
            throw StoreError.userCancelled
        @unknown default:
            throw StoreError.unknown("purchase returned an unknown case")
        }
    }

    /// Apple's StoreKit 2 contract: `unverified` results are spoofed
    /// or tampered — never trust them. This helper centralises the
    /// rejection path so every callsite (purchase / updates / restore)
    /// gets the same treatment.
    public func verifyResult<T>(_ result: VerificationResult<T>) throws -> T {
        switch result {
        case .verified(let value):
            return value
        case .unverified(_, let error):
            throw StoreError.verificationFailed(error.localizedDescription)
        }
    }

    public func refreshEntitlements() async {
        var owned: Set<String> = []
        for await result in Transaction.currentEntitlements {
            if case .verified(let txn) = result {
                owned.insert(txn.productID)
            }
        }
        self.purchasedProductIDs = owned
    }

    private func observeTransactionUpdates() async {
        for await update in Transaction.updates {
            do {
                let txn = try verifyResult(update)
                await txn.finish()
                await refreshEntitlements()
            } catch {
                Self.logger.error(
                    "Transaction update verification failed: \(error.localizedDescription, privacy: .public)"
                )
            }
        }
    }
}
