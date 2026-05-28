// OP-1814 (Track B / B-1) — RTSP playback controller (Media3/ExoPlayer).
//
// Step 3 of the client capability: play the RTSP URI resolved by
// OnvifMediaClient.getStreamUri(). Wraps a Media3 ExoPlayer configured
// with the RTSP MediaSource so the Compose layer (RtspPlayerScreen) only
// deals with a StateFlow of [PlaybackState] and a PlayerView, never the
// player's threading or release rules.
//
// Dependency (add to app/build.gradle.kts — not in the base skeleton):
//   implementation("androidx.media3:media3-exoplayer:1.3.1")
//   implementation("androidx.media3:media3-exoplayer-rtsp:1.3.1")
//   implementation("androidx.media3:media3-ui:1.3.1")
//
// Lifecycle contract: build() once per screen, release() exactly once on
// teardown (RtspPlayerScreen ties this to a DisposableEffect). An
// ExoPlayer instance is single-use and must be released off the hot path
// or it leaks the codec + surface.

package com.omnisight.pilot.rtsp

import android.content.Context
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.rtsp.RtspMediaSource
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** What the player surface needs to know, lifecycle-safe for Compose. */
sealed interface PlaybackState {
    data object Idle : PlaybackState
    data object Buffering : PlaybackState
    data object Playing : PlaybackState
    data object Ended : PlaybackState
    data class Failed(val message: String) : PlaybackState
}

@OptIn(UnstableApi::class)
class RtspPlaybackController(
    private val context: Context,
    /** Some cameras only accept interleaved (TCP) RTP — flip when UDP stalls. */
    private val forceTcp: Boolean = true,
) {
    private val _state = MutableStateFlow<PlaybackState>(PlaybackState.Idle)
    val state: StateFlow<PlaybackState> = _state.asStateFlow()

    private var player: ExoPlayer? = null

    /** The live player, or null before [build] / after [release]. */
    val currentPlayer: ExoPlayer?
        get() = player

    /**
     * Build a player bound to [rtspUri] and start preparing it. Idempotent
     * teardown of any prior player first, so re-pointing at a new camera
     * never leaks the old session.
     */
    fun build(rtspUri: String): ExoPlayer {
        release()
        val source = RtspMediaSource.Factory()
            .setForceUseRtpTcp(forceTcp)
            .createMediaSource(MediaItem.fromUri(rtspUri))

        return ExoPlayer.Builder(context).build().also { exo ->
            exo.addListener(playbackListener)
            exo.setMediaSource(source)
            exo.prepare()
            exo.playWhenReady = true
            player = exo
        }
    }

    /** Release the underlying player exactly once; safe to call repeatedly. */
    fun release() {
        player?.let { exo ->
            exo.removeListener(playbackListener)
            exo.release()
        }
        player = null
        _state.update { PlaybackState.Idle }
    }

    private val playbackListener = object : Player.Listener {
        override fun onPlaybackStateChanged(playbackState: Int) {
            _state.update {
                when (playbackState) {
                    Player.STATE_BUFFERING -> PlaybackState.Buffering
                    Player.STATE_READY -> PlaybackState.Playing
                    Player.STATE_ENDED -> PlaybackState.Ended
                    else -> PlaybackState.Idle
                }
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            _state.update { PlaybackState.Failed(error.errorCodeName) }
        }
    }
}
