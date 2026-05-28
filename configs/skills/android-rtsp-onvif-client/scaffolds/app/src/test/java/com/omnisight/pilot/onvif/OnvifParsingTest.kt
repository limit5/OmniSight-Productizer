// OP-1814 (Track B / B-1) — JVM unit tests for the ONVIF parsers.
//
// The socket / HTTP code in WsDiscoveryClient + OnvifMediaClient needs a
// device to exercise, but the XML parsing + WS-Security digest are pure
// and run on the plain JVM — pinned here so a regression in the wire
// format handling fails fast in `gradle test`, no camera required.

package com.omnisight.pilot.onvif

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OnvifParsingTest {

    @Test
    fun parsesProbeMatchXAddrsAndEndpoint() {
        val xml = """
            <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                        xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                        xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">
              <e:Body>
                <d:ProbeMatches>
                  <d:ProbeMatch>
                    <a:EndpointReference>
                      <a:Address>urn:uuid:abcd-0001</a:Address>
                    </a:EndpointReference>
                    <d:Scopes>onvif://www.onvif.org/name/FrontDoor onvif://www.onvif.org/location/lobby</d:Scopes>
                    <d:XAddrs>http://192.168.1.50/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
                  </d:ProbeMatch>
                </d:ProbeMatches>
              </e:Body>
            </e:Envelope>
        """.trimIndent()

        val devices = WsDiscoveryClient.parseProbeMatches(xml)

        assertEquals(1, devices.size)
        val device = devices.single()
        assertEquals("urn:uuid:abcd-0001", device.endpointReference)
        assertEquals(
            "http://192.168.1.50/onvif/device_service",
            device.primaryServiceAddress,
        )
        assertEquals(2, device.serviceAddresses.size)
        assertTrue(device.scopes.any { it.contains("FrontDoor") })
    }

    @Test
    fun nonProbeMatchPayloadYieldsEmpty() {
        assertTrue(WsDiscoveryClient.parseProbeMatches("not xml at all").isEmpty())
        assertTrue(WsDiscoveryClient.parseProbeMatches("<a:Hello/>").isEmpty())
    }

    @Test
    fun parsesMediaProfiles() {
        val xml = """
            <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                        xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                        xmlns:tt="http://www.onvif.org/ver10/schema">
              <s:Body>
                <trt:GetProfilesResponse>
                  <trt:Profiles token="Profile_1"><tt:Name>MainStream</tt:Name></trt:Profiles>
                  <trt:Profiles token="Profile_2"><tt:Name>SubStream</tt:Name></trt:Profiles>
                </trt:GetProfilesResponse>
              </s:Body>
            </s:Envelope>
        """.trimIndent()

        val profiles = OnvifMediaClient.parseProfiles(xml)

        assertEquals(listOf("Profile_1", "Profile_2"), profiles.map { it.token })
        assertEquals("MainStream", profiles.first().name)
    }

    @Test
    fun parsesStreamUri() {
        val xml = """
            <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                        xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                        xmlns:tt="http://www.onvif.org/ver10/schema">
              <s:Body>
                <trt:GetStreamUriResponse>
                  <trt:MediaUri>
                    <tt:Uri>rtsp://192.168.1.50:554/Streaming/Channels/101</tt:Uri>
                    <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
                    <tt:InvalidAfterReboot>true</tt:InvalidAfterReboot>
                  </trt:MediaUri>
                </trt:GetStreamUriResponse>
              </s:Body>
            </s:Envelope>
        """.trimIndent()

        val stream = OnvifMediaClient.parseStreamUri(xml)

        assertEquals("rtsp://192.168.1.50:554/Streaming/Channels/101", stream.uri)
        assertFalse(stream.invalidAfterConnect)
        assertTrue(stream.invalidAfterReboot)
    }

    @Test
    fun passwordDigestIsStableForKnownInputs() {
        // SHA-1(nonce + created + password), Base64. Fixed inputs → fixed
        // digest, so a change in the hashing order trips immediately.
        val nonce = byteArrayOf(0, 1, 2, 3, 4, 5, 6, 7)
        val digest = OnvifMediaClient.passwordDigest(
            nonce = nonce,
            created = "2026-05-28T00:00:00Z",
            password = "admin123",
        )
        assertEquals("vTUGQ60l4bb7SH4/dA85rwZsXZU=", digest)
    }
}
