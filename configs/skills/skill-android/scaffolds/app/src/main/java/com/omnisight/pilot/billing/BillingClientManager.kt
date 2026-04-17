// SKILL-ANDROID (P8 #293) — Play Billing Library 7 wrapper.
//
// Locked to minSdk 24 via the platform profile floor (BillingClient 6+
// requires at least that). The classic consumeAsync / acknowledgePurchase
// API is intentionally NOT mixed in — BillingClient 7 + Play Billing 7's
// `onPurchasesUpdated` is strictly better (typed, stable, async).
//
// Public surface:
//   - `startConnection()`       — connect to the Play Billing service.
//   - `queryProductDetails()`   — fetch the catalogue from Play.
//   - `launchBillingFlow(_:)`   — drive the purchase sheet.
//   - `onPurchasesUpdated(…)`   — receive the result callback.
//
// SECURITY:
//   - Every purchase must be verified server-side against the Play
//     Developer API (`purchases.products.get` / `purchases.subscriptions.get`).
//     `verifyPurchase(_:)` is a stub that MUST be replaced with a real
//     backend call before shipping. Granting entitlements on the raw
//     client response is a well-known IAP bypass.

package com.omnisight.pilot.billing

import android.app.Activity
import android.content.Context
import android.util.Log
import com.android.billingclient.api.AcknowledgePurchaseParams
import com.android.billingclient.api.BillingClient
import com.android.billingclient.api.BillingClientStateListener
import com.android.billingclient.api.BillingFlowParams
import com.android.billingclient.api.BillingResult
import com.android.billingclient.api.ProductDetails
import com.android.billingclient.api.Purchase
import com.android.billingclient.api.PurchasesUpdatedListener
import com.android.billingclient.api.QueryProductDetailsParams
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class BillingClientManager(context: Context) : PurchasesUpdatedListener {

    private val _products = MutableStateFlow<List<ProductDetails>>(emptyList())
    val products: StateFlow<List<ProductDetails>> = _products.asStateFlow()

    private val _purchasedProductIds = MutableStateFlow<Set<String>>(emptySet())
    val purchasedProductIds: StateFlow<Set<String>> = _purchasedProductIds.asStateFlow()

    private val _lastError = MutableStateFlow<String?>(null)
    val lastError: StateFlow<String?> = _lastError.asStateFlow()

    private val client: BillingClient = BillingClient.newBuilder(context)
        .enablePendingPurchases()
        .setListener(this)
        .build()

    fun startConnection(onReady: () -> Unit = {}) {
        client.startConnection(object : BillingClientStateListener {
            override fun onBillingServiceDisconnected() {
                Log.w(TAG, "Billing service disconnected; will retry on next flow")
            }

            override fun onBillingSetupFinished(result: BillingResult) {
                if (result.responseCode == BillingClient.BillingResponseCode.OK) {
                    Log.i(TAG, "Billing client ready")
                    onReady()
                } else {
                    _lastError.value = result.debugMessage
                    Log.w(TAG, "Billing setup failed code=${result.responseCode}")
                }
            }
        })
    }

    fun queryProductDetails(productIds: List<String>) {
        val productList = productIds.map { id ->
            QueryProductDetailsParams.Product.newBuilder()
                .setProductId(id)
                .setProductType(BillingClient.ProductType.INAPP)
                .build()
        }
        val params = QueryProductDetailsParams.newBuilder()
            .setProductList(productList)
            .build()
        client.queryProductDetailsAsync(params) { result, detailsList ->
            if (result.responseCode == BillingClient.BillingResponseCode.OK) {
                _products.value = detailsList
            } else {
                _lastError.value = result.debugMessage
            }
        }
    }

    fun launchBillingFlow(activity: Activity, productDetails: ProductDetails) {
        val flowParams = BillingFlowParams.newBuilder()
            .setProductDetailsParamsList(
                listOf(
                    BillingFlowParams.ProductDetailsParams.newBuilder()
                        .setProductDetails(productDetails)
                        .build()
                )
            )
            .build()
        client.launchBillingFlow(activity, flowParams)
    }

    override fun onPurchasesUpdated(result: BillingResult, purchases: MutableList<Purchase>?) {
        if (result.responseCode != BillingClient.BillingResponseCode.OK) {
            _lastError.value = result.debugMessage
            return
        }
        purchases?.forEach { purchase ->
            if (!verifyPurchase(purchase)) {
                _lastError.value = "Purchase verification failed; not granting entitlement"
                return@forEach
            }
            _purchasedProductIds.value = _purchasedProductIds.value + purchase.products
            if (!purchase.isAcknowledged) {
                acknowledge(purchase)
            }
        }
    }

    /// Stub verification. REPLACE with a server-side call to your
    /// backend which in turn asks the Play Developer API (`purchases.products.get`)
    /// to validate the purchase token. Never grant entitlements on a
    /// raw client-side response.
    fun verifyPurchase(purchase: Purchase): Boolean {
        if (purchase.purchaseState != Purchase.PurchaseState.PURCHASED) return false
        // TODO(server-verify): send purchase.purchaseToken to backend /
        // verify there, then return the backend's verdict.
        return true
    }

    private fun acknowledge(purchase: Purchase) {
        val params = AcknowledgePurchaseParams.newBuilder()
            .setPurchaseToken(purchase.purchaseToken)
            .build()
        client.acknowledgePurchase(params) { result ->
            if (result.responseCode != BillingClient.BillingResponseCode.OK) {
                _lastError.value = result.debugMessage
            }
        }
    }

    companion object {
        private const val TAG = "BillingClientManager"

        val PRODUCT_IDS = listOf(
            "com.example.test.consumable.coins_100",
            "com.example.test.nonconsumable.removeads",
            "com.example.test.subscription.monthly_pro",
        )
    }
}
