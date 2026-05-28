// OP-1814 (Track B / B-1) — ONVIF client domain models.
//
// Plain immutable value types shared by the WS-Discovery probe
// (WsDiscoveryClient) and the Device/Media SOAP client (OnvifMediaClient).
// Kept dependency-free so they unit-test on the JVM without Android.

package com.omnisight.pilot.onvif

/**
 * A camera discovered on the LAN via WS-Discovery.
 *
 * @param endpointReference the device's urn:uuid EndpointReference Address
 *   — stable per device, used to de-duplicate ProbeMatches that arrive on
 *   more than one interface.
 * @param serviceAddresses the device-service XAddrs advertised in the
 *   ProbeMatch (the SOAP endpoints under which Device/Media services live).
 * @param scopes the onvif:// scope URIs (name / location / hardware) the
 *   device advertised — useful for display and filtering.
 */
data class OnvifDevice(
    val endpointReference: String,
    val serviceAddresses: List<String>,
    val scopes: List<String> = emptyList(),
) {
    /** First advertised service address, or null if the device sent none. */
    val primaryServiceAddress: String?
        get() = serviceAddresses.firstOrNull()
}

/**
 * A media profile returned by the ONVIF Media service `GetProfiles`.
 * The [token] is what `GetStreamUri` is keyed on.
 */
data class MediaProfile(
    val token: String,
    val name: String,
)

/**
 * The result of `GetStreamUri` for a profile: the playable RTSP URI plus
 * the transport hints ONVIF returns alongside it.
 */
data class StreamUri(
    val uri: String,
    val invalidAfterConnect: Boolean = false,
    val invalidAfterReboot: Boolean = false,
) {
    init {
        require(uri.startsWith("rtsp://")) {
            "ONVIF GetStreamUri must yield an rtsp:// URI, got: $uri"
        }
    }
}

/** Credentials for the ONVIF WS-Security UsernameToken (digest) header. */
data class OnvifCredentials(
    val username: String,
    val password: String,
)
