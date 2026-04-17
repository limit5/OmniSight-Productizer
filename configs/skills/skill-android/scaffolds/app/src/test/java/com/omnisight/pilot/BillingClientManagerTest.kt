// SKILL-ANDROID (P8 #293) — BillingClientManager unit test skeleton.

package com.omnisight.pilot

import com.omnisight.pilot.billing.BillingClientManager
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Test

class BillingClientManagerTest {

    @Test
    fun productIdsAreNonEmpty() {
        assertTrue(BillingClientManager.PRODUCT_IDS.isNotEmpty())
    }

    @Test
    fun productIdsAreWellFormed() {
        BillingClientManager.PRODUCT_IDS.forEach { id ->
            assertTrue(id.matches(Regex("^[a-z0-9_.]+$")))
            assertFalse(id.endsWith("."))
        }
    }
}
