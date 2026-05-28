// OP-1814 (Track B / B-1) — RTSP player Compose screen.
//
// The UI surface for the client: hand it an RTSP URI (the one
// OnvifMediaClient.getStreamUri() resolved for a discovered camera) and
// it renders the live stream in a Media3 PlayerView, with the player's
// lifecycle pinned to the composition via DisposableEffect so the codec
// + surface are released the moment the screen leaves the tree.
//
// Follows the same P4 anti-pattern rules the base HomeScreen does: state
// is a StateFlow read with collectAsStateWithLifecycle(), the PlayerView
// is the only View in an otherwise Compose-first tree, and the connect /
// teardown is effect-scoped — never run as a recomposition side effect.
//
// Dependency: see RtspPlaybackController (media3-exoplayer / -rtsp / -ui).

package com.omnisight.pilot.rtsp

import androidx.annotation.OptIn
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.PlayerView

/**
 * Plays [rtspUri] full-bleed. Owns one [RtspPlaybackController] for the
 * lifetime of the composition; rebuilds it whenever [rtspUri] changes and
 * releases it on dispose.
 */
@OptIn(UnstableApi::class)
@Composable
fun RtspPlayerScreen(
    rtspUri: String,
    modifier: Modifier = Modifier,
    forceTcp: Boolean = true,
) {
    val context = LocalContext.current
    val controller = remember(rtspUri, forceTcp) {
        RtspPlaybackController(context, forceTcp = forceTcp)
    }
    val state by controller.state.collectAsStateWithLifecycle()

    // Build/teardown is effect-scoped — never a recomposition side effect.
    // The PlayerView only *binds* to the already-built player in update().
    DisposableEffect(controller) {
        controller.build(rtspUri)
        onDispose { controller.release() }
    }

    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag("RtspPlayerScreen.root"),
        contentAlignment = Alignment.Center,
    ) {
        AndroidView(
            modifier = Modifier
                .fillMaxSize()
                .semantics { contentDescription = "Live camera stream" },
            factory = { ctx -> PlayerView(ctx).apply { useController = true } },
            update = { view -> view.player = controller.currentPlayer },
        )

        when (val s = state) {
            is PlaybackState.Buffering -> Text(
                text = "Connecting…",
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier
                    .padding(16.dp)
                    .testTag("RtspPlayerScreen.status"),
            )
            is PlaybackState.Failed -> Text(
                text = "Stream error: ${s.message}",
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier
                    .padding(16.dp)
                    .testTag("RtspPlayerScreen.status"),
            )
            else -> Unit
        }
    }
}
