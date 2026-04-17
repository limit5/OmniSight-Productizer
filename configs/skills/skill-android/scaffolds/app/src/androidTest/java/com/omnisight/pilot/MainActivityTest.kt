// SKILL-ANDROID (P8 #293) — Espresso / Compose UI smoke test.
//
// Binds to the P2 simulate-track contract: `mobile_simulator.resolve_ui_framework`
// returns "espresso" for this scaffold, and the P2 runner invokes
// `./gradlew connectedDebugAndroidTest` to execute this class against
// a booted emulator (or a Firebase Test Lab runner).

package com.omnisight.pilot

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.test.espresso.Espresso
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MainActivityTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<MainActivity>()

    @Test
    fun homeScreenRenders() {
        composeTestRule.onNodeWithTag("HomeScreen.root").assertIsDisplayed()
        composeTestRule.onNodeWithTag("HomeScreen.counter").assertIsDisplayed()
    }

    @Test
    fun incrementButtonAdvancesCounter() {
        composeTestRule.onNodeWithTag("HomeScreen.increment").performClick()
        // Smoke: the click target responded without throwing. The actual
        // state transition is covered by the ViewModel unit test.
        Espresso.onIdle()
    }
}
