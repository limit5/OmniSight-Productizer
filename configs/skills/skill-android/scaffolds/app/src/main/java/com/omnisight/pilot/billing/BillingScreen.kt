// SKILL-ANDROID (P8 #293) — Billing buy-sheet Compose surface.

package com.omnisight.pilot.billing

import android.app.Activity
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@Composable
fun BillingScreen(manager: BillingClientManager) {
    val products by manager.products.collectAsStateWithLifecycle()
    val purchased by manager.purchasedProductIds.collectAsStateWithLifecycle()
    val activity = LocalContext.current as? Activity

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .testTag("BillingScreen.root"),
    ) {
        Text(text = "Available products")
        products.forEach { product ->
            val owned = product.productId in purchased
            Button(
                onClick = {
                    activity?.let { manager.launchBillingFlow(it, product) }
                },
                enabled = !owned,
                modifier = Modifier.semantics { contentDescription = "Buy ${product.productId}" },
            ) {
                Text(text = (if (owned) "Owned: " else "Buy: ") + product.productId)
            }
        }
    }
}
